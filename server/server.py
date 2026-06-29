"""Voice pipeline backend.

Receives a continuous 16 kHz / 16-bit / mono PCM audio stream from the ESP32 over
a WebSocket, runs openWakeWord on it, and on a wake-word hit captures the following
utterance (ending on trailing silence) and transcribes it with Groq's whisper.

The pipeline STOPS at the transcript. `handle_transcript()` is the handoff seam:
it logs the text and, if SUPABASE_URL/SUPABASE_ANON_KEY are set, enqueues it into the
Supabase `voice_requests` table (INSERT-only via the anon key + RLS) for downstream
consumers to pick up. Wire the rest of the system off that queue.

The WebSocket optionally requires a device token: if VOICE_DEVICE_TOKEN is set,
clients must present it (via `?token=` on the ws path or an X-Device-Token /
Authorization header) or the connection is rejected. If it's unset, all
connections are accepted (so existing firmware keeps working).
"""

import asyncio
import io
import logging
import os
import re
import time
import wave
from datetime import datetime, timezone, timedelta
from collections import deque
from http import HTTPStatus

import httpx
import numpy as np
import websockets
from groq import Groq
from openwakeword.model import Model

# ---- config (env) ----------------------------------------------------------
PORT = int(os.environ.get("PORT", "8080"))
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_rhasspy")            # "Hey Rhasspy" → fast tier (Haiku)
WAKE_MODEL_POWER = os.environ.get("WAKE_MODEL_POWER", "hey_jarvis") # "Hey Jarvis"  → power tier (Opus + ultrathink)
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
GAIN = float(os.environ.get("GAIN", "15"))   # software boost for the quiet 16-bit mic
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
# Supabase enqueue target for transcripts (INSERT-only via anon key + RLS).
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
# Optional device-token auth on the ws handshake; enforced only when set.
VOICE_DEVICE_TOKEN = os.environ.get("VOICE_DEVICE_TOKEN")
# Shared secret the worker uses to ping POST /led-off when the model finishes (turns the ESP LED off).
LED_TOKEN = os.environ.get("LED_TOKEN", "")
# The currently-connected ESP, so the /led-off HTTP handler can reach it. One device → one global ref.
_active_esp = None
# Push-to-talk active anywhere (Hammerspoon dictation) → the ESP ignores ALL audio: the mute is global,
# not Mac-only. Flipped by /capture-off and /capture-on; auto-clears as a safety so a missed unmute
# can't strand the ESP deaf.
_suppressed = False
_suppress_safety = None
# Voice inject mode (hub toggle voice_inject_mode): when ON, the Mac mic is the ONLY listener — speech is
# pasted into the focused Mac window. The ESP must go deaf for it (Mac-only listening), so the server gates
# its own capture off here, read straight from app_settings (survives restarts / a sleeping Mac).
_inject = False
# Wake-free (speaker-ID) mode for the ESP, congruent with the Mac: the global wakefree_enabled toggle is
# bridged here by the Mac listener via /wakefree-on|off. When ON, the ESP captures on speech ENERGY (no
# wake word) and verifies the utterance is Jared's voice (ECAPA + the shared voiceprint) before
# enqueueing. When OFF, wake words. The model + voiceprint load lazily on turn-on (hence the 2 GB box).
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
_wakefree = False
# Wake-free has no wake word to pick the route per-utterance (unlike 'Hey Rhasspy'=fast / 'Hey Jarvis'=
# power), so it runs on ONE configurable route: app_settings.wakefree_route ('fast'=Haiku / 'power'=Opus).
# Loaded with the voiceprint on turn-on and updated live by the Mac listener via /wakefree-route.
_wake_route = "fast"
_enc = None          # ECAPA model (lazy)
_bank = None         # voiceprint: (N, 192) exemplar embeddings from app_settings.voiceprint
_vthresh = 0.30
_vpause = 2.0

# ---- Single-owner LED state ---------------------------------------------------------------------
# The ESP has one LED and it must reflect ONE coherent state, never a race between code paths. Two
# flags drive it with strict precedence: working (the worker is processing a command) > capturing (a
# mic is actively recording) > idle. EVERY LED change goes through _render_led() — nothing calls
# ws.send("LED_*") directly — so the old "solid → off → blink" flicker (the local capture machine and
# the worker's universal pings stomping each other last-writer-wins) cannot happen.
_led_working = False     # set by the worker's /led-blink, cleared by /led-off (universal, any source)
_led_capturing = False   # set while this ESP is recording an utterance
_other_hearing_jared = False  # another mic (Mac) currently hears Jared → keep this ESP's LED solid too
_other_clear_task = None      # timer that clears the above when the other mic goes stale

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1280          # 80 ms @ 16 kHz — openWakeWord's expected chunk
# Endpointing: adaptive to the room's measured noise floor (no hardcoded level).
# The floor is estimated from the ambient audio just before the wake word (TV
# included); speech is whatever rises clearly above it, so it self-calibrates.
AMBIENT_WINDOW_S = 3.0        # rolling window used to estimate the noise floor
FLOOR_MULT = 3.0             # speech must exceed floor * this
FLOOR_MIN_MARGIN = 150       # ...and at least this far above floor (guards floor≈0)
SILENCE_HANGOVER_S = float(os.environ.get("HANGOVER_S", "3.0"))  # trailing silence to end (env-tunable)
MIN_UTTERANCE_S = 0.6        # never endpoint before this (avoids instant cut)
MAX_UTTERANCE_S = float(os.environ.get("MAX_UTTERANCE_S", "600"))  # absolute safety cap (10 min)
LONG_UTTERANCE_WARN_S = 300.0  # warn if a single capture runs this long

