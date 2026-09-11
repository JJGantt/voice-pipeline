#!/usr/bin/env python3
"""Wake-free voice capture — no wake word, gated on Jared's voice.

Continuously listens. Whenever speech energy is present it scores a rolling window against the
enrolled voiceprint (ECAPA exemplar matching). While that's Jared, it accumulates the audio into an
utterance; the utterance closes after ~2s with no Jared-voice (configurable). Each closed utterance is
transcribed (Groq) and enqueued to voice_requests exactly like a wake-word command (source='mac').
The worker's __IGNORE__ directive drops anything that isn't a real command, so every chunk can be
treated as one.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Push-to-talk coordination: Hammerspoon sends SIGUSR1 (pause) / SIGUSR2 (resume) while you dictate to
# Claude, so wake-free never fights the mic or executes your dictation. set = capturing; cleared = muted.
# The pause is a SOFTWARE mute: frames keep arriving and are discarded. The stream is never stopped —
# AudioDeviceStop from a paused state is what deadlocked in the HAL mutex on Jun 25 and froze this
# process for six days. CoreAudio input is shared on macOS, so holding the stream open never blocks
# the push-to-talk recorder itself.
_resumed = threading.Event()
_resumed.set()
# Installed BEFORE the imports below: SIGUSR1/2 terminate a Python process that has no handler for them, and
# the listener sends one whenever push-to-talk starts or stops — including during the seconds this process
# spends importing torch and loading ECAPA. Respawns logged as rc=-30 / rc=-31 were exactly that window.
signal.signal(signal.SIGUSR1, lambda *_: _resumed.clear())
signal.signal(signal.SIGUSR2, lambda *_: _resumed.set())

from collections import deque

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
# Shared with the listener (mac-listener/listener.py) — both processes drive the one on-screen pill.
HUD_STATE_FILE = os.path.expanduser("~/.cache/voice-mac-listener/hud-state")
_hud_send_lock = threading.Lock()
VOICE_DAEMON = os.path.expanduser("~/scripts/voice_daemon.sh")  # warm Kokoro TTS engine (pre-warmed at capture)
JARED_VOICE_URL = os.environ.get("JARED_VOICE_URL", "https://jared-voice.fly.dev").rstrip("/")
# 'Heard you' cue for inject mode: inject bypasses the hub/worker and hands off to the focused window, so
# it has no TV/Pi round-trip to signal you. Play the same cue locally on the Mac so you know it caught you.
HEARD_WAV = os.path.expanduser(os.environ.get("HEARD_WAV", "~/voice/sfx-candidates/02_initial_message-pop.wav"))

SR = 16000
FRAME_S = 0.1          # 100ms capture frames — responsive endpointing
ENERGY_GATE = 0.006    # below this RMS = silence (calibrated under Jared's speech ~0.008+)
THRESHOLD = float(os.environ.get("VOICEID_THRESHOLD", "0.30"))   # cosine ≥ this = Jared (overridden by hub setting)
TOPK = 3
MAX_UTTER_S = 600.0   # runaway backstop ONLY (matches the ESP server) — the speaker gate ends a command
                      # when you actually stop; this never touches real speech. Was a stale 30s default.
MIN_UTTER_S = 0.5
# Speaker-gated endpointing: keep the utterance alive on Jared's VERIFIED presence (rolling ECAPA), not a
# blind energy timeout — so thinking-pauses don't chop a command. Mirrors the Fly ESP server's design.
VAD_WINDOW_S = 2.0           # trailing audio scored per check
VAD_CHECK_EVERY_S = 0.7      # re-score cadence while capturing
VAD_FIRST_CHECK_S = 1.4      # min audio before the first check (the close Mac mic confirms fast)
VAD_NO_JARED_ABORT_S = 3.5  # never confirmed within this much audio → not Jared, drop
KEEPALIVE_MARGIN = 0.16      # hysteresis: once confirmed, stay locked unless match falls to ~not-you/silence
# How many CONSECUTIVE non-confirming checks close a capture — event-based, NOT a timer that can race
# the check cadence and cut you off between words. The number SCALES with how long you have been
# talking, because the two cases pull opposite ways. A short command is one breath: closing on the
# first non-confirm ends it the moment you stop, instead of holding the mic open over whatever is said
# next. A long, multi-part command has real thinking-pauses in it, and cutting one in half loses the
# whole command — which costs far more than a second of trailing silence. Each extra miss buys roughly
# another VAD_CHECK_EVERY_S of pause.
ABSENT_MISSES_MIN = 1        # short command: close on the first non-confirm (~2s of silence)
ABSENT_MISSES_MAX = 5        # ceiling — past this only MAX_UTTER_S is left to stop a runaway capture
ABSENT_RAMP_S = 8.0          # +1 tolerated miss per this much speech (~2.0s of pause at the floor,
                             # ~4.8s at the ceiling — a 50s dictated command survives a 4s thinking-pause)
PREROLL_S = 1.2              # audio kept from before the energy trigger (same as the ESP server). Was one
                             # 100ms frame here, which clipped a soft first syllable the gate only caught
                             # mid-word. Excluded from the VAD timing (seed_s) exactly as the server does.
# Back-fill — see server/server.py for the full reasoning and the measurements behind BACKFILL_THR. A capture
# that never confirms you inside VAD_NO_JARED_ABORT_S is dropped as noise; if you are mid-sentence the next
# capture opens on the next frame and used to lose everything before it (56 of 184 Mac commands in the week
# to 2026-09-10 began within 1s of such a drop). Now the raw audio before the trigger is kept, and an ACCEPTED
# capture walks backward through it, prepending each 2s window that still scores as you. MUST match server.py.
HISTORY_S = 8.0
BACKFILL_MAX_S = 5.0
BACKFILL_STEP_S = 0.5
BACKFILL_THR = 0.22


def absent_misses(audio_s: float) -> int:
    """Consecutive non-confirms needed to close a capture that has run this long. MUST match the ESP
    server (server/server.py): the two mics have to endpoint identically, or the same sentence is cut
    differently depending on which one heard it and the pair stops collapsing into one command."""
    return min(ABSENT_MISSES_MAX, ABSENT_MISSES_MIN + int(audio_s // ABSENT_RAMP_S))


# CoreAudio liveness: stream.read returns at a fixed hardware cadence (every 100ms), muted or not. The
# watchdog fires only when that guaranteed cadence stops while we're actually reading — i.e. the stream
# is wedged inside CoreAudio — and hard-exits WITHOUT unwinding (os._exit skips the stream teardown that
# deadlocks). _expect_frames is cleared around known-blocking work (Groq transcription etc.) where reads
# legitimately pause. The listener supervises this process and respawns it.
_last_frame = time.monotonic()
_expect_frames = threading.Event()
_expect_frames.set()
WATCHDOG_EXIT_CODE = 3


def _lid_closed() -> bool:
    """True when the laptop lid is shut — see the listener's copy for why this gate exists. Checked only
    when the watchdog is about to bail, and unreadable state counts as open."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return False
    return '"AppleClamshellState" = Yes' in out


