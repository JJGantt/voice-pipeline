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
PREROLL_S = 0.5              # audio kept from just before the wake hit

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


async def handle_transcript(text: str, duration_s: float, truncated: bool, tier: str = "") -> bool:
    """HANDOFF SEAM — the rest of the system hooks in here.

    `duration_s` is the captured length; `truncated` is True if the capture ended
    by hitting MAX_UTTERANCE_S while speech was still active (the hub surfaces this).
    `tier` ('fast'|'power'|'') comes from which wake word fired; it rides the queue row.
    """
    log.info("TRANSCRIPT (%.0fs%s, tier=%s): %r", duration_s, ", TRUNCATED" if truncated else "", tier or "-", text)
    # Strip a leading wake phrase the STT often captures ("Hey Rhasspy, …" / "Hey Jarvis, …") — it's the
    # trigger word, not part of the command. Tolerant of Whisper mishears (raspy/rhaspy/raspberry, jarvis/jarvas; "very").
    text = re.sub(r"^\W*(hey|hi|ok|okay|very)?\W*(rhasspy|rhaspy|raspy|rasbee|rasby|raspberry|rasp|jarvis|jarvas|jervis)\b[\s,.!:;-]*",
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
                json={"transcript": text, "source": "fly", **({"tier": tier} if tier else {})},
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
    preroll = bytearray()           # rolling pre-wake audio
    preroll_max = int(PREROLL_S * SAMPLE_RATE) * 2
    ambient = deque(maxlen=max(1, int(AMBIENT_WINDOW_S * SAMPLE_RATE / FRAME_SAMPLES)))
    capturing = False
    utterance = bytearray()
    wake_tier = None                # which wake word fired this utterance ('fast' | 'power')
    threshold = 0.0                 # adaptive speech/silence level, set at wake
    last_voice = 0.0
    started = 0.0

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

            if _suppressed:               # global push-to-talk mute → drop everything, abort any capture
                if capturing:
                    capturing = False
                    utterance = bytearray()
                    oww.reset()
                    _led_capturing = False
                    await _render_led(ws)
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
                    _led_capturing = True     # solid while recording you (precedence: working > this > idle)
                    await _render_led(ws)
                    capturing = True
                    utterance = bytearray(preroll)
                    preroll.clear()
                    started = time.monotonic()
                    last_voice = started
                    if _wakefree:
                        log.info("wake-free: speech — capturing…")
                    else:
                        log.info("wake! tier=%s (fast=%.2f power=%.2f)", wake_tier, fast_score, power_score)
            else:
                utterance.extend(raw)
                now = time.monotonic()
                if rms(frame) > threshold:
                    last_voice = now
                elapsed = now - started
                hang = _vpause if _wakefree else SILENCE_HANGOVER_S
                done = (elapsed >= MIN_UTTERANCE_S and now - last_voice >= hang) or elapsed >= MAX_UTTERANCE_S
                if done:
                    dur = len(utterance) / 2 / SAMPLE_RATE
                    truncated = elapsed >= MAX_UTTERANCE_S and (now - last_voice) < hang
                    if truncated:
                        log.warning("utterance hit %ds cap while still active — truncated", int(MAX_UTTERANCE_S))
                    elif dur >= LONG_UTTERANCE_WARN_S:
                        log.warning("long utterance: %.0fs", dur)
                    if _wakefree:   # verify the speaker on the whole utterance; drop if it isn't Jared
                        sim = _verify_jared(np.frombuffer(bytes(utterance), dtype=np.int16))
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
                        enqueued = await handle_transcript(text, dur, truncated, wake_tier or "")
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
    return "LED_BLINK" if _led_working else ("LED_ON" if _led_capturing else "LED_OFF")


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
    _wake_route = "power" if s.get("wakefree_route") == "power" else "fast"
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
    e = _enc.encode_batch(torch.tensor(wav).unsqueeze(0)).squeeze().detach().cpu().numpy()
    e = e / (np.linalg.norm(e) + 1e-9)
    sims = _bank @ e
    k = min(3, len(sims))
    return float(np.mean(np.sort(sims)[-k:]))


async def _suppress_auto_clear():
    """Safety: un-mute after 90s even if the /capture-on never arrives, so the ESP can't get stuck deaf."""
    global _suppressed
    await asyncio.sleep(90)
    _suppressed = False
    log.info("capture suppress auto-cleared (safety)")


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
        # No token: muting briefly is low-risk and it auto-clears; keeps the secret out of Hammerspoon.
        global _suppressed, _suppress_safety
        on = path.startswith("/capture-off")
        _suppressed = on
        if _suppress_safety:
            _suppress_safety.cancel()
            _suppress_safety = None
        if on:
            _suppress_safety = asyncio.create_task(_suppress_auto_clear())
        log.info("capture %s (push-to-talk)", "MUTED" if on else "unmuted")
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
        _wake_route = "power" if v == "power" else "fast"
        log.info("wake-free route → %s", _wake_route)
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path in ("/health", "/"):
        return connection.respond(HTTPStatus.OK, "ok\n")
    return None


async def main():
    log.info("voice backend up on :%d  wake=%s  stt=%s", PORT, WAKE_MODEL, GROQ_MODEL)
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