# Wake-free speaker-gated endpointing (replaces energy-vs-noise-floor, which a loud TV corrupts — it
# both clips you mid-sentence and holds the capture open for ages). Keep the capture alive on YOUR
# verified presence: re-score the trailing window with ECAPA off the event loop and close only after
# you've been absent (no confirmed-Jared window) for the hangover. Tune from the logged vad/endpoint lines.
VAD_WINDOW_S = 2.0           # trailing audio fed to ECAPA per check
VAD_CHECK_EVERY_S = 0.7      # re-score cadence while capturing
VAD_ABSENT_MISSES = 2       # close only after this many CONSECUTIVE non-confirming checks (event-based, not a
                            # timer that races the cadence and clips you between words)
VAD_FIRST_CHECK_S = 1.8      # wait for this much audio before the first check
VAD_NO_JARED_ABORT_S = 3.5  # never confirmed you within this much audio → it's noise/TV, drop cheap
VAD_KEEPALIVE_MARGIN = 0.16  # hysteresis: once confirmed, only DROP you when the match falls to clearly-
                             # not-you/silence (~0.14), not on the dips your far-field voice grazes (0.17–0.24).
                             # Calibrated from logs: your real sim 0.17–0.34, room/TV noise ≤0.11 — a clean gap.
PREROLL_S = 0.5              # MINIMUM audio kept before the detected onset (guaranteed floor)
# Onset recovery: the energy trigger is deliberately high (it avoids false starts), so for far-field/quiet
# speech it fires a syllable or two IN — clipping your first word. On trigger we walk BACK through a longer
# lookback ring to where speech actually began, using a LOWER onset threshold, bridging brief inter-syllable
# dips and stopping at true pre-speech silence. Bounded by LOOKBACK_S, so a noisy room adds at most that much
# prefix to a capture that's STILL speaker-gated. Recovers the dropped first word without lowering the trigger.
LOOKBACK_S = 2.5            # how far back we'll reach to recover a soft onset
ONSET_FLOOR_MULT = 1.6      # onset threshold = max(floor*this, floor+ONSET_FLOOR_MARGIN); LOWER than the
ONSET_FLOOR_MARGIN = 70     #   trigger's FLOOR_MULT(3)/FLOOR_MIN_MARGIN(150) so the quiet first syllable counts
ONSET_GAP_S = 0.32          # bridge energy dips up to this long while walking back; longer = the true start

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

groq = Groq(api_key=os.environ["GROQ_API_KEY"])
oww = Model(wakeword_models=[WAKE_MODEL, WAKE_MODEL_POWER], inference_framework="onnx")


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


async def transcribe(pcm: bytes) -> str:
    wav = pcm_to_wav(pcm)
    # Groq SDK is sync; run off the event loop.
    def _call():
        return groq.audio.transcriptions.create(
            file=("utterance.wav", wav),
            model=GROQ_MODEL,
            response_format="text",
        )
    result = await asyncio.to_thread(_call)
    return (result if isinstance(result, str) else getattr(result, "text", "")).strip()