def _open_input_stream(frame: int):
    """Open the capture stream, waiting out a closed lid instead of dying into a respawn loop — the
    listener respawns this process on any exit, so an unhandled PortAudioError against a powered-down
    HAL becomes a tight restart cycle. A PortAudioError with the lid OPEN is a real fault and still
    propagates."""
    waited = False
    while True:
        stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=frame)
        try:
            stream.start()      # where a powered-down HAL actually fails; a second start() in `with` is a no-op
        except sd.PortAudioError as exc:
            stream.close()
            if not _lid_closed():
                raise
            if not waited:
                log(f"lid closed — CoreAudio won't bind an input stream ({exc}); waiting for it to open")
                waited = True
            time.sleep(5)
            continue
        if waited:
            log("lid open — capture stream bound")
        return stream


def _watchdog() -> None:
    """Exit for respawn on a wedged stream — but never while the lid is shut. The listener respawns this
    process on rc=3, so a lid-closed exit loop is a two-process version of the same ~15s thrash that ran
    all night on 2026-08-01 (`wakefree.py died rc=3 after 13s`, over and over). With the lid down there is
    no working HAL to respawn onto; hold, and let the >10s test judge the stream once it opens.

    `baseline` is the watchdog's own reference so _last_frame stays the honest mic-liveness signal that
    _alive_beacon reports to the hub."""
    lid_shut = False
    baseline = time.monotonic()
    while True:
        time.sleep(5)
        if not _expect_frames.is_set():     # known-blocking work (Groq transcription) — reads legitimately pause
            baseline = time.monotonic()
            continue
        baseline = max(baseline, _last_frame)
        if time.monotonic() - baseline <= 10:
            continue
        if _lid_closed():
            if not lid_shut:
                log("lid closed — audio hardware is down, not a wedged stream; holding until it opens")
                lid_shut = True
            baseline = time.monotonic()     # re-arm, so the wedge test starts over when the lid opens
            continue
        if lid_shut:
            log("lid open — audio hardware back; judging the stream from here")
            lid_shut = False
        log("no audio frames for >10s — CoreAudio stream wedged; exiting for respawn")
        os._exit(WATCHDOG_EXIT_CODE)


