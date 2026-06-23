#!/usr/bin/env python3
"""Always-on Mac voice listener.

openWakeWord ("hey_rhasspy") runs continuously on the built-in mic. On a wake hit it captures the
following utterance (ending on a trailing silence — same 3s convention as the ESP), transcribes it
LOCALLY with whisper.cpp (no Groq, no network for STT), strips the wake phrase, and enqueues the
transcript into Supabase `voice_requests` with source='mac' — the exact same queue the ESP/Fly path
feeds. The Mac worker then drains it into the shared 'hub' conversation, identical to every other
front-end. The Mac is just another set of ears.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone

import numpy as np
import sounddevice as sd
import httpx
from openwakeword.model import Model

# ---- config (env, with sane defaults) -------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")   # service_role or anon — only needs INSERT on voice_requests
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_rhasspy")          # fast tier → Haiku
WAKE_MODEL_POWER = os.environ.get("WAKE_MODEL_POWER", "hey_jarvis") # power tier → Opus + ultrathink
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
WHISPER_BIN = os.path.expanduser(os.environ.get("WHISPER_BIN", "~/whisper.cpp/build/bin/whisper-cli"))
WHISPER_MODEL = os.path.expanduser(os.environ.get("WHISPER_MODEL", "~/whisper.cpp/models/ggml-medium.en.bin"))
# Transcription: Groq whisper-large-v3-turbo when a key is set (faster + more accurate, same as the
# ESP/web path); otherwise local whisper.cpp (offline, no key). Set GROQ_API_KEY to switch over.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")

SAMPLE_RATE = 16000
FRAME = 1280            # 80 ms @ 16 kHz — openWakeWord's expected chunk
HANGOVER_S = float(os.environ.get("HANGOVER_S", "3.0"))   # trailing silence that ends an utterance
MAX_UTTER_S = 30.0
CAL_S = 0.35           # learn the ambient noise floor over the first moments of capture
MIN_UTTER_S = 0.4      # ignore sub-0.4s "utterances" (just the wake tail)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} [mac-listener] {msg}", flush=True)


# ---- local feedback: a visual wake notification + Raspy's reply spoken aloud --------------------
SPEAK_SH = os.path.expanduser("~/scripts/speak.sh")


def notify(msg: str) -> None:
    """Visual 'I heard you' cue (macOS notification) — no audio chime before the command."""
    try:
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(msg)} with title "Raspy"'],
                       capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _latest_assistant_id():
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_messages",
                      params={"focus": "eq.hub", "role": "eq.assistant", "order": "created_at.desc",
                              "limit": "1", "select": "id"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        rows = r.json()
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        return None


def speak_reply(baseline) -> None:
    """Wait for Raspy's reply to land in the shared thread (whichever listener's request produced it —
    robust to dedup), then speak it aloud. Background thread → never blocks the wake loop."""
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_messages",
                          params={"focus": "eq.hub", "role": "eq.assistant", "order": "created_at.desc",
                                  "limit": "1", "select": "id,text"},
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
            rows = r.json()
            if rows and rows[0]["id"] != baseline and (rows[0].get("text") or "").strip():
                try:
                    subprocess.run([SPEAK_SH], input=rows[0]["text"], text=True, capture_output=True, timeout=90)
                except Exception as e:  # noqa: BLE001
                    log(f"speak failed: {e}")
                return
        except Exception:  # noqa: BLE001
            pass


def strip_wake(text: str) -> str:
    """Drop a leading wake phrase the STT often captures (tolerant of mishears)."""
    text = re.sub(
        r"^\W*(hey|hi|ok|okay)?\W*(rhasspy|rhaspy|raspy|rasbee|rasby|raspberry|rasp)\b[\s,.!:;-]*",
        "", text or "", count=1, flags=re.IGNORECASE,
    ).strip()
    return re.sub(
        r"^\W*(hey|hi|ok|okay)?\W*(jarvis|jarvas|jervis)\b[\s,.!:;-]*",
        "", text, count=1, flags=re.IGNORECASE,
    ).strip()


def _write_wav(pcm: np.ndarray) -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return path


def _groq_transcribe(path: str):
    """whisper-large-v3-turbo on Groq — faster + more accurate than local. None → fall back to local."""
    try:
        with open(path, "rb") as f:
            r = httpx.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("clip.wav", f, "audio/wav")},
                data={"model": GROQ_MODEL, "response_format": "text"},
                timeout=30.0,
            )
        if r.status_code == 200:
            return r.text.strip()
        log(f"groq {r.status_code} — falling back to local whisper")
    except Exception as e:  # noqa: BLE001
        log(f"groq error ({e}) — falling back to local whisper")
    return None


def transcribe(pcm: np.ndarray) -> str:
    """Groq (fast + accurate) when GROQ_API_KEY is set; else local whisper.cpp. '' if too short/failed."""
    if pcm.size < int(SAMPLE_RATE * MIN_UTTER_S):
        return ""
    path = _write_wav(pcm)
    try:
        if GROQ_API_KEY:
            text = _groq_transcribe(path)
            if text is not None:
                return text
        out = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", path, "--no-prints", "-nt", "-l", "en"],
            capture_output=True, text=True, timeout=90,
        )
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        log(f"transcribe failed: {e}")
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def enqueue(text: str, tier: str = "") -> None:
    """Insert the transcript into voice_requests (source='mac', focus='hub'). Best-effort."""
    if not (SUPABASE_KEY and text):
        return
    try:
        payload: dict = {"transcript": text, "source": "mac", "focus": "hub"}
        if tier:
            payload["tier"] = tier
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/voice_requests",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=payload,
            timeout=10.0,
        )
        log(f"enqueued (tier={tier or 'default'}): {text[:80]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"enqueue failed: {e}")


def capture_utterance(stream) -> np.ndarray:
    """Read frames until the speaker goes quiet for HANGOVER_S (or MAX_UTTER_S). Adaptive floor."""
    frames: list[np.ndarray] = []
    floor = None
    cal: list[float] = []
    spoke = False
    silence_start = None
    t0 = time.monotonic()
    while True:
        buf, _ = stream.read(FRAME)
        i16 = np.frombuffer(bytes(buf), dtype=np.int16)
        frames.append(i16.copy())
        rms = float(np.sqrt(np.mean((i16.astype(np.float32) / 32768.0) ** 2)))
        now = time.monotonic()
        if now - t0 < CAL_S:
            cal.append(rms)
        else:
            if floor is None:
                base = (sum(cal) / len(cal)) if cal else 0.01
                floor = max(base * 2.5, 0.02)
            if rms >= floor:
                spoke = True
                silence_start = None
            elif spoke:
                if silence_start is None:
                    silence_start = now
                elif now - silence_start >= HANGOVER_S:
                    break
        if now - t0 >= MAX_UTTER_S:
            break
    return np.concatenate(frames) if frames else np.array([], dtype=np.int16)


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY required (INSERT on voice_requests).", file=sys.stderr)
        return 2
    stt = f"groq:{GROQ_MODEL}" if GROQ_API_KEY else "whisper.cpp (local)"
    if not GROQ_API_KEY and (not os.path.exists(WHISPER_BIN) or not os.path.exists(WHISPER_MODEL)):
        print(f"FATAL: no GROQ_API_KEY and whisper.cpp not found ({WHISPER_BIN}).", file=sys.stderr)
        return 2

    log(f"loading wake models: fast={WAKE_MODEL!r} power={WAKE_MODEL_POWER!r} (threshold {WAKE_THRESHOLD})…")
    oww = Model(wakeword_models=[WAKE_MODEL, WAKE_MODEL_POWER], inference_framework="onnx")
    log(f"listening — 'Hey Rhasspy' (fast/Haiku) or 'Hey Jarvis' (power/Opus)  (stt={stt}, hangover={HANGOVER_S}s)")

    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME, dtype="int16", channels=1) as stream:
        while True:
            buf, _ = stream.read(FRAME)
            audio = np.frombuffer(bytes(buf), dtype=np.int16)
            scores = oww.predict(audio)
            fast_score = scores.get(WAKE_MODEL, 0.0)
            power_score = scores.get(WAKE_MODEL_POWER, 0.0)
            fired_tier = None
            if power_score >= WAKE_THRESHOLD:
                fired_tier = "power"   # power checked first — higher specificity wins ties
            elif fast_score >= WAKE_THRESHOLD:
                fired_tier = "fast"
            if fired_tier:
                log(f"wake! tier={fired_tier} (fast={fast_score:.2f} power={power_score:.2f}) — capturing…")
                notify("🎙 Listening…")
                baseline = _latest_assistant_id()   # snapshot before this command's reply lands
                pcm = capture_utterance(stream)
                text = strip_wake(transcribe(pcm))
                if text:
                    log(f"transcript (tier={fired_tier}): {text!r}")
                    enqueue(text, tier=fired_tier)
                    threading.Thread(target=speak_reply, args=(baseline,), daemon=True).start()
                else:
                    log("…nothing intelligible, ignoring")
                oww.reset()  # clear the activation so the next wake is fresh


if __name__ == "__main__":
    sys.exit(main())