async def handle_transcript(text: str, duration_s: float, truncated: bool, tier: str = "",
                            cap_start: str | None = None, cap_end: str | None = None) -> bool:
    """HANDOFF SEAM — the rest of the system hooks in here.

    `duration_s` is the captured length; `truncated` is True if the capture ended
    by hitting MAX_UTTERANCE_S while speech was still active (the hub surfaces this).
    `tier` ('fast'|'power'|'') comes from which wake word fired; it rides the queue row.
    """
    log.info("TRANSCRIPT (%.0fs%s, tier=%s): %r", duration_s, ", TRUNCATED" if truncated else "", tier or "-", text)
    # Strip a leading wake phrase the STT often captures ("Hey Rhasspy, …" / "Hey Jarvis, …") — it's the
    # trigger word, not part of the command. Tolerant of Whisper mishears (raspy/rhaspy/raspberry, jarvis/jarvas; "very").
    text = re.sub(r"^\W*(hey|hi|ok|okay|very)?\W*(rhasspy|rhaspy|raspy|rasbee|rasby|raspberry|rasp|aspie|aspy|asbie|aspen|jarvis|jarvas|jervis)\b[\s,.!:;-]*",
                  "", text or "", count=1, flags=re.IGNORECASE).strip()
    if not (SUPABASE_URL and SUPABASE_ANON_KEY and text):
        return False
    # Best-effort enqueue into the Supabase voice_requests table. The anon key is INSERT-only by RLS,
    # so we send only the two fields and ask for no representation back. Returns whether it enqueued
    # (the caller drives the LED: blink while the worker is working, off otherwise).
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/voice_requests",
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={"transcript": text, "source": "fly", **({"tier": tier} if tier else {}),
                      **({"capture_start": cap_start} if cap_start else {}),
                      **({"capture_end": cap_end} if cap_end else {})},
            )
        return True
    except Exception as e:
        log.warning("supabase enqueue failed: %s", e)
        return False


def _client_token(ws) -> str | None:
    """Pull the device token off the handshake (websockets 15 asyncio API).

    Checks, in order: the `?token=` query param on the ws path, then the
    `X-Device-Token` header, then a `Bearer ...` Authorization header. In
    websockets 15 the server handler gets a ServerConnection whose `.request`
    is the handshake Request — `.path` carries the full request target
    (path + query) and `.headers` is the Headers mapping.
    """
    request = getattr(ws, "request", None)
    path = getattr(request, "path", "") or ""
    if "?" in path:
        from urllib.parse import parse_qs
        qs = parse_qs(path.split("?", 1)[1])
        tok = qs.get("token", [None])[0]
        if tok:
            return tok
    headers = getattr(request, "headers", None)
    if headers is not None:
        tok = headers.get("X-Device-Token")
        if tok:
            return tok
        auth = headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
    return None