def _alive_beacon() -> None:
    """Mic-liveness beacon: stamp voice_presence.alive_at every 30s WHILE frames are flowing. The hub
    shows a stale beacon as 'mic down' — the signal that would have caught the Jun 25 six-day hang.
    Gated on _last_frame (real audio arriving), not on this process merely existing."""
    while True:
        if time.monotonic() - _last_frame < 10:
            try:
                httpx.post(f"{SUPABASE_URL}/rest/v1/voice_presence?on_conflict=source",
                           headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                    "Content-Type": "application/json",
                                    "Prefer": "resolution=merge-duplicates,return=minimal"},
                           json={"source": "mac-wakefree",
                                 "alive_at": datetime.now(timezone.utc).isoformat()}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(30)


# Governor brake: run as a PASSIVE activity sensor — confirm Jared's voice and stamp the shared
# heard_jared_at (so the worker can re-arm always-on when the room goes quiet), but never capture,
# transcribe, enqueue, or touch the HUD/TTS. Spawned by the listener with this env while braked.
SENSOR = os.environ.get("WAKEFREE_SENSOR") == "1"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} [{'wf-sensor' if SENSOR else 'wakefree'}] {msg}", flush=True)


def hud(state: str) -> None:
    """Drive the Hammerspoon overlay pill. Redundant states are dropped rather than sent, against the
    SAME state file the listener writes — one pill, one state, whichever process last set it.
    No-op in sensor mode: passive sensing must not paint 'listening'/'locked' over the wake-word pill.

    Hammerspoon's IPC port is not safe to flood: its error path logs back through the same port and
    re-enters the runloop, so queued requests recurse instead of unwinding (it crashed that way on
    2026-08-01). This process is respawned every ~15s while the audio stack is wedged, and each start
    re-asserted the pill; on disk, because a fresh process would otherwise remember nothing."""
    if SENSOR:
        return
    try:
        with _hud_send_lock:
            try:
                with open(HUD_STATE_FILE) as fh:
                    if fh.read().strip() == state:
                        return
            except OSError:
                pass                                    # no record yet → send it
            os.makedirs(os.path.dirname(HUD_STATE_FILE), exist_ok=True)
            with open(HUD_STATE_FILE, "w") as fh:
                fh.write(state)
            subprocess.Popen([HS_BIN, "-c", f'VoiceHUD("{state}")'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def cue() -> None:
    """Play the 'heard you' cue locally on the Mac (afplay) — used in inject mode, where there's no hub
    round-trip to signal capture. Fire-and-forget; missing file or afplay is a silent no-op."""
    try:
        if os.path.exists(HEARD_WAV):
            subprocess.Popen(["afplay", HEARD_WAV], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def publish_presence(jared: bool) -> None:
    """Upsert the Mac's row in the shared voice_presence — the system-wide 'who is hearing Jared' truth.
    Fire-and-forget in a daemon thread so the audio loop never stalls on the network call. Any mic's
    fresh heard_jared_at makes the whole system 'Hearing Jared' (drives every indicator + send-suppression)."""
    def _post():
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            body = {"source": "mac", "updated_at": now_iso, "heard_sound_at": now_iso}
            if jared:
                body["heard_jared_at"] = now_iso
            httpx.post(f"{SUPABASE_URL}/rest/v1/voice_presence?on_conflict=source",
                       headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                "Content-Type": "application/json",
                                "Prefer": "resolution=merge-duplicates,return=minimal"},
                       json=body, timeout=5)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_post, daemon=True).start()


def other_heard_jared():
    """Most recent time a mic OTHER than the Mac confirmed Jared (datetime, or None). At capture close the
    Mac yields ONLY if the ESP clearly heard you more recently (the Mac is the primary mic)."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_presence",
                      params={"source": "neq.mac", "select": "heard_jared_at"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=4)
        stamps = [datetime.fromisoformat(row["heard_jared_at"]) for row in r.json() if row.get("heard_jared_at")]
        return max(stamps) if stamps else None
    except Exception:  # noqa: BLE001
        return None


def warm_tts() -> None:
    """Pre-warm the Kokoro TTS daemon the INSTANT speech capture starts — its ~7s cold load then overlaps
    capture + STT + the model's first turn, so the spoken `say` line plays immediately instead of waiting
    on a cold load (the wake-WORD path warms identically at wake detection; this brings parity). The
    daemon self-exits after 30s idle, so it's only resident around active wake-free speech, never held.
    No-op in sensor mode — nothing will be spoken."""
    if SENSOR:
        return
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


LED_TOKEN = os.environ.get("LED_TOKEN", "")


def _esp_working() -> None:
    """Tell the ESP a (Mac-captured) command is now in flight → its LED shows the universal 'working'
    blink immediately. Without this the ESP, which also heard the utterance but dropped its own far-mic
    copy as 'not Jared', would briefly show idle before the worker's slower blink-ping lands."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/led-blink",
                  params={"token": LED_TOKEN} if LED_TOKEN else None, timeout=3)
    except Exception:  # noqa: BLE001
        pass


