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
# ---- Voiceprint enrollment from THIS mic (/enroll-start) -----------------------------------------
# The original exemplars were recorded on the Mac mic; matching ESP far-field audio against them is a
# cross-device comparison that costs similarity. Enroll mode collects exemplars from the ESP itself:
# while armed, each energy-endpointed utterance is embedded (same trim+normalize as verification) and
# appended to the shared bank in app_settings.voiceprint — so both devices' channels are represented
# and top-3 matching picks whichever exemplars fit the capturing mic. Speaker gating is bypassed while
# armed (you can't gate on the voiceprint you're rebuilding). LED: blink = armed, solid flash = phrase
# accepted, off = done. IMPORTANT: only arm this with other voices (TV!) silenced — it enrolls whoever talks.
_enroll_left = 0     # phrases still to collect (0 = enroll mode off)
_enroll_new = []     # embeddings collected this run
# ESP-mic liveness: stamped on every audio message from the device; the alive beacon publishes it to
# voice_presence.alive_at so the hub can show 'ESP mic down' when frames stop (device dead, WiFi out,
# socket wedged) — regardless of whether the websocket object still exists.
_last_esp_frame = 0.0

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
FLOOR_MULT = 1.75            # speech only has to rise modestly above the room floor to START a capture —
                            # ECAPA (the voiceprint gate) is the real discriminator, so the energy trigger
                            # should be lenient. Was 3.0 (needed ~3x over a loud TV to even record); 1.4 was an
                            # overcorrection that fired on nearly all ambient TV noise, keeping ECAPA pinned.
                            # 1.75 = your quieter / far-field voice still triggers, with fewer wasted checks.
FLOOR_MIN_MARGIN = 100       # ...and at least this far above floor (guards floor≈0)
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
VAD_KEEPALIVE_MIN = 0.14     # once confirmed, KEEP the capture alive until the match falls to clearly-not-you
                             # / silence (~0.14, just above the ≤0.11 room/TV noise ceiling). ABSOLUTE and
                             # DECOUPLED from the confirm bar (_vthresh) on purpose: if this tracked _vthresh
                             # with a margin, lowering voiceid_threshold dragged it UNDER the noise floor and
                             # captures held open forever (0.20 threshold made it 0.04 — a capture ran 78s until
                             # push-to-talk aborted it). Keep this fixed regardless of how low the confirm bar goes.
PREROLL_S = 1.2              # audio kept from BEFORE the energy trigger, so a soft first syllable that only
                            # crosses the threshold mid-word isn't clipped. Longer than the trigger latency to
                            # be safe. CRITICAL: the VAD timing below subtracts this (seed_len_s) so the pre-roll
                            # never counts toward the first-check / no-Jared-abort timers — measuring the speaker
                            # window on the INFLATED total is exactly what broke the earlier walk-back version.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

groq = Groq(api_key=os.environ["GROQ_API_KEY"])
oww = Model(wakeword_models=[WAKE_MODEL, WAKE_MODEL_POWER], inference_framework="onnx")


def pcm_to_wav(pcm: bytes) -> bytes:
    # Peak-normalize to -1 dBFS headroom: utterances are stored un-gained (the mic runs quiet, ~-14 dBFS
    # peaks), so bring the level up for Whisper WITHOUT the clipping the old stored-gain caused. Skip
    # near-silence (peak ≤ ~1% FS) — normalizing an empty capture would just amplify room noise.
    arr = np.frombuffer(pcm, dtype=np.int16)
    peak = int(np.abs(arr).max()) if arr.size else 0
    if peak > 327:
        arr = (arr.astype(np.float32) * (0.9 * 32767.0 / peak)).astype(np.int16)
        pcm = arr.tobytes()
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
    try:
        result = await asyncio.to_thread(_call)
    except Exception as e:  # noqa: BLE001 — one retry for transient Groq/network blips
        log.warning("Groq transcription failed (%s) — retrying once", e)
        await asyncio.sleep(0.5)
        result = await asyncio.to_thread(_call)   # a second failure propagates to the caller
    return (result if isinstance(result, str) else getattr(result, "text", "")).strip()