async def connection(ws):
    global _active_esp, _led_working, _led_capturing
    peer = getattr(ws, "remote_address", "?")
    # Device-token gate: enforced only when VOICE_DEVICE_TOKEN is set, so existing
    # firmware that doesn't send a token keeps working until it's updated.
    if VOICE_DEVICE_TOKEN:
        if _client_token(ws) != VOICE_DEVICE_TOKEN:
            log.warning("rejected unauthenticated/invalid connection: %s", peer)
            await ws.close(code=1008, reason="invalid device token")
            return
    log.info("ESP32 connected: %s", peer)
    _active_esp = ws
    _led_working = False
    _led_capturing = False
    oww.reset()

    pending = bytearray()           # raw bytes not yet aligned to a frame
    lookback = deque(maxlen=max(1, int(LOOKBACK_S * SAMPLE_RATE / FRAME_SAMPLES)))  # (raw, rms) per pre-capture frame — for the onset walk-back
    ambient = deque(maxlen=max(1, int(AMBIENT_WINDOW_S * SAMPLE_RATE / FRAME_SAMPLES)))
    capturing = False
    utterance = bytearray()
    wake_tier = None                # which wake word fired this utterance ('fast' | 'power')
    threshold = 0.0                 # adaptive speech/silence level, set at wake
    capture_floor = 0.0             # noise floor measured at capture start (for endpoint diagnostics)
    last_voice = 0.0
    started = 0.0
    # Wake-free speaker-gated VAD state (reset at each capture start):
    check_task = None               # in-flight ECAPA check (one at a time, off the event loop)
    last_check = 0.0                # monotonic time the last check was launched
    miss = 0                        # consecutive non-confirming checks since the last confirm
    jared_seen = False              # have we confirmed Jared at all this capture?
    last_confirm = None             # wall-clock of the last confirm (send-suppression vs the other mic)

    async for msg in ws:
        if not isinstance(msg, (bytes, bytearray)):
            # Non-audio control text from the device (e.g. "RSSI -67" signal telemetry). Log it so
            # WiFi signal strength is visible in `fly logs`, then keep going — it isn't audio.
            if isinstance(msg, str) and msg.startswith("RSSI"):
                log.info("device %s %s dBm", peer, msg)
            continue
        pending.extend(msg)

        while len(pending) >= FRAME_SAMPLES * 2:
            raw = bytes(pending[: FRAME_SAMPLES * 2])
            del pending[: FRAME_SAMPLES * 2]
            frame = np.frombuffer(raw, dtype=np.int16)
            if GAIN != 1.0:                 # boost the quiet mic, then re-pack
                frame = np.clip(frame.astype(np.float32) * GAIN, -32768, 32767).astype(np.int16)
                raw = frame.tobytes()

            if _suppressed or _inject:    # push-to-talk mute OR inject mode (Mac-only) → ESP drops everything
                if capturing:
                    log.info("capture ABORTED mid-utterance (%.1fs in) by %s",
                             time.monotonic() - started, "mute" if _suppressed else "inject")
                    if check_task is not None:
                        check_task.cancel()
                        check_task = None
                    capturing = False
                    utterance = bytearray()
                    oww.reset()
                    _led_capturing = False
                    await _render_led(ws)
                continue

            if not capturing:
                cur_rms = rms(frame)
                lookback.append((raw, cur_rms))
                ambient.append(cur_rms)         # track the room's noise floor
                floor = float(np.median(ambient)) if ambient else 0.0
                speech_level = max(floor * FLOOR_MULT, floor + FLOOR_MIN_MARGIN)

                if _wakefree:
                    # No wake word — start on speech energy; the speaker is verified at close. The route
                    # (Haiku/Opus) is the configured wake-free mode, not per-utterance.
                    wake_tier = _wake_route if cur_rms > speech_level else None
                    fast_score = power_score = 0.0
                else:
                    scores = oww.predict(frame)
                    power_score = scores.get(WAKE_MODEL_POWER, 0.0)
                    fast_score = scores.get(WAKE_MODEL, 0.0)
                    # "Hey Jarvis" (power) checked first so it wins ties; "Hey Rhasspy" (fast) otherwise.
                    wake_tier = "power" if power_score >= WAKE_THRESHOLD else ("fast" if fast_score >= WAKE_THRESHOLD else None)
                if wake_tier:
                    threshold = speech_level
                    capture_floor = floor     # remember the room floor that set this threshold (diagnostics)
                    capturing = True          # LED stays as-is until we CONFIRM it's Jared (not raw energy)
                    # Recover the dropped onset: walk back from the trigger frame to where speech actually
                    # began — energy above a lower onset threshold, bridging brief dips — then seed from there.
                    onset_level = max(floor * ONSET_FLOOR_MULT, floor + ONSET_FLOOR_MARGIN)
                    frames = list(lookback)            # oldest → newest; the newest IS the trigger frame
                    gap_budget = max(1, int(ONSET_GAP_S * SAMPLE_RATE / FRAME_SAMPLES))
                    min_keep = max(1, int(PREROLL_S * SAMPLE_RATE / FRAME_SAMPLES))
                    start, gap = len(frames) - 1, 0
                    for i in range(len(frames) - 1, -1, -1):
                        if frames[i][1] >= onset_level:
                            start, gap = i, 0
                        else:
                            gap += 1
                            if gap > gap_budget:
                                break
                    start = max(0, min(start, len(frames) - min_keep))   # never keep LESS than PREROLL_S
                    utterance = bytearray(b"".join(f[0] for f in frames[start:]))
                    lookback.clear()
                    started = time.monotonic()
                    started_wall = datetime.now(timezone.utc)   # wall-clock start of the recording window
                    last_voice = started
                    if check_task is not None:
                        check_task.cancel()
                    check_task, last_check, miss, jared_seen, last_confirm = None, 0.0, 0, False, None
                    if _wakefree:
                        log.info("wake-free: speech — capturing…")
                    else:
                        log.info("wake! tier=%s (fast=%.2f power=%.2f)", wake_tier, fast_score, power_score)
            else:
                utterance.extend(raw)
                now = time.monotonic()
                elapsed = now - started
                hang = _vpause if _wakefree else SILENCE_HANGOVER_S

                # Speaker-gated endpointing for wake-free (when ECAPA + voiceprint are loaded): keep the
                # capture alive on Jared's verified presence, not on energy. Falls back to the energy
                # endpoint for wake-word captures (or before the voiceprint loads).
                speaker_gated = _wakefree and _enc is not None and _bank is not None
                close = False
                reason = ""
                is_noise = False           # True → never confirmed you (drop without enqueue)
                if speaker_gated:
                    audio_s = len(utterance) / 2 / SAMPLE_RATE
                    # Launch one ECAPA check at a time on the trailing window, OFF the event loop so
                    # frame capture keeps flowing while it runs (~0.4s for a 2.5s window on this box).
                    if check_task is None and audio_s >= VAD_FIRST_CHECK_S and now - last_check >= VAD_CHECK_EVERY_S:
                        win = bytes(utterance[-int(VAD_WINDOW_S * SAMPLE_RATE) * 2:])
                        last_check = now
                        check_task = asyncio.create_task(_check_speaker(np.frombuffer(win, dtype=np.int16), now))
                    if check_task is not None and check_task.done():
                        try:
                            sim, _ = check_task.result()
                        except Exception:
                            sim = 0.0
                        check_task = None
                        thr = _vthresh - (VAD_KEEPALIVE_MARGIN if jared_seen else 0.0)
                        if sim >= thr:
                            miss = 0
                            if not jared_seen:
                                jared_seen = True
                                log.info("wake-free: Jared confirmed (sim=%.2f) — tracking", sim)
                                _led_capturing = True        # LED → solid: confirmed it's YOU
                                await _render_led(ws)
                            asyncio.create_task(_publish_presence(True))   # shared 'Hearing Jared' truth
                            last_confirm = datetime.now(timezone.utc)
                        elif jared_seen:
                            miss += 1
                            log.info("wake-free: vad sim=%.2f<thr=%.2f miss=%d", sim, thr, miss)
                    if elapsed >= MAX_UTTERANCE_S:
                        close, reason = True, "max"
                    elif jared_seen and miss >= VAD_ABSENT_MISSES:   # consecutive non-confirms → you've stopped
                        close, reason = True, "absent"
                    elif not jared_seen and audio_s >= VAD_NO_JARED_ABORT_S:  # never you → it's noise
                        close, reason, is_noise = True, "noise-abort", True
                else:
                    if rms(frame) > threshold:
                        last_voice = now
                    if (elapsed >= MIN_UTTERANCE_S and now - last_voice >= hang) or elapsed >= MAX_UTTERANCE_S:
                        close, reason = True, ("max" if elapsed >= MAX_UTTERANCE_S else "silence")

                if not close:
                    continue

                # ---- close the capture ----
                if check_task is not None:
                    check_task.cancel()
                    check_task = None
                end_wall = datetime.now(timezone.utc)   # wall-clock end of the recording window
                dur = len(utterance) / 2 / SAMPLE_RATE
                _arr = np.frombuffer(bytes(utterance), dtype=np.int16)
                log.info("endpoint: dur=%.1fs reason=%s jared_seen=%s floor=%.0f thresh=%.0f utt_rms=%.0f peak=%d",
                         dur, reason, (jared_seen if speaker_gated else "-"), capture_floor, threshold,
                         rms(_arr), int(np.abs(_arr).max()) if _arr.size else 0)
                truncated = reason == "max"
                if truncated:
                    log.warning("utterance hit %ds cap while still active — truncated", int(MAX_UTTERANCE_S))
                elif dur >= LONG_UTTERANCE_WARN_S:
                    log.warning("long utterance: %.0fs", dur)

                # Speaker decision: the gated path already tracked you live (noise-abort = never you →
                # drop). The energy path (wake words) still verifies the whole utterance once, as before.
                if speaker_gated:
                    if is_noise:
                        log.info("wake-free: never confirmed Jared (%.1fs) — dropped", dur)
                        _led_capturing = False
                        await _render_led(ws)
                        capturing = False
                        utterance = bytearray()
                        continue
                    # Send-suppression (shared presence): if the OTHER mic still has you — heard you within
                    # 1.5s and at least as recently as this ESP did — this capture is a PARTIAL. Drop it; that
                    # mic will send the complete one. (Mac is primary: ESP yields ties via the −0.3s margin.)
                    other = await _other_heard_jared()
                    if other is not None and last_confirm is not None:
                        _n = datetime.now(timezone.utc)
                        if (_n - other).total_seconds() <= 1.5 and other >= last_confirm - timedelta(seconds=0.3):
                            log.info("wake-free: another mic still has you — suppressing this ESP partial (%.1fs)", dur)
                            _led_capturing = False
                            await _render_led(ws)
                            capturing = False
                            utterance = bytearray()
                            continue
                elif _wakefree:
                    sim = _verify_jared(_arr)
                    if sim < _vthresh:
                        log.info("wake-free: not Jared (sim=%.2f) — dropped", sim)
                        _led_capturing = False
                        await _render_led(ws)
                        capturing = False
                        utterance = bytearray()
                        continue
                    log.info("wake-free: Jared (sim=%.2f)", sim)

                enqueued = False
                try:
                    text = await transcribe(bytes(utterance))
                    enqueued = await handle_transcript(text, dur, truncated, wake_tier or "",
                                                       started_wall.isoformat(), end_wall.isoformat())
                except Exception as e:
                    log.exception("transcription failed: %s", e)
                if enqueued:
                    # Stay solid (capturing) until the worker's /led-blink takes over (working) and
                    # its /led-off (done) clears everything — so the bulb never dips to off between.
                    asyncio.create_task(_led_safety(ws))  # backstop in case no done-ping arrives
                else:
                    _led_capturing = False                # nothing to do → idle
                    await _render_led(ws)
                capturing = False
                utterance = bytearray()
                oww.reset()

    if _active_esp is ws:
        _active_esp = None
    log.info("ESP32 disconnected: %s", peer)