def open_request(cap_start: str, cap_end: str) -> str | None:
    """Open the command's lifecycle row at CAPTURE-ACCEPT (status 'transcribing', no transcript yet).
    This is what turns the working indicators on — the floor lamp goes amber on this insert and the
    ESP blink is pinged alongside it — at the earliest moment the system has committed to the command,
    not after STT or at worker-claim. Returns the row id, or None (fall back to insert-at-enqueue)."""
    if not SUPABASE_KEY:
        return None
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/voice_requests",
                       headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                "Content-Type": "application/json", "Prefer": "return=representation"},
                       json={"status": "transcribing", "source": "mac", "focus": "hub",
                             "capture_start": cap_start, "capture_end": cap_end}, timeout=6)
        rows = r.json()
        return rows[0]["id"] if isinstance(rows, list) and rows else None
    except Exception as e:  # noqa: BLE001
        log(f"open-request failed: {e}")
        return None


def update_request_pending(rid: str, text: str) -> None:
    """Fill the lifecycle row in with the transcript and hand it to the workers (status 'pending')."""
    try:
        httpx.patch(f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"transcript": text, "status": "pending",
                          "updated_at": datetime.now(timezone.utc).isoformat()}, timeout=10)
        log(f"enqueued: {text[:80]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"enqueue (update) failed: {e}")


def close_request_error(rid: str, err: str) -> None:
    """Close a lifecycle row whose capture never became a runnable command — turns the indicators off."""
    try:
        httpx.patch(f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"status": "error", "error": err,
                          "updated_at": datetime.now(timezone.utc).isoformat()}, timeout=6)
    except Exception:  # noqa: BLE001
        pass


