#!/usr/bin/env python3
"""Always-on Mac voice listener.

openWakeWord runs continuously on the built-in mic with two wake words: "Hey Rhasspy" → fast tier
(Haiku) and "Hey Jarvis" → power tier (Opus). On a wake hit it captures the following utterance
(ending on a trailing silence), transcribes it with Groq whisper-large-v3-turbo when GROQ_API_KEY
is set else local whisper.cpp, strips the wake phrase, and enqueues the transcript into Supabase
`voice_requests` with source='mac' (carrying the tier) — the exact same queue the ESP/Fly path
feeds. The Mac worker then drains it into the shared 'hub' conversation, identical to every other
front-end. The Mac is just another set of ears. Live state (listening → working → done) is shown
on screen via the Hammerspoon overlay HUD (see hud()).
"""

import asyncio
import os
import re
import signal
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
MIN_UTTER_S = 0.4      # ignore sub-0.4s "utterances" (just the wake tail)

# Mic-ownership state machine. Two things take the mic from the wake-word listener: the GLOBAL mute
# (app_settings.voice_muted — set by ANY explicit input: push-to-talk dictation OR a hub FAB button)
# and wake-free mode (wakefree_enabled → wakefree.py owns the mic). The listener captures iff neither
# holds it. The mute is ONE shared flag so every source (Mac + ESP) goes deaf together; push-to-talk
# also fires instantly via SIGUSR (Hammerspoon) and propagates to the ESP by writing the flag.
WAKEFREE_RUN = os.path.expanduser("~/repos/voice-pipeline/voice-id/run-wakefree.sh")
_muted = threading.Event()      # set = global voice_muted (an explicit command is being captured)
_wakefree = threading.Event()   # set = wake-free mode on
_capture = threading.Event()    # set = the listener should own the mic (wake-word)
_capture.set()


def _recompute_capture() -> None:
    (_capture.clear if (_muted.is_set() or _wakefree.is_set()) else _capture.set)()


JARED_VOICE_URL = os.environ.get("JARED_VOICE_URL", "https://jared-voice.fly.dev").rstrip("/")