LED_SAFETY_S = 60.0  # clear the 'working' blink after this long even if the worker never pings


def _led_cmd() -> str:
    # Precedence: HEARING JARED (this ESP confirmed it's you, OR any other mic) wins over working — so it
    # stays SOLID blue while you're talking even if a command is processing in the background. You stop →
    # blink (working). Idle → off. Solid is gated on CONFIRMED you, never on raw sound/energy.
    return "LED_ON" if (_led_capturing or _other_hearing_jared) else ("LED_BLINK" if _led_working else "LED_OFF")


async def _clear_other_hearing_after(delay: float = 1.6) -> None:
    """Drop the 'other mic hears Jared' flag once that mic stops publishing (it goes stale), then repaint."""
    global _other_hearing_jared
    try:
        await asyncio.sleep(delay)
        _other_hearing_jared = False
        if _active_esp is not None:
            await _render_led(_active_esp)
    except asyncio.CancelledError:
        pass


def _on_presence(payload) -> None:
    """Another mic's voice_presence changed → reflect the shared 'Hearing Jared' on this ESP's LED, so it
    stays solid blue while the Mac still has you even when the ESP itself isn't capturing."""
    global _other_hearing_jared, _other_clear_task
    try:
        rec = (payload.get("data") or {}).get("record") or {}
    except AttributeError:
        return
    if rec.get("source") == "esp":
        return                                   # our own row — ignore
    hj = rec.get("heard_jared_at")
    try:
        fresh = bool(hj) and (datetime.now(timezone.utc) - datetime.fromisoformat(hj)).total_seconds() <= 1.5
    except Exception:  # noqa: BLE001
        fresh = False
    if not fresh:
        return
    _other_hearing_jared = True
    if _active_esp is not None:
        asyncio.create_task(_render_led(_active_esp))
    if _other_clear_task:
        _other_clear_task.cancel()
    _other_clear_task = asyncio.create_task(_clear_other_hearing_after())