async def _notify_transcribe_failure() -> None:
    """A capture was confirmed as a command but transcription failed even after the retry. Say so in the
    hub conversation — a spoken command silently vanishing is the worst failure mode this pipeline has."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(f"{SUPABASE_URL}/rest/v1/voice_messages",
                         headers={"apikey": SUPABASE_SERVICE_KEY,
                                  "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                                  "Content-Type": "application/json", "Prefer": "return=minimal"},
                         json={"focus": "hub", "mode": "hub", "role": "assistant", "done": True,
                               "text": "⚠️ Heard you, but speech-to-text failed twice — say that again?"})
    except Exception:  # noqa: BLE001 — the log line above is the floor
        pass


async def _open_request(tier: str = "", cap_start: str | None = None, cap_end: str | None = None) -> str | None:
    """Open the command's lifecycle row at CAPTURE-ACCEPT — status 'transcribing', no transcript yet.
    This is the moment the working indicators turn on (ESP blink here, floor lamp via pi-audio's
    realtime sub on this insert): the earliest instant the system has committed to processing the
    command. Returns the row id, or None (no creds / insert failed → caller falls back to the old
    insert-at-enqueue behavior)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return None
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/voice_requests",
                headers={"apikey": SUPABASE_SERVICE_KEY,
                         "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                         "Content-Type": "application/json", "Prefer": "return=representation"},
                json={"status": "transcribing", "source": "fly", "focus": "hub",
                      **({"tier": tier} if tier else {}),
                      **({"capture_start": cap_start} if cap_start else {}),
                      **({"capture_end": cap_end} if cap_end else {})},
            )
            rows = r.json()
            return rows[0]["id"] if isinstance(rows, list) and rows else None
    except Exception as e:  # noqa: BLE001
        log.warning("open-request failed: %s", e)
        return None