def _ping_esp_capture(on: bool) -> None:
    """Tell the jared-voice server to mute/unmute the ESP, so the global mute reaches it too."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/capture-{'off' if on else 'on'}", timeout=3)
    except Exception:  # noqa: BLE001
        pass


def _ping_esp_wakefree(on: bool) -> None:
    """Bridge the global wake-free mode to the ESP — it switches to speaker-ID capture (on) or back to
    wake words (off), congruent with the Mac. Generous timeout: the server lazy-loads ECAPA on turn-on."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/wakefree-{'on' if on else 'off'}", timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _ping_esp_route(tier: str) -> None:
    """Bridge a wake-free route change (Efficiency/Accuracy) to the ESP so its speaker-ID captures run on
    the same tier as the Mac's — one system, congruent. (The Mac's own wakefree.py reads the route live.)"""
    try:
        httpx.get(f"{JARED_VOICE_URL}/wakefree-route", params={"v": tier}, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _apply_mute(on: bool) -> None:
    """Apply the global mute EVERYWHERE: this Mac (listener mic / wakefree) AND the ESP. One explicit
    command in flight = every source goes deaf. The ESP gets it two ways: an instant HTTP ping AND the
    `voice_muted` DB flag (the source of truth the Fly server reads). Driving it off the DB flag means
    the mute is held EXACTLY as long as push-to-talk is active — no arbitrary timeout can wake the ESP
    mid-dictation (that 90s auto-clear was the bug)."""
    if on == _muted.is_set():
        return
    (_muted.set if on else _muted.clear)()
    _recompute_capture()
    if _wakefree.is_set():
        subprocess.Popen(["pkill", "-USR1" if on else "-USR2", "-f", "wakefree.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=_ping_esp_capture, args=(on,), daemon=True).start()
    threading.Thread(target=_write_setting, args=("voice_muted", "1" if on else "0"), daemon=True).start()


def _on_ptt_start(*_):   # Hammerspoon double-tap → global mute (Mac instant + ESP via /capture)
    _apply_mute(True)


def _on_ptt_stop(*_):
    _apply_mute(False)


def set_wakefree(on: bool) -> None:
    """Toggle wake-free mode (driven by the hub setting). On → listener yields the mic and starts
    wakefree.py; Off → stop wakefree.py and reclaim the mic for wake-word capture."""
    if on and not _wakefree.is_set():
        _wakefree.set(); _recompute_capture()
        log("wake-free ON — yielding mic, starting wakefree.py (+ ESP)")
        try:
            subprocess.Popen([WAKEFREE_RUN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            log(f"failed to start wakefree: {e}")
        # Pin the TTS engine warm for the whole session (don't let it idle-exit between commands and
        # cold-reload), and warm it now so the first command isn't delayed.
        _pin_tts(True); warm_tts()
        threading.Thread(target=_ping_esp_wakefree, args=(True,), daemon=True).start()
    elif not on and _wakefree.is_set():
        log("wake-free OFF — stopping wakefree.py, reclaiming mic (+ ESP)")
        subprocess.Popen(["pkill", "-f", "wakefree.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wakefree.clear(); _recompute_capture()
        _pin_tts(False)   # session over → let the daemon idle-exit and give the ~1 GB back
        threading.Thread(target=_ping_esp_wakefree, args=(False,), daemon=True).start()


# Flag the Claude Code hooks read: present → speak my replies (Kokoro) + write them spoken-word-friendly.
# Maintained here from the hub's voice_inject_mode (this replaces the old Cmd+Opt+C conversation toggle).
INJECT_FLAG = os.path.expanduser("~/.hammerspoon/voice-inject-mode")


def set_inject_mode(on: bool) -> None:
    """Voice inject mode (hub toggle `voice_inject_mode`). On → ensure always-listening (wake-free) is up
    so the Mac mic drives it, and drop the hook flag so my replies are spoken + spoken-word-friendly. Off →
    remove the flag and restore wake-free to its own hub setting. The ESP is gated off by the Fly server
    itself (it reads voice_inject_mode), so the Mac stays the only listener."""
    if on:
        if not _wakefree.is_set():
            set_wakefree(True)
        try:
            open(INJECT_FLAG, "w").close()
        except OSError:
            pass
        log("inject mode ON — Mac-mic → focused window; ESP gated off by the server; replies spoken")
    else:
        try:
            os.remove(INJECT_FLAG)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        set_wakefree(_read_setting("wakefree_enabled"))   # inject forced wake-free on; restore its own toggle
        log("inject mode OFF — wake-free restored to its hub setting")


def _read_setting(key: str) -> bool:
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": f"eq.{key}", "select": "value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        rows = r.json()
        return bool(rows) and str(rows[0].get("value")) not in ("0", "false", "off", "no", "", "None")
    except Exception:  # noqa: BLE001
        return False


def _write_setting(key: str, value: str) -> None:
    """Write a hub setting. Used to make push-to-talk's global mute a DB flag (`voice_muted`) the Fly
    server reads, so the ESP mute is gated on real state instead of an arbitrary server-side timeout."""
    try:
        httpx.patch(f"{SUPABASE_URL}/rest/v1/app_settings",
                    params={"key": f"eq.{key}"},
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"value": value}, timeout=8)
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} [mac-listener] {msg}", flush=True)


# ---- local feedback: a screen-level state HUD + Raspy's reply spoken aloud -----------------------
SPEAK_SH = os.path.expanduser("~/scripts/speak.sh")
HS_BIN = os.environ.get("HS_BIN", "/opt/homebrew/bin/hs")   # Hammerspoon IPC CLI
VOICE_DAEMON = os.path.expanduser("~/scripts/voice_daemon.sh")  # starts the warm Kokoro TTS engine


def hud(state: str) -> None:
    """Drive the Hammerspoon overlay HUD (listening/working/done/error/off). Fire-and-forget so it
    never adds latency to the wake→capture path. The HUD is a screen-level canvas that shows over
    full-screen apps and through Focus/DnD — unlike the old osascript banner, which macOS silently
    suppressed (attributed to Script Editor, killed in full-screen and Do-Not-Disturb)."""
    try:
        subprocess.Popen([HS_BIN, "-c", f'VoiceHUD("{state}")'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def warm_tts() -> None:
    """Pre-warm the Kokoro TTS daemon the moment a command is incoming, so its ~8s model load happens
    DURING capture + the model's thinking and the engine is already hot when the reply is spoken. The
    daemon self-exits after idle, so this only holds the ~1 GB around active voice use — never
    permanently. Idempotent + fire-and-forget (voice_daemon.sh no-ops if it's already running)."""
    try:
        subprocess.Popen([VOICE_DAEMON, "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


KOKORO_PIN = "/tmp/kokoro.pin"   # present = keep the TTS daemon warm (don't idle-exit); see kokoro_daemon.py


def _pin_tts(on: bool) -> None:
    """Pin/unpin the TTS daemon warm. While wake-free is ON we keep the engine resident for the whole
    session so spaced-out commands never hit a cold ~7s reload; OFF lets it idle-exit and free the RAM."""
    try:
        if on:
            open(KOKORO_PIN, "w").close()
        else:
            os.path.exists(KOKORO_PIN) and os.remove(KOKORO_PIN)
    except Exception:  # noqa: BLE001
        pass


def _speak(text: str) -> None:
    """Play a reply aloud via Kokoro (speak.sh), in its own thread so neither the realtime loop nor
    the wake loop is ever blocked by synthesis/playback."""
    try:
        subprocess.run([SPEAK_SH], input=text, text=True, capture_output=True, timeout=90)
    except Exception as e:  # noqa: BLE001
        log(f"speak failed: {e}")


# ---- universal feedback (event-driven) -----------------------------------------------------------
# ONE persistent Supabase realtime subscription that drives ALL universal voice feedback on this Mac,
# regardless of which input captured the command (see voice-pipeline/ARCHITECTURE.md, "Feedback is
# universal"):
#   · voice_speech INSERT   → play the assistant's single spoken line aloud (Kokoro). This is the only
#                             audio channel; the model emits it via the `say` MCP tool.
#   · voice_requests UPDATE → drive the HUD state: status 'claimed' → working, 'done'/'error' → done.
# The local 'listening' state stays on the wake loop below — only this Mac knows its own mic is open.
# No polling, no fixed wait; both tables are in the supabase_realtime publication (00016 / 00021 /
# 00024). The lib server-assigns a distinct id per binding and only invokes the matching callback,
# so the two bindings never cross-fire. Mirrors the worker's RealtimeWaker (same AsyncRealtimeClient).
class RealtimeFeedback:
    def __init__(self):
        self._loop = None
        self._sock = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, name="rt-feedback", daemon=True).start()
        log("realtime feedback starting (speech on voice_speech + universal HUD on voice_requests)")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            log(f"realtime feedback loop exited: {e}")

    async def _serve(self) -> None:
        from realtime import AsyncRealtimeClient
        # Outer loop rebuilds the client if connect()/subscribe() itself fails (e.g. no network at
        # boot); auto_reconnect handles transient drops once connected.
        while True:
            try:
                self._sock = AsyncRealtimeClient(f"{SUPABASE_URL}/realtime/v1", SUPABASE_KEY,
                                                 auto_reconnect=True)
                await self._sock.connect()
                ch = self._sock.channel("mac-listener-feedback")
                ch.on_postgres_changes("INSERT", callback=self._on_speech,
                                       schema="public", table="voice_speech")
                ch.on_postgres_changes("UPDATE", callback=self._on_request,
                                       schema="public", table="voice_requests")
                ch.on_postgres_changes("UPDATE", callback=self._on_setting,
                                       schema="public", table="app_settings")
                await ch.subscribe(self._on_sub)
                while self._sock.is_connected:
                    await asyncio.sleep(1.0)
            except Exception as e:  # noqa: BLE001
                log(f"realtime feedback error: {e}; retrying in 5s")
            try:
                if self._sock:
                    await self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5.0)

    def _on_speech(self, payload) -> None:
        """A new spoken line (any source) → play it aloud. Universal: not gated on who captured it."""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        text = (rec.get("text") or "").strip()
        if text:
            log(f"speaking: {text[:80]!r}")
            threading.Thread(target=_speak, args=(text,), daemon=True).start()

    def _on_setting(self, payload) -> None:
        """A hub setting changed → react to the wake-free toggle and the global mute. Event-driven."""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        key = rec.get("key")
        truthy = str(rec.get("value")) not in ("0", "false", "off", "no", "", "None")
        if key == "wakefree_enabled":
            set_wakefree(truthy)
        elif key == "voice_muted":      # global mute set by any explicit input (PTT / FAB)
            _apply_mute(truthy)
        elif key == "wakefree_route":   # bridge the wake-free route to the ESP so its tier MATCHES the Mac
            v = str(rec.get("value"))   # pass the real route through — never downgrade custom/power to fast
            tier = v if v in ("power", "custom") else "fast"
            threading.Thread(target=_ping_esp_route, args=(tier,), daemon=True).start()
        elif key == "voice_inject_mode":   # hands-free: Mac-mic speech → focused window (ESP off, replies spoken)
            set_inject_mode(truthy)

    def _on_request(self, payload) -> None:
        """A command's status changed (any source) → reflect it on the HUD, and pre-warm TTS on claim.
        Universal. (Warming lives here, not on a voice_requests INSERT binding, because realtime-py
        doesn't reliably support two bindings on the same table; 'claimed' is ~0.3s after INSERT, so
        the timing is unchanged. This Mac's OWN commands are warmed even earlier by the wake loop.)"""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        status = str(rec.get("status") or "")
        if status == "claimed":
            warm_tts()              # any source's command is now being worked → make the TTS engine hot
            log("HUD → working (a command was claimed)")
            hud("working")          # the worker started a turn — every screen shows it
        elif status in ("done", "error"):
            log(f"HUD → done (status={status})")
            hud("done")             # turn ended → HUD shows done, then fades

    def _on_sub(self, state, err=None) -> None:
        log(f"realtime feedback subscription: {state}{(' err=' + str(err)) if err else ''}")


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


def enqueue(text: str, tier: str = ""):
    """Insert the transcript into voice_requests (source='mac', focus='hub'). Returns the new
    request_id (so the reply can be matched the instant it lands) or None."""
    if not (SUPABASE_KEY and text):
        return None
    try:
        payload: dict = {"transcript": text, "source": "mac", "focus": "hub"}
        if tier:
            payload["tier"] = tier
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/voice_requests",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json=payload,
            timeout=10.0,
        )
        rid = None
        try:
            rows = r.json()
            rid = rows[0]["id"] if isinstance(rows, list) and rows else None
        except Exception:  # noqa: BLE001
            pass
        log(f"enqueued (tier={tier or 'default'}, id={rid}): {text[:80]!r}")
        return rid
    except Exception as e:  # noqa: BLE001
        log(f"enqueue failed: {e}")
        return None


def capture_utterance(stream) -> np.ndarray:
    """Read frames until the speaker goes quiet for HANGOVER_S (or MAX_UTTER_S).

    The silence floor is tracked continuously as a running minimum of frame RMS — it snaps down to
    any quieter frame and drifts up only slowly toward sustained noise. That means the loud tail of
    the wake word at the very start of capture can't inflate it, and the floor keeps correcting all
    through the utterance. (The old approach froze a floor from the first 0.35s — which usually
    caught "…asspy" or the command's onset, set the threshold above the speaker's level so the
    command never registered as speech, and ran the full 30s cap. That was the "took forever" bug.)
    Speech = RMS a few × above that floor; once we've heard speech, HANGOVER_S of sub-threshold
    frames ends the utterance.
    """
    frames: list[np.ndarray] = []
    noise = None
    spoke = False
    silence_start = None
    t0 = time.monotonic()
    while True:
        buf, _ = stream.read(FRAME)
        i16 = np.frombuffer(bytes(buf), dtype=np.int16)
        frames.append(i16.copy())
        rms = float(np.sqrt(np.mean((i16.astype(np.float32) / 32768.0) ** 2)))
        now = time.monotonic()

        if noise is None:
            noise = rms
        elif rms < noise:
            noise = rms                            # snap down to any quieter frame (true ambient)
        else:
            noise = 0.99 * noise + 0.01 * rms      # rise slowly toward sustained background noise

        threshold = max(noise * 3.0, 0.015)        # speech sits well above the ambient floor
        if rms >= threshold:
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

    feedback = RealtimeFeedback()
    feedback.start()   # one persistent realtime sub → universal speech + HUD state (any source)

    # Push-to-talk takes the mic via signals; wake-free takes it via the hub toggle. Capture iff neither.
    signal.signal(signal.SIGUSR1, _on_ptt_start)
    signal.signal(signal.SIGUSR2, _on_ptt_stop)
    if _read_setting("wakefree_enabled"):  # apply persisted hub toggle at boot; live flips via RealtimeFeedback
        set_wakefree(True)
    set_inject_mode(_read_setting("voice_inject_mode"))  # apply persisted inject toggle (forces wake-free on if set)
    # Clear any stale global mute on boot: push-to-talk mutes are Mac-local, so a fresh listener means
    # nothing is dictating. This un-sticks a voice_muted left set by a crash mid-dictation — the only
    # safety we need now that the server's arbitrary 90s auto-clear is gone.
    _write_setting("voice_muted", "0")

    while True:
        if not _capture.is_set():
            log("mic yielded (push-to-talk / wake-free) — waiting")
            _capture.wait()              # block until the listener owns the mic again — event-driven
            log("listening (wake word)")
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME, dtype="int16", channels=1) as stream:
            oww.reset()
            while _capture.is_set():
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
                    hud("listening")                    # LOCAL: only this Mac knows its own mic is open
                    warm_tts()                          # pre-warm TTS now — load overlaps capture + thinking
                    pcm = capture_utterance(stream)
                    hud("working")                      # capture done → transcribing / processing
                    text = strip_wake(transcribe(pcm))
                    if text:
                        log(f"transcript (tier={fired_tier}): {text!r}")
                        # working/done HUD and the spoken reply are now driven UNIVERSALLY off
                        # voice_requests status + voice_speech (RealtimeFeedback) — for every source.
                        enqueue(text, tier=fired_tier)
                    else:
                        hud("off")                      # nothing intelligible → clear the local indicator
                        log("…nothing intelligible, ignoring")
                    oww.reset()  # clear the activation so the next wake is fresh


if __name__ == "__main__":
    sys.exit(main())