async def _render_led(ws) -> None:
    """The ONE place the LED is set, from the precedence state machine (working > capturing > idle).
    Routing every change through here is what makes the indicator coherent — no two paths fight."""
    try:
        await ws.send(_led_cmd())
    except Exception:  # noqa: BLE001
        pass


async def _led_safety(ws):
    """Backstop: clear the working blink after a while even if the worker's done-ping never arrives."""
    global _led_working, _led_capturing
    try:
        await asyncio.sleep(LED_SAFETY_S)
        _led_working = False
        _led_capturing = False
        await _render_led(ws)
    except Exception:  # noqa: BLE001
        pass


def _get_settings(keys):
    """Read app_settings values (service key) — the shared voiceprint + wake-free thresholds."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return {}
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": f"in.({','.join(keys)})", "select": "key,value"},
                      headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                      timeout=15)
        return {row["key"]: row["value"] for row in r.json()}
    except Exception as e:  # noqa: BLE001
        log.warning("settings read failed: %s", e)
        return {}


def _load_voiceid():
    """Lazy-load ECAPA + the shared voiceprint + thresholds when wake-free turns on."""
    global _enc, _bank, _vthresh, _vpause, _wake_route
    import base64
    from speechbrain.inference.speaker import EncoderClassifier
    if _enc is None:
        _enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir="/app/.ecapa")
    s = _get_settings(["voiceprint", "voiceid_threshold", "wakefree_pause_s", "wakefree_route"])
    _bank = np.load(io.BytesIO(base64.b64decode(s["voiceprint"]))) if s.get("voiceprint") else None
    _vthresh = float(s.get("voiceid_threshold") or 0.30)
    _vpause = float(s.get("wakefree_pause_s") or 2.0)
    _wake_route = s.get("wakefree_route") if s.get("wakefree_route") in ("power", "custom") else "fast"
    log.info("wake-free loaded: voiceprint=%s thresh=%.2f pause=%.1fs route=%s",
             None if _bank is None else _bank.shape, _vthresh, _vpause, _wake_route)


def _trim_voiced(wav, gate=0.006, margin_s=0.15):
    step = int(0.05 * SAMPLE_RATE)
    v = [i for i in range(0, len(wav) - step, step) if float(np.sqrt(np.mean(wav[i:i + step] ** 2))) >= gate]
    if not v:
        return np.array([], dtype=np.float32)
    m = int(margin_s * SAMPLE_RATE)
    return wav[max(0, v[0] - m): min(len(wav), v[-1] + step + m)]


def _verify_jared(pcm_i16):
    """Top-3 cosine of the trimmed utterance vs the voiceprint. 0 if no model/voiceprint loaded."""
    if _enc is None or _bank is None:
        return 0.0
    import torch
    wav = _trim_voiced(pcm_i16.astype(np.float32) / 32768.0)
    if wav.size < int(0.4 * SAMPLE_RATE):
        return 0.0
    _t = time.monotonic()
    e = _enc.encode_batch(torch.tensor(wav).unsqueeze(0)).squeeze().detach().cpu().numpy()
    log.info("ECAPA encode: %.0fms for %.1fs voiced audio", (time.monotonic() - _t) * 1000, wav.size / SAMPLE_RATE)
    e = e / (np.linalg.norm(e) + 1e-9)
    sims = _bank @ e
    k = min(3, len(sims))
    return float(np.mean(np.sort(sims)[-k:]))


async def _check_speaker(window_i16, win_end):
    """Run the (blocking) ECAPA verify off the event loop so frame capture keeps flowing while it runs.
    Returns (similarity, win_end); win_end is the capture time the window closed at, so the caller can
    timestamp 'last confirmed Jared' precisely rather than at result-collection time."""
    sim = await asyncio.to_thread(_verify_jared, window_i16)
    return sim, win_end


async def _publish_presence(jared: bool) -> None:
    """Upsert this ESP's row in the shared voice_presence — the system-wide 'who is hearing Jared' truth.
    Fire-and-forget on each confirmed check; any mic's fresh heard_jared_at makes the whole system
    'Hearing Jared' (drives every indicator + lets the other mic suppress a partial capture)."""
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    body = {"source": "esp", "updated_at": now_iso, "heard_sound_at": now_iso}
    if jared:
        body["heard_jared_at"] = now_iso
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{SUPABASE_URL}/rest/v1/voice_presence?on_conflict=source",
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json",
                                  "Prefer": "resolution=merge-duplicates,return=minimal"},
                         json=body)
    except Exception:  # noqa: BLE001 — presence is best-effort
        pass


async def _other_heard_jared():
    """Most recent time a mic OTHER than this ESP confirmed Jared (datetime, or None). Used at capture
    close to suppress a partial: if the Mac still has you, the ESP drops its fragment and the Mac sends
    the whole utterance."""
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        return None
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/voice_presence",
                            params={"source": "neq.esp", "select": "heard_jared_at"},
                            headers={"apikey": key, "Authorization": f"Bearer {key}"})
        stamps = [datetime.fromisoformat(row["heard_jared_at"]) for row in r.json() if row.get("heard_jared_at")]
        return max(stamps) if stamps else None
    except Exception:  # noqa: BLE001
        return None


# (The old 90s _suppress_auto_clear was removed — it woke the ESP mid-dictation when a push-to-talk
# session ran longer than 90s. The mute is now gated on the voice_muted DB flag the listener
# sets/clears, so it lasts exactly as long as push-to-talk is held.)


async def process_request(connection, request):
    """Plain HTTP on the same port (websockets 15 asyncio API). The worker POSTs /led-off?token=... when
    the model finishes, so the ESP light goes blink (working) → off. Returning a Response short-circuits
    the handshake; returning None lets the WS upgrade proceed."""
    path = request.path
    if path.startswith("/led-"):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(path)
        tok = parse_qs(u.query).get("token", [None])[0]
        if LED_TOKEN and tok != LED_TOKEN:
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        # The worker drives these for ANY command (universal indicator), not just ESP-sourced ones:
        # /led-blink while working, /led-off when done, /led-on as a solid 'listening' cue. They feed the
        # SAME precedence state machine as local capture, so the bulb is always one coherent state.
        global _led_working, _led_capturing
        if u.path == "/led-blink":
            _led_working = True
            if _active_esp is not None:
                asyncio.create_task(_led_safety(_active_esp))  # backstop: self-clear if no /led-off ever arrives
        elif u.path == "/led-off":           # done → full reset (clears any lingering capture too)
            _led_working = False
            _led_capturing = False
        elif u.path == "/led-on":
            _led_capturing = True
        if _active_esp is not None:
            await _render_led(_active_esp)
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path.startswith("/capture-o"):     # /capture-off | /capture-on — global push-to-talk mute.
        # Instant fast-path; the SOURCE OF TRUTH is the voice_muted DB flag (read live by
        # _settings_listener), which holds the mute exactly as long as push-to-talk is active. No
        # timeout here on purpose — the old 90s auto-clear woke the ESP mid-dictation. If a /capture-on
        # is ever lost, voice_muted (set to 0 on release, and cleared by the listener on boot) un-sticks it.
        global _suppressed
        _suppressed = path.startswith("/capture-off")
        log.info("capture %s (push-to-talk)", "MUTED" if _suppressed else "unmuted")
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path.startswith("/wakefree-o"):    # /wakefree-on | /wakefree-off — global mode, bridged by the Mac listener
        global _wakefree
        on = path.startswith("/wakefree-on")
        if on and not _wakefree:
            _load_voiceid(); _wakefree = True
            log.info("wake-free ON (speaker-ID capture)")
        elif not on and _wakefree:
            _wakefree = False
            log.info("wake-free OFF (wake-word capture)")
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path.startswith("/wakefree-route"):  # live route change, bridged by the Mac listener (?v=fast|power)
        from urllib.parse import urlparse, parse_qs
        global _wake_route
        v = parse_qs(urlparse(path).query).get("v", [None])[0]
        _wake_route = v if v in ("power", "custom") else "fast"
        log.info("wake-free route → %s", _wake_route)
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path in ("/health", "/"):
        return connection.respond(HTTPStatus.OK, "ok\n")
    return None


# ---- Self-managed state ------------------------------------------------------------------------
# Fly is the ALWAYS-ON host for the ESP; the Mac is an intermittent device. So the server reads its
# own state (wake-free, mute, route, voiceprint, thresholds) straight from app_settings — on boot AND
# live over realtime — instead of waiting for the Mac listener to push it. A server restart or a Mac
# that's asleep can no longer strand the ESP in the wrong mode. The Mac's HTTP bridges remain only as
# a redundant fast-path; the database is the source of truth.
def _truthy(v) -> bool:
    return str(v).strip().lower() not in ("0", "false", "off", "no", "", "none")


def _load_voiceid_bg() -> None:
    """Load ECAPA + the voiceprint OFF the event loop, so it never blocks the ESP websocket."""
    try:
        asyncio.get_running_loop().run_in_executor(None, _load_voiceid)
    except RuntimeError:
        _load_voiceid()


def _apply_setting(key, value) -> None:
    """Apply one app_settings change to the server's own state (no Mac bridge required)."""
    global _wakefree, _suppressed, _wake_route, _inject
    if key == "voice_inject_mode":
        _inject = _truthy(value)
        log.info("voice inject mode %s (from app_settings) — ESP %s", "ON" if _inject else "off",
                 "muted" if _inject else "live")
        return
    if key == "wakefree_enabled":
        on = _truthy(value)
        if on and not _wakefree:
            _wakefree = True; _load_voiceid_bg()
            log.info("wake-free ON (from app_settings)")
        elif not on and _wakefree:
            _wakefree = False
            log.info("wake-free OFF (from app_settings)")
    elif key == "voice_muted":
        _suppressed = _truthy(value)
        log.info("global mute %s (from app_settings)", "ON" if _suppressed else "off")
    elif key == "wakefree_route":
        _wake_route = str(value) if str(value) in ("power", "custom") else "fast"
        log.info("wake-free route → %s (from app_settings)", _wake_route)
    elif key in ("voiceid_threshold", "wakefree_pause_s", "voiceprint") and _wakefree:
        _load_voiceid_bg()   # refresh thresholds / voiceprint live