def enqueue(text: str, cap_start: str | None = None, cap_end: str | None = None) -> None:
    if not (SUPABASE_KEY and text):
        return
    try:
        payload = {"transcript": text, "source": "mac", "focus": "hub"}
        if cap_start:
            payload["capture_start"] = cap_start
        if cap_end:
            payload["capture_end"] = cap_end
        httpx.post(f"{SUPABASE_URL}/rest/v1/voice_requests",
                   headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                            "Content-Type": "application/json", "Prefer": "return=minimal"},
                   json=payload, timeout=10.0)
        log(f"enqueued: {text[:80]!r}")
        threading.Thread(target=_esp_working, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log(f"enqueue failed: {e}")


def inject_mode() -> bool:
    """True when the hub's voice_inject_mode is on — route confirmed commands to the focused Mac window
    (paste + Enter) instead of the hub. Read live per command (one GET when a command fires)."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "eq.voice_inject_mode", "select": "value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=5)
        rows = r.json()
        return bool(rows) and str(rows[0].get("value")).strip().lower() not in ("0", "false", "off", "no", "", "none")
    except Exception:  # noqa: BLE001
        return False


def inject_text(text: str) -> None:
    """Inject-mode sink: paste the command into the frontmost Mac window + Enter via Hammerspoon (reuses
    voice.lua's injectVoiceText). No hub, no actions — just hands-free input where the cursor is."""
    try:
        subprocess.Popen([HS_BIN, "-c", f"injectVoiceText([==[{text}]==])"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"injected: {text[:80]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"inject failed: {e}")


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


def backfill(history: np.ndarray, score) -> int:
    """How many samples off the END of `history` (the audio right before a capture's trigger) are still you
    talking. Walks back one BACKFILL_STEP_S at a time, scoring the VAD_WINDOW_S window that ends at each step,
    and stops at the first window that is silence (under 0.4s voiced) or not you. One ECAPA encode per step,
    only at capture-accept. Twin of server.py's _backfill()."""
    step, win, cap = int(BACKFILL_STEP_S * SR), int(VAD_WINDOW_S * SR), int(BACKFILL_MAX_S * SR)
    n, inc = len(history), 0
    while inc + step <= min(n, cap):
        end = n - inc
        seg = trim_voiced(history[max(0, end - win):end], ENERGY_GATE)
        if seg.size < int(0.4 * SR) or score(seg) < BACKFILL_THR:
            break
        inc += step
    return inc


def _load_bank():
    """Prefer the SHARED voiceprint (app_settings.voiceprint) — one bank for every mic, kept current by
    re-enrollment on any device (e.g. the ESP's /enroll-start). Falls back to the local exemplars.npy
    from enroll.py. Read at boot; the listener's supervisor respawns this process, so a restart after
    re-enrollment picks the new bank up."""
    import base64
    import io
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "eq.voiceprint", "select": "value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        rows = r.json()
        if rows and rows[0].get("value"):
            bank = np.load(io.BytesIO(base64.b64decode(rows[0]["value"])))
            log(f"voiceprint: shared bank from app_settings {bank.shape}")
            return bank
    except Exception as e:  # noqa: BLE001
        log(f"voiceprint: shared bank unavailable ({e}); trying local exemplars.npy")
    ref_bank = HERE / "exemplars.npy"
    if ref_bank.exists():
        bank = np.load(ref_bank)
        log(f"voiceprint: local exemplars.npy {bank.shape}")
        return bank
    return None


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY required.", file=sys.stderr); return 2
    bank = _load_bank()
    if bank is None:
        print("FATAL: no voiceprint (app_settings.voiceprint empty and no exemplars.npy) — enroll first.",
              file=sys.stderr); return 2

    global THRESHOLD               # hub UI controls this via app_settings (falls back to the default above)
    THRESHOLD = get_setting("voiceid_threshold", THRESHOLD)

    log("loading ECAPA…")
    enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(HERE / ".model"))
    log(f"wake-free listening — Jared's voice only (thresh={THRESHOLD}, stt=groq:{GROQ_MODEL})")

    def score(seg_f32: np.ndarray) -> float:
        # Peak-normalize before encoding (same as the server + enrollment) so the embedding doesn't
        # depend on mic distance/level and every comparison runs through the identical pipeline.
        peak = float(np.abs(seg_f32).max())
        if peak > 0.01:
            seg_f32 = seg_f32 * (0.9 / peak)
        e = enc.encode_batch(torch.tensor(seg_f32).unsqueeze(0)).squeeze().detach().cpu().numpy()
        e = e / (np.linalg.norm(e) + 1e-9)
        sims = bank @ e
        k = min(TOPK, len(sims))
        return float(np.mean(np.sort(sims)[-k:]))

    # Segment on speech ENERGY (any length), then verify the speaker ONCE on the trimmed utterance.
    frame = int(FRAME_S * SR)
    vad_pool = ThreadPoolExecutor(max_workers=1)   # off-thread speaker checks so audio capture never stalls

    global _last_frame
    seg: list[np.ndarray] = []
    history: deque = deque(maxlen=int(HISTORY_S / FRAME_S))   # rolling raw frames, kept capturing or not
    preroll_n = int(PREROLL_S / FRAME_S)
    history_snap = np.zeros(0, dtype=np.float32)   # audio older than the pre-roll, frozen at each trigger
    seed_s = 0.0               # pre-roll seconds in seg — excluded from the VAD timing below
    sims_seen: list[float] = []
    capturing = False
    t0 = 0.0
    vad_future = None          # in-flight speaker check (one at a time)
    last_check = 0.0           # when the last check was launched
    miss = 0                   # consecutive non-confirming checks since the last confirm
    jared_seen = False         # confirmed Jared at all this utterance?
    last_confirm = None        # wall-clock of the last confirm (send-suppression vs the other mic)
    paused_logged = False
    with _open_input_stream(frame) as stream:
        _last_frame = time.monotonic()
        threading.Thread(target=_watchdog, daemon=True).start()
        threading.Thread(target=_alive_beacon, daemon=True).start()
        while True:
                chunk, _ = stream.read(frame)
                chunk = chunk.flatten()
                _last_frame = time.monotonic()
                if not _resumed.is_set():
                    if capturing:              # drop any partial capture — dictation isn't a command
                        capturing = False
                        seg = []
                        vad_future = None
                        hud("off")
                    if not paused_logged:
                        log("paused (push-to-talk) — muted in software, stream stays open")
                        paused_logged = True
                    history.clear()            # nothing recorded around a dictation may be back-filled later
                    continue
                if paused_logged:
                    paused_logged = False
                    log("resumed — listening")
                now = time.monotonic()
                history.append(chunk)
                voiced = float(np.sqrt(np.mean(chunk ** 2))) >= ENERGY_GATE

                if voiced and not capturing:
                    capturing = True
                    frames = list(history)
                    seg = frames[-(preroll_n + 1):]          # pre-roll + this frame
                    seed_s = (len(seg) - 1) * FRAME_S
                    older = frames[:-(preroll_n + 1)]
                    history_snap = np.concatenate(older) if older else np.zeros(0, dtype=np.float32)
                    sims_seen = []
                    t0 = now
                    t0_wall = datetime.now(timezone.utc)   # wall-clock start of the recording window
                    vad_future, last_check, miss, jared_seen, last_confirm = None, 0.0, 0, False, None
                    hud("listening")
                    warm_tts()      # speech started → warm TTS now so the spoken reply isn't cold-delayed
                elif capturing:
                    seg.append(chunk)
                    audio_s = sum(len(s) for s in seg) / SR - seed_s   # post-trigger audio only
                    # Launch one speaker check at a time on the trailing window, OFF-thread so frame capture
                    # never stalls. The Mac is fast (~0.1s/check), so this stays real-time as you talk.
                    if vad_future is None and audio_s >= VAD_FIRST_CHECK_S and now - last_check >= VAD_CHECK_EVERY_S:
                        last_check = now
                        win = trim_voiced(np.concatenate(seg)[-int(VAD_WINDOW_S * SR):], ENERGY_GATE)
                        if win.size >= int(0.4 * SR):
                            vad_future = vad_pool.submit(score, win)   # enough voice to score
                        elif jared_seen:
                            miss += 1                                  # window went silent → you're not speaking
                    if vad_future is not None and vad_future.done():
                        try:
                            sim = vad_future.result()
                        except Exception:
                            sim = 0.0
                        vad_future = None
                        thr = THRESHOLD - (KEEPALIVE_MARGIN if jared_seen else 0.0)
                        sims_seen.append(sim)
                        if sim >= thr:
                            miss = 0
                            if not jared_seen:
                                jared_seen = True
                                log(f"Jared confirmed (sim={sim:.2f} after {audio_s:.1f}s) — tracking")
                            hud("locked")          # live 'it's you' — you're being heard right now
                            publish_presence(True)  # shared 'Hearing Jared' truth
                            last_confirm = datetime.now(timezone.utc)
                        elif jared_seen:
                            miss += 1
                            log(f"vad sim={sim:.2f}<thr={thr:.2f} miss={miss}/{absent_misses(audio_s)} audio={audio_s:.1f}s")
                    close, reason = False, ""
                    if now - t0 >= MAX_UTTER_S:
                        close, reason = True, "max"
                    elif jared_seen and miss >= absent_misses(audio_s):   # consecutive non-confirms → you've stopped
                        close, reason = True, "absent"
                    elif not jared_seen and audio_s >= VAD_NO_JARED_ABORT_S:
                        close, reason = True, "noise"
                    if close:
                        cap_end_wall = datetime.now(timezone.utc)   # wall-clock end of the recording window
                        if vad_future is not None:
                            vad_future.cancel()
                            vad_future = None
                        raw = np.concatenate(seg)
                        pcm = trim_voiced(raw, ENERGY_GATE)
                        capturing = False
                        seg = []
                        if reason == "noise" or pcm.size < int(SR * MIN_UTTER_S):
                            if reason == "noise":
                                log(f"never confirmed Jared ({audio_s:.1f}s, sims "
                                    f"{'/'.join(f'{x:.2f}' for x in sims_seen) or 'none'}) — skipped")
                            hud("off")
                        elif SENSOR:
                            # Sensor mode: the confirm loop above already stamped heard_jared_at — that IS
                            # the output. No suppression check, no transcription, no enqueue.
                            log(f"sensor: tracked your voice for {audio_s:.1f}s")
                        else:                          # tracked as Jared live → no end-of-utterance re-check
                            # Blocking network work (presence check + Groq + enqueue) pauses stream reads —
                            # tell the watchdog frames are legitimately not arriving until we're back.
                            _expect_frames.clear()
                            try:
                                other = other_heard_jared()  # shared presence: is another mic still on you?
                                suppressed = (other is not None and last_confirm is not None
                                              and (datetime.now(timezone.utc) - other).total_seconds() <= 1.5
                                              and other > last_confirm + timedelta(seconds=0.3))
                                if suppressed:               # ESP clearly heard you later → this is the partial; drop it
                                    log("another mic still has you — suppressing this Mac partial")
                                    hud("off")
                                else:
                                    # Reach back for the start of the sentence the trigger (or a noise-abort
                                    # restart) missed — see backfill().
                                    back = backfill(history_snap, score) if history_snap.size else 0
                                    if back:
                                        raw = np.concatenate([history_snap[-back:], raw])
                                        pcm = trim_voiced(raw, ENERGY_GATE)
                                        t0_wall -= timedelta(seconds=back / SR)
                                        log(f"back-filled {back / SR:.1f}s of your voice from before the trigger")
                                    # CAPTURE ACCEPTED — the command is committed from this instant:
                                    # open the lifecycle row (lamp amber) + blink the ESP NOW, pre-STT.
                                    # Inject mode has no hub lifecycle (paste-to-window), so skip both.
                                    inject = inject_mode()
                                    rid = None
                                    if not inject:
                                        rid = open_request(t0_wall.isoformat(), cap_end_wall.isoformat())
                                        threading.Thread(target=_esp_working, daemon=True).start()
                                    text = transcribe(pcm)
                                    if text:
                                        log(f"Jared → {text!r} (reason={reason})")
                                        if inject:
                                            cue()              # local 'heard you' cue (you're at the computer; no TV round-trip)
                                            inject_text(text)  # paste into the focused Mac window + Enter (no hub)
                                            hud("listening")   # inject mode keeps the mic open → back to the waiting state
                                        elif rid:
                                            update_request_pending(rid, text)   # worker drives HUD/speech
                                        else:
                                            enqueue(text, t0_wall.isoformat(), cap_end_wall.isoformat())  # fallback: insert at enqueue
                                    else:
                                        if rid:
                                            close_request_error(rid, "empty or failed transcription")
                                        hud("off")
                            finally:
                                _last_frame = time.monotonic()
                                _expect_frames.set()


if __name__ == "__main__":
    sys.exit(main())