async def _close_request_error(rid: str, err: str) -> None:
    """Close a lifecycle row whose capture never became a runnable command (empty/failed STT) — the
    error status is what turns the working indicators back off."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY and rid):
        return
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}",
                headers={"apikey": SUPABASE_SERVICE_KEY,
                         "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                         "Content-Type": "application/json", "Prefer": "return=minimal"},
                json={"status": "error", "error": err,
                      "updated_at": datetime.now(timezone.utc).isoformat()},
            )
    except Exception as e:  # noqa: BLE001
        log.warning("close-request failed: %s", e)


async def handle_transcript(text: str, duration_s: float, truncated: bool, tier: str = "",
                            cap_start: str | None = None, cap_end: str | None = None,
                            rid: str | None = None) -> bool:
    """HANDOFF SEAM — the rest of the system hooks in here.

    `duration_s` is the captured length; `truncated` is True if the capture ended
    by hitting MAX_UTTERANCE_S while speech was still active (the hub surfaces this).
    `tier` ('fast'|'power'|'') comes from which wake word fired; it rides the queue row.
    With `rid` (the lifecycle row opened at capture-accept) the transcript PATCHes that row to
    'pending'; without one it falls back to inserting the row here.
    """
    log.info("TRANSCRIPT (%.0fs%s, tier=%s): %r", duration_s, ", TRUNCATED" if truncated else "", tier or "-", text)
    # Strip a leading wake phrase the STT often captures ("Hey Rhasspy, …" / "Hey Jarvis, …") — it's the
    # trigger word, not part of the command. Tolerant of Whisper mishears (raspy/rhaspy/raspberry, jarvis/jarvas; "very").
    text = re.sub(r"^\W*(hey|hi|ok|okay|very)?\W*(rhasspy|rhaspy|raspy|rasbee|rasby|raspberry|rasp|aspie|aspy|asbie|aspen|jarvis|jarvas|jervis)\b[\s,.!:;-]*",
                  "", text or "", count=1, flags=re.IGNORECASE).strip()
    if not (SUPABASE_URL and SUPABASE_ANON_KEY and text):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            if rid and SUPABASE_SERVICE_KEY:
                now = datetime.now(timezone.utc).isoformat()
                await c.patch(
                    f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}",
                    headers={"apikey": SUPABASE_SERVICE_KEY,
                             "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    # transcribed_at marks STT-done (transcribing→pending) so the created→claimed gap
                    # splits into STT (created→transcribed_at) vs worker pickup (transcribed_at→claimed_at).
                    json={"transcript": text, "status": "pending",
                          "transcribed_at": now, "updated_at": now},
                )
            else:
                # Fallback (no lifecycle row): insert at enqueue, as before. The anon key is INSERT-only by RLS.
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
    global _active_esp, _led_working, _led_capturing, _last_esp_frame
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
    preroll = bytearray()           # rolling pre-wake audio
    preroll_max = int(PREROLL_S * SAMPLE_RATE) * 2
    ambient = deque(maxlen=max(1, int(AMBIENT_WINDOW_S * SAMPLE_RATE / FRAME_SAMPLES)))
    capturing = False
    utterance = bytearray()
    wake_tier = None                # which wake word fired this utterance ('fast' | 'power')
    threshold = 0.0                 # adaptive speech/silence level, set at wake
    capture_floor = 0.0             # noise floor measured at capture start (for endpoint diagnostics)
    last_voice = 0.0
    started = 0.0
    seed_len_s = 0.0             # seconds of pre-roll prepended at trigger; subtracted from the VAD's audio_s
                                # so a longer pre-roll can never shorten the speaker-verification window
    # Wake-free speaker-gated VAD state (reset at each capture start):
    check_task = None               # in-flight ECAPA check (one at a time, off the event loop)
    last_check = 0.0                # monotonic time the last check was launched
    miss = 0                        # consecutive non-confirming checks since the last confirm
    jared_seen = False              # have we confirmed Jared at all this capture?
    mute_flush = False              # mute landed mid-capture on audio worth keeping → close it, don't bin it
    last_confirm = None             # wall-clock of the last confirm (send-suppression vs the other mic)

    async for msg in ws:
        if not isinstance(msg, (bytes, bytearray)):
            # Non-audio control text from the device (e.g. "RSSI -67" signal telemetry). Log it so
            # WiFi signal strength is visible in `fly logs`, then keep going — it isn't audio.
            if isinstance(msg, str) and msg.startswith("RSSI"):
                log.info("device %s %s dBm", peer, msg)
            continue
        pending.extend(msg)
        _last_esp_frame = time.monotonic()   # ESP-mic liveness (feeds the alive beacon)

        while len(pending) >= FRAME_SAMPLES * 2:
            raw = bytes(pending[: FRAME_SAMPLES * 2])
            del pending[: FRAME_SAMPLES * 2]
            frame = np.frombuffer(raw, dtype=np.int16)
            if GAIN != 1.0:
                # The boost feeds DETECTION ONLY (energy floors + wake word) — the buffered audio (raw)
                # stays un-gained. Gaining the stored audio (the old behavior) clipped loud/close speech
                # to the int16 rail (peak=32767), and a clipped waveform distorts ECAPA embeddings so
                # badly that Jared scores as a stranger (sim 0.03-0.16 vs thr 0.30). ECAPA and Whisper
                # are level-robust — they need fidelity, not volume; each normalizes at point of use.
                frame = np.clip(frame.astype(np.float32) * GAIN, -32768, 32767).astype(np.int16)

            if _suppressed or _inject:    # push-to-talk mute OR inject mode (Mac-only) → ESP drops everything
                if capturing:
                    # The mute exists so the ESP doesn't ALSO capture the dictation you're about to
                    # start — that argument covers audio recorded AFTER the mute, and only that.
                    # Whatever was recorded BEFORE it is a separate, already-spoken utterance, so
                    # finalize it through the normal close path instead of binning it. Only a capture
                    # that never earned the right to be sent is still discarded: speaker-gated audio
                    # that never confirmed you (noise/TV), or one too short to transcribe.
                    _sg = _wakefree and _enc is not None and _bank is not None and _enroll_left == 0
                    _dur = len(utterance) / 2 / SAMPLE_RATE
                    if (jared_seen or not _sg) and _dur >= MIN_UTTERANCE_S:
                        log.info("capture FLUSHED at %s (%.1fs, %s) — finalizing, not discarding",
                                 "mute" if _suppressed else "inject", _dur,
                                 "speaker-confirmed" if _sg else "wake-word")
                        mute_flush = True      # consumed by the capture branch below → close, reason='mute-flush'
                    else:
                        log.info("capture ABORTED mid-utterance (%.1fs in) by %s — %s",
                                 _dur, "mute" if _suppressed else "inject",
                                 "never confirmed you" if _sg else "too short")
                        if check_task is not None:
                            check_task.cancel()
                            check_task = None
                        capturing = False
                        utterance = bytearray()
                        oww.reset()
                        _led_capturing = False
                        await _render_led(ws)
                        continue
                else:
                    continue

            if not capturing:
                preroll.extend(raw)
                if len(preroll) > preroll_max:
                    del preroll[: len(preroll) - preroll_max]
                ambient.append(rms(frame))      # track the room's noise floor
                floor = float(np.median(ambient)) if ambient else 0.0
                speech_level = max(floor * FLOOR_MULT, floor + FLOOR_MIN_MARGIN)

                if _wakefree:
                    # No wake word — start on speech energy; the speaker is verified at close. The route
                    # (Haiku/Opus) is the configured wake-free mode, not per-utterance.
                    wake_tier = _wake_route if rms(frame) > speech_level else None
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
                    utterance = bytearray(preroll)
                    seed_len_s = len(utterance) / 2 / SAMPLE_RATE   # pre-roll length — excluded from VAD timing below
                    preroll.clear()
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
                if not mute_flush:
                    utterance.extend(raw)   # post-mute audio is the dictation — never append it
                now = time.monotonic()
                elapsed = now - started
                hang = _vpause if _wakefree else SILENCE_HANGOVER_S

                # Speaker-gated endpointing for wake-free (when ECAPA + voiceprint are loaded): keep the
                # capture alive on Jared's verified presence, not on energy. Falls back to the energy
                # endpoint for wake-word captures (or before the voiceprint loads).
                speaker_gated = _wakefree and _enc is not None and _bank is not None and _enroll_left == 0
                close = False
                reason = ""
                is_noise = False           # True → never confirmed you (drop without enqueue)
                if mute_flush:
                    close, reason, mute_flush = True, "mute-flush", False
                elif speaker_gated:
                    audio_s = len(utterance) / 2 / SAMPLE_RATE - seed_len_s   # POST-trigger audio only — the
                    # pre-roll must NOT count toward VAD_FIRST_CHECK_S / VAD_NO_JARED_ABORT_S (that inflation is
                    # what made the earlier pre-roll abort captures before they could confirm you)
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
                        thr = min(_vthresh, VAD_KEEPALIVE_MIN) if jared_seen else _vthresh
                        # DIAG: log every speaker check (sim vs thr) so a dropped command shows WHY —
                        # too-low sim (bad match / clipping) vs a check that never ran in time (slow encode).
                        log.info("wake-free: check sim=%.2f thr=%.2f seen=%s audio_s=%.1f", sim, thr, jared_seen, audio_s)
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
                _arr = np.frombuffer(bytes(utterance), dtype=np.int16)   # RAW un-gained audio
                # floor/thresh are tracked in the gained (detection) domain — divide by GAIN so every
                # number in this line is in the same raw-mic units. peak=32767 here now means the MIC
                # itself clipped (acoustic overload), not the software gain.
                log.info("endpoint: dur=%.1fs reason=%s jared_seen=%s floor=%.0f thresh=%.0f utt_rms=%.0f peak=%d (raw units)",
                         dur, reason, (jared_seen if speaker_gated else "-"), capture_floor / GAIN, threshold / GAIN,
                         rms(_arr), int(np.abs(_arr).max()) if _arr.size else 0)
                truncated = reason == "max"
                if truncated:
                    log.warning("utterance hit %ds cap while still active — truncated", int(MAX_UTTERANCE_S))
                elif dur >= LONG_UTTERANCE_WARN_S:
                    log.warning("long utterance: %.0fs", dur)

                # Enroll mode: this utterance is a voiceprint phrase, not a command — embed it into the
                # bank and skip verification/transcription entirely.
                if _enroll_left > 0 and _wakefree:
                    await _enroll_sample(_arr, capture_floor / GAIN, ws)
                    _led_capturing = False
                    capturing = False
                    utterance = bytearray()
                    oww.reset()
                    continue

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
                    #
                    # EXCEPT on a mute flush. The mute is GLOBAL — it silenced the other mic in the same
                    # instant (the Mac just logs "paused (push-to-talk)" and bins its buffer), so the
                    # "complete one" this would yield to is never coming and the utterance is lost by both.
                    # Yielding is only correct while the other mic is still live to finish the job.
                    other = None if reason == "mute-flush" else await _other_heard_jared()
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

                # ---- CAPTURE ACCEPTED: the command is committed from this instant. Open its lifecycle
                # row (status 'transcribing' — pi-audio turns the floor lamp amber on this insert) and
                # flip the LED from solid (hearing you) to blink (working on it) NOW, before STT. The
                # indicators answer "did my command go through?", so they start the moment we KNOW
                # we're going to work, not when the worker eventually claims it.
                rid = await _open_request(wake_tier or "", started_wall.isoformat(), end_wall.isoformat())
                _led_capturing = False
                _led_working = True
                await _render_led(ws)
                asyncio.create_task(_led_safety(ws))  # backstop in case no done-ping ever arrives
                enqueued = False
                try:
                    text = await transcribe(bytes(utterance))
                    enqueued = await handle_transcript(text, dur, truncated, wake_tier or "",
                                                       started_wall.isoformat(), end_wall.isoformat(), rid=rid)
                except Exception as e:
                    log.exception("transcription failed: %s", e)
                    asyncio.create_task(_notify_transcribe_failure())
                if not enqueued:
                    # Nothing intelligible / enqueue failed → close the lifecycle row (lamp off) and idle the LED.
                    if rid:
                        asyncio.create_task(_close_request_error(rid, "empty or failed transcription"))
                    _led_working = False
                    _led_capturing = False
                    await _render_led(ws)
                capturing = False
                utterance = bytearray()
                oww.reset()

    if _active_esp is ws:
        _active_esp = None
    log.info("ESP32 disconnected: %s", peer)


LED_SAFETY_S = 60.0  # clear the 'working' blink after this long even if the worker never pings


def _led_cmd() -> str:
    # Precedence: ENROLLING (armed, waiting for phrases) blinks above all else — unambiguous 'the mic is
    # in enroll mode' at the device. Then HEARING JARED (this ESP confirmed it's you, OR any other mic)
    # wins over working — so it stays SOLID blue while you're talking even if a command is processing in
    # the background. You stop → blink (working). Idle → off. Solid is gated on CONFIRMED you.
    if _enroll_left > 0:
        return "LED_BLINK"
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
    import torch
    # On this shared 1-vCPU machine, torch's default thread pool oversubscribes the single core it
    # actually gets scheduled on, so a single ECAPA encode thrashes and balloons from ~0.2s to 7-10s.
    # That blows past the 3.5s no-Jared abort window, so real commands drop as noise before ECAPA can
    # confirm you. Pin to one thread — a single small inference is FASTER single-threaded here (no
    # coordination overhead), and the latency stays stable inside the confirm window.
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)   # can only be set once, before any inter-op work starts
    except RuntimeError:
        pass
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


def _embed(pcm_i16, min_voiced_s: float = 0.4):
    """Trim → peak-normalize → ECAPA-embed an utterance. Returns a unit-norm (192,) embedding, or None
    if the model isn't loaded / not enough voiced audio. Shared by verification AND enrollment so both
    sides of every comparison go through the identical pipeline."""
    if _enc is None:
        return None
    import torch
    wav = _trim_voiced(pcm_i16.astype(np.float32) / 32768.0)
    if wav.size < int(min_voiced_s * SAMPLE_RATE):
        return None
    # Peak-normalize before encoding: the embedding then doesn't depend on mic distance / device gain,
    # so ESP far-field checks land on the same scale as the enrolled exemplars. Skip near-silence
    # (peak ≤ 0.01 ≈ -40 dBFS) — boosting pure room noise 90x would just embed the noise.
    peak = float(np.abs(wav).max())
    if peak > 0.01:
        wav = wav * (0.9 / peak)
    _t = time.monotonic()
    e = _enc.encode_batch(torch.tensor(wav).unsqueeze(0)).squeeze().detach().cpu().numpy()
    log.info("ECAPA encode: %.0fms for %.1fs voiced audio", (time.monotonic() - _t) * 1000, wav.size / SAMPLE_RATE)
    return e / (np.linalg.norm(e) + 1e-9)


def _verify_jared(pcm_i16):
    """Top-3 cosine of the trimmed utterance vs the voiceprint. 0 if no model/voiceprint loaded."""
    if _bank is None:
        return 0.0
    e = _embed(pcm_i16)
    if e is None:
        return 0.0
    sims = _bank @ e
    k = min(3, len(sims))
    return float(np.mean(np.sort(sims)[-k:]))


def _upload_voiceprint(bank) -> bool:
    """Write the merged exemplar bank back to app_settings.voiceprint — the shared source of truth every
    mic loads from."""
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        return False
    import base64
    buf = io.BytesIO()
    np.save(buf, bank.astype(np.float32))
    try:
        r = httpx.patch(f"{SUPABASE_URL}/rest/v1/app_settings?key=eq.voiceprint",
                        headers={"apikey": key, "Authorization": f"Bearer {key}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"},
                        json={"value": base64.b64encode(buf.getvalue()).decode(),
                              "updated_at": datetime.now(timezone.utc).isoformat()},
                        timeout=10)
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.exception("voiceprint upload failed: %s", e)
        return False


async def _enroll_sample(pcm_i16, floor_raw: float, ws) -> None:
    """Handle one energy-endpointed utterance while enroll mode is armed: embed it, add it to the bank
    when the last phrase lands, and flash the LED so Jared gets per-phrase confirmation at the device."""
    global _enroll_left, _enroll_new, _bank
    utt_rms = rms(pcm_i16)
    if utt_rms < max(floor_raw * 2.0, 200.0):
        log.info("enroll: sample too quiet (rms=%.0f, floor=%.0f) — speak closer; not counted", utt_rms, floor_raw)
        return
    e = await asyncio.to_thread(_embed, pcm_i16, 1.0)   # ≥1s of voiced audio for a stable exemplar
    if e is None:
        log.info("enroll: not enough voiced audio — not counted")
        return
    if _bank is not None:
        sims = _bank @ e
        log.info("enroll: phrase accepted (sim vs old bank: %.2f)", float(np.sort(sims)[-min(3, len(sims)):].mean()))
    else:
        log.info("enroll: phrase accepted")
    _enroll_new.append(e)
    _enroll_left -= 1
    if ws is not None:      # solid flash = 'got it', then back to the armed blink
        try:
            await ws.send("LED_ON")
            await asyncio.sleep(0.4)
            await _render_led(ws)
        except Exception:  # noqa: BLE001
            pass
    if _enroll_left > 0:
        log.info("enroll: %d phrase(s) to go", _enroll_left)
        return
    new = np.vstack(_enroll_new).astype(np.float32)
    prev_shape = None if _bank is None else _bank.shape
    merged = new if _bank is None else np.vstack([_bank, new]).astype(np.float32)
    _bank = merged
    if _upload_voiceprint(merged):
        log.info("enroll: DONE — bank now %s (was %s; +%d from this mic), uploaded to app_settings",
                 merged.shape, prev_shape, new.shape[0])
    else:
        log.warning("enroll: upload FAILED — new exemplars kept in memory only for this process")
    _enroll_new = []
    if ws is not None:
        await _render_led(ws)


async def _check_speaker(window_i16, win_end):
    """Run the (blocking) ECAPA verify off the event loop so frame capture keeps flowing while it runs.
    Returns (similarity, win_end); win_end is the capture time the window closed at, so the caller can
    timestamp 'last confirmed Jared' precisely rather than at result-collection time."""
    sim = await asyncio.to_thread(_verify_jared, window_i16)
    return sim, win_end


async def _alive_beacon() -> None:
    """Mic-liveness beacon: stamp voice_presence.alive_at (source 'esp') every 30s WHILE audio frames
    are arriving from the device. Gated on _last_esp_frame — real audio flow, not process/socket
    existence — so a stale beacon always means the ESP mic is genuinely down."""
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        return
    while True:
        if _last_esp_frame and time.monotonic() - _last_esp_frame < 10:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.post(f"{SUPABASE_URL}/rest/v1/voice_presence?on_conflict=source",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json",
                                          "Prefer": "resolution=merge-duplicates,return=minimal"},
                                 json={"source": "esp",
                                       "alive_at": datetime.now(timezone.utc).isoformat()})
            except Exception:  # noqa: BLE001 — best-effort; a missed beat reads as one late beacon
                pass
        await asyncio.sleep(30)


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
    if path.startswith("/enroll-"):
        # Voiceprint enrollment from the ESP mic (token-gated like /led-*). /enroll-start?n=10 arms it:
        # the next n energy-endpointed utterances become exemplars in the shared bank. /enroll-stop
        # disarms and discards a partial run. /enroll-status reports progress. Arm ONLY in a quiet room —
        # it enrolls whoever talks.
        from urllib.parse import urlparse, parse_qs
        global _enroll_left, _enroll_new
        u = urlparse(path)
        tok = parse_qs(u.query).get("token", [None])[0]
        if LED_TOKEN and tok != LED_TOKEN:
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        if u.path == "/enroll-start":
            if not _wakefree:
                return connection.respond(HTTPStatus.CONFLICT, "wake-free mode is off — enable it first\n")
            n = int(parse_qs(u.query).get("n", ["10"])[0])
            _enroll_left, _enroll_new = max(1, min(n, 30)), []
            _load_voiceid_bg()   # make sure ECAPA + the current bank are loaded
            log.info("enroll: ARMED for %d phrases (speak near the ESP; LED blinks while armed)", _enroll_left)
        elif u.path == "/enroll-stop":
            log.info("enroll: stopped with %d collected (discarded)", len(_enroll_new))
            _enroll_left, _enroll_new = 0, []
        elif u.path == "/enroll-status":
            body = f'{{"left": {_enroll_left}, "collected": {len(_enroll_new)}}}\n'
            return connection.respond(HTTPStatus.OK, body)
        if _active_esp is not None:
            await _render_led(_active_esp)
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


# Durable settings-realtime: a Supabase realtime socket can go HALF-OPEN (is_connected stays True while no
# events flow), which silently stranded the ESP mute for days. So the listener never trusts the socket alone
# — it re-reads the flags on a short poll as a backstop AND proactively recycles the socket on a timer.
SETTINGS_POLL_S = 60.0          # backstop re-read of the flags (catches a change realtime silently dropped);
                                # realtime applies changes instantly via _cb, so this is only the rare-miss
                                # net, and SETTINGS_SOCKET_MAX_S recycles a half-open socket independently
SETTINGS_SOCKET_MAX_S = 300.0   # recycle the realtime socket this often so a half-open one can't stay deaf


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

            def _on_request(payload) -> None:
                # Authoritative LED 'working' state from the command lifecycle itself: in flight
                # (transcribing / pending / claimed) → blink; done/error → off. The HTTP pings
                # (capturers at capture-accept, worker at claim/done) remain as the fast edge; this
                # keeps the LED truthful even when a ping is lost or a capture dies mid-transcription
                # — same source of truth as the floor lamp (pi-audio watches these very rows).
                global _led_working, _led_capturing
                try:
                    rec = (payload.get("data") or {}).get("record") or {}
                except AttributeError:
                    return
                status = str(rec.get("status") or "")
                if status in ("transcribing", "pending", "claimed"):
                    _led_working = True
                elif status in ("done", "error"):
                    _led_working = False
                    _led_capturing = False
                else:
                    return
                if _active_esp is not None:
                    asyncio.create_task(_render_led(_active_esp))

            ch.on_postgres_changes("UPDATE", callback=_cb, schema="public", table="app_settings")
            ch.on_postgres_changes("UPDATE", callback=_on_presence, schema="public", table="voice_presence")
            ch.on_postgres_changes("INSERT", callback=_on_request, schema="public", table="voice_requests")
            ch.on_postgres_changes("UPDATE", callback=_on_request, schema="public", table="voice_requests")
            await ch.subscribe()
            log.info("settings listener subscribed (app_settings + voice_presence + voice_requests realtime)")
            started = time.monotonic()
            while sock.is_connected and (time.monotonic() - started) < SETTINGS_SOCKET_MAX_S:
                await asyncio.sleep(SETTINGS_POLL_S)
                # Backstop: re-read the flags and apply anything that drifted from our in-memory state —
                # catches (and logs) a change realtime silently dropped, so a half-open socket can't leave
                # the ESP mute / inject / wake-free stuck on a stale value.
                try:
                    s = _get_settings(["wakefree_enabled", "voice_muted", "wakefree_route", "voice_inject_mode"])
                    for k, cur in (("voice_muted", _suppressed), ("voice_inject_mode", _inject),
                                   ("wakefree_enabled", _wakefree)):
                        if k in s and _truthy(s[k]) != cur:
                            log.warning("settings backstop: realtime missed %s → applying from poll", k)
                            _apply_setting(k, s[k])
                    if "wakefree_route" in s and str(s["wakefree_route"]) != _wake_route:
                        _apply_setting("wakefree_route", s["wakefree_route"])
                except Exception as e:  # noqa: BLE001
                    log.warning("settings backstop poll failed: %s", e)
            log.info("settings listener recycling realtime socket (connected=%s)", sock.is_connected)
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
    asyncio.create_task(_alive_beacon())        # ESP-mic liveness → voice_presence.alive_at
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