async def _settings_listener() -> None:
    """Restore state from app_settings on boot, then react to hub changes live over realtime — so the
    ESP's mode survives server restarts and hub toggles even with the Mac off."""
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        log.warning("settings listener: no supabase creds; ESP state will rely on the Mac bridge only")
        return
    s = _get_settings(["wakefree_enabled", "voice_muted", "wakefree_route", "voice_inject_mode"])
    for k in ("wakefree_route", "voice_muted", "wakefree_enabled", "voice_inject_mode"):
        if k in s:
            _apply_setting(k, s[k])
    log.info("startup state from DB: wakefree=%s muted=%s route=%s inject=%s", _wakefree, _suppressed, _wake_route, _inject)
    from realtime import AsyncRealtimeClient
    while True:
        sock = None
        try:
            sock = AsyncRealtimeClient(f"{SUPABASE_URL}/realtime/v1", key, auto_reconnect=True)
            await sock.connect()
            ch = sock.channel("jared-voice-settings")

            def _cb(payload):
                try:
                    rec = (payload.get("data") or {}).get("record") or {}
                    _apply_setting(rec.get("key"), rec.get("value"))
                except Exception as e:  # noqa: BLE001
                    log.warning("settings apply failed: %s", e)

            ch.on_postgres_changes("UPDATE", callback=_cb, schema="public", table="app_settings")
            ch.on_postgres_changes("UPDATE", callback=_on_presence, schema="public", table="voice_presence")
            await ch.subscribe()
            log.info("settings listener subscribed (app_settings + voice_presence realtime)")
            while sock.is_connected:
                await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            log.warning("settings listener error: %s; retry in 5s", e)
        try:
            if sock:
                await sock.close()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(5.0)


async def main():
    log.info("voice backend up on :%d  wake=%s  stt=%s", PORT, WAKE_MODEL, GROQ_MODEL)
    asyncio.create_task(_settings_listener())   # Fly owns its own state (app_settings: boot + live)
    # ping_interval=None: the ESP32 is a continuous PCM pump and doesn't reliably answer
    # WebSocket keepalive pings, so server-initiated pings were closing the connection mid-
    # utterance ("keepalive ping timeout"). The constant audio stream keeps the socket live;
    # if the ESP drops, the recv loop ends on its own.
    async with websockets.serve(connection, "0.0.0.0", PORT, max_size=None,
                                ping_interval=None, close_timeout=5,
                                process_request=process_request):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
