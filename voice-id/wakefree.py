#!/usr/bin/env python3
"""Wake-free voice capture — no wake word, gated on Jared's voice.

Continuously listens. Whenever speech energy is present it scores a rolling window against the
enrolled voiceprint (ECAPA exemplar matching). While that's Jared, it accumulates the audio into an
utterance; the utterance closes after ~2s with no Jared-voice (configurable). Each closed utterance is
transcribed (Groq) and enqueued to voice_requests exactly like a wake-word command — source='mac',
tier='fast' (Haiku + 1h prompt caching). The worker's __IGNORE__ directive drops anything that isn't a
real command, so every chunk can be treated as one.

The TV / other people never reach transcription — the speaker-ID gate filters them for free. Runs only
while wake-free mode is toggled on (the listener releases the mic and keeps doing speech + HUD).

Env: SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY (sourced from mac-listener/.env by run-wakefree.sh).
"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
import httpx
import torch

try:
    from speechbrain.inference.speaker import EncoderClassifier
except Exception:  # noqa: BLE001
    from speechbrain.pretrained import EncoderClassifier

HERE = Path(__file__).parent
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
HS_BIN = os.environ.get("HS_BIN", "/opt/homebrew/bin/hs")
VOICE_DAEMON = os.path.expanduser("~/scripts/voice_daemon.sh")  # warm Kokoro TTS engine (pre-warmed at capture)
JARED_VOICE_URL = os.environ.get("JARED_VOICE_URL", "https://jared-voice.fly.dev").rstrip("/")

SR = 16000
FRAME_S = 0.1          # 100ms capture frames — responsive endpointing
ENERGY_GATE = 0.006    # below this RMS = silence (calibrated under Jared's speech ~0.008+)
THRESHOLD = float(os.environ.get("VOICEID_THRESHOLD", "0.30"))   # cosine ≥ this = Jared (overridden by hub setting)
TOPK = 3
PAUSE_S = float(os.environ.get("WAKEFREE_PAUSE_S", "2.0"))       # silence that ends an utterance
MAX_UTTER_S = 30.0
MIN_UTTER_S = 0.5

# Push-to-talk coordination: Hammerspoon sends SIGUSR1 (pause) / SIGUSR2 (resume) while you dictate to
# Claude, so wake-free never fights the mic or executes your dictation. set = capturing; cleared = mic released.
_resumed = threading.Event()
_resumed.set()


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} [wakefree] {msg}", flush=True)


def hud(state: str) -> None:
    try:
        subprocess.Popen([HS_BIN, "-c", f'VoiceHUD("{state}")'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def warm_tts() -> None:
    """Pre-warm the Kokoro TTS daemon the INSTANT speech capture starts — its ~7s cold load then overlaps
    capture + STT + the model's first turn, so the spoken `say` line plays immediately instead of waiting
    on a cold load (the wake-WORD path warms identically at wake detection; this brings parity). The
    daemon self-exits after 30s idle, so it's only resident around active wake-free speech, never held."""
    try:
        subprocess.Popen([VOICE_DAEMON, "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def _write_wav(pcm_f32: np.ndarray) -> str:
    i16 = np.clip(pcm_f32 * 32768.0, -32768, 32767).astype(np.int16)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(i16.tobytes())
    return path


def transcribe(pcm_f32: np.ndarray) -> str:
    if pcm_f32.size < int(SR * MIN_UTTER_S) or not GROQ_API_KEY:
        return ""
    path = _write_wav(pcm_f32)
    try:
        with open(path, "rb") as f:
            r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                           headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                           files={"file": ("clip.wav", f, "audio/wav")},
                           data={"model": GROQ_MODEL, "response_format": "text"}, timeout=30.0)
        return r.text.strip() if r.status_code == 200 else ""
    except Exception as e:  # noqa: BLE001
        log(f"transcribe failed: {e}")
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def route_tier() -> str:
    """Wake-free has no wake word to pick the route per-utterance, so it runs on ONE configurable route:
    app_settings.wakefree_route ('fast'=Haiku / 'power'=Opus). Read live per command (event-driven, one
    GET when a real command fires — not a poll) so the hub toggle takes effect without a restart."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "eq.wakefree_route", "select": "value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        rows = r.json()
        v = rows[0].get("value") if rows else None
        if v in ("power", "custom"):
            return v
    except Exception:  # noqa: BLE001
        pass
    return "fast"


def _esp_working() -> None:
    """Tell the ESP a (Mac-captured) command is now being worked → its LED shows the universal 'working'
    blink immediately. Without this the ESP, which also heard the utterance but dropped its own far-mic
    copy as 'not Jared', would briefly show idle before the worker's slower blink-ping lands."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/led-blink", timeout=3)
    except Exception:  # noqa: BLE001
        pass


def enqueue(text: str) -> None:
    if not (SUPABASE_KEY and text):
        return
    try:
        httpx.post(f"{SUPABASE_URL}/rest/v1/voice_requests",
                   headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                            "Content-Type": "application/json", "Prefer": "return=minimal"},
                   json={"transcript": text, "source": "mac", "focus": "hub", "tier": route_tier()}, timeout=10.0)
        log(f"enqueued: {text[:80]!r}")
        threading.Thread(target=_esp_working, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log(f"enqueue failed: {e}")


def get_setting(key: str, default: float) -> float:
    """Read a numeric setting from the hub's shared app_settings so the hub UI controls it. Falls back
    to the default if unset/unreachable."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": f"eq.{key}", "select": "value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        rows = r.json()
        if rows and rows[0].get("value") not in (None, ""):
            return float(rows[0]["value"])
    except Exception:  # noqa: BLE001
        pass
    return default


def trim_voiced(pcm: np.ndarray, gate: float, margin_s: float = 0.15) -> np.ndarray:
    """Keep only the voiced span (first→last 50ms frame above the gate) + a small margin. Trimming
    leading/trailing silence is what makes SHORT commands work — otherwise a 1s command padded with
    silence dilutes the speaker embedding below threshold and reads as 'not Jared'."""
    if pcm.size == 0:
        return pcm
    step = int(0.05 * SR)
    voiced = [i for i in range(0, len(pcm) - step, step)
              if float(np.sqrt(np.mean(pcm[i:i + step] ** 2))) >= gate]
    if not voiced:
        return np.array([], dtype=np.float32)
    m = int(margin_s * SR)
    return pcm[max(0, voiced[0] - m): min(len(pcm), voiced[-1] + step + m)]


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY required.", file=sys.stderr); return 2
    ref_bank = HERE / "exemplars.npy"
    if not ref_bank.exists():
        print("FATAL: no exemplars.npy — run enroll.py first.", file=sys.stderr); return 2
    bank = np.load(ref_bank)

    global THRESHOLD, PAUSE_S      # hub UI controls these via app_settings (fall back to the defaults above)
    THRESHOLD = get_setting("voiceid_threshold", THRESHOLD)
    PAUSE_S = get_setting("wakefree_pause_s", PAUSE_S)

    log("loading ECAPA…")
    enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(HERE / ".model"))
    log(f"wake-free listening — Jared's voice only (thresh={THRESHOLD}, pause={PAUSE_S}s, stt=groq:{GROQ_MODEL})")

    def score(seg_f32: np.ndarray) -> float:
        e = enc.encode_batch(torch.tensor(seg_f32).unsqueeze(0)).squeeze().detach().cpu().numpy()
        e = e / (np.linalg.norm(e) + 1e-9)
        sims = bank @ e
        k = min(TOPK, len(sims))
        return float(np.mean(np.sort(sims)[-k:]))

    # Segment on speech ENERGY (any length), then verify the speaker ONCE on the trimmed utterance.
    frame = int(FRAME_S * SR)
    # Push-to-talk releases the mic: SIGUSR1 pauses capture (closes the stream), SIGUSR2 resumes.
    signal.signal(signal.SIGUSR1, lambda *_: _resumed.clear())
    signal.signal(signal.SIGUSR2, lambda *_: _resumed.set())

    while True:
        if not _resumed.is_set():
            log("paused (push-to-talk) — mic released")
            _resumed.wait()                    # block until resumed — event-driven, no poll
            log("resumed — listening")
        seg: list[np.ndarray] = []
        prev = np.zeros(frame, dtype=np.float32)   # 1-frame pre-roll so the first phoneme isn't clipped
        capturing = False
        silence_start = None
        t0 = 0.0
        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=frame) as stream:
            while _resumed.is_set():
                chunk, _ = stream.read(frame)
                chunk = chunk.flatten()
                now = time.monotonic()
                voiced = float(np.sqrt(np.mean(chunk ** 2))) >= ENERGY_GATE

                if voiced and not capturing:
                    capturing = True
                    seg = [prev, chunk]
                    silence_start = None
                    t0 = now
                    hud("listening")
                    warm_tts()      # speech started → warm TTS now so the spoken reply isn't cold-delayed
                elif capturing:
                    seg.append(chunk)
                    silence_start = None if voiced else (silence_start or now)
                    if (silence_start and now - silence_start >= PAUSE_S) or (now - t0 >= MAX_UTTER_S):
                        pcm = trim_voiced(np.concatenate(seg), ENERGY_GATE)
                        capturing = False
                        seg = []
                        silence_start = None
                        if pcm.size < int(SR * MIN_UTTER_S):
                            hud("off")
                        else:
                            sim = score(pcm)               # speaker-ID on the whole trimmed command
                            if sim >= THRESHOLD:
                                text = transcribe(pcm)
                                if text:
                                    log(f"Jared (sim={sim:.2f}) → {text!r}")
                                    enqueue(text)          # worker drives working/done HUD + speech via the listener
                                else:
                                    hud("off")
                            else:
                                hud("off")
                                log(f"not Jared (sim={sim:.2f}) — skipped")
                prev = chunk


if __name__ == "__main__":
    sys.exit(main())
