"""Plain HTTP on the websocket's port: the LED, mute, wake-free and enrollment controls (LED_TOKEN), and
/verify (VERIFY_TOKEN), each token in an X-Token (or Authorization: Bearer) header."""

import asyncio
import io
import logging
import wave
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np

from . import config, db, enroll, esp, led, modes, voiceid
from .auth import http_token, token_ok

log = logging.getLogger("voice")


async def process_request(connection, request):
    """websockets 15 asyncio API. The worker GETs /led-off (X-Token) when the model finishes, so the ESP
    light goes blink (working) → off. Returning a Response short-circuits the handshake; returning None
    lets the WS upgrade proceed."""
    path = request.path
    if path.startswith("/led-"):
        u = urlparse(path)
        if not token_ok(http_token(request), config.LED_TOKEN):
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        # The worker drives these for ANY command (universal indicator), not just ESP-sourced ones:
        # /led-blink while working, /led-off when done, /led-on as a solid 'listening' cue. They feed the
        # SAME precedence state machine as local capture, so the bulb is always one coherent state.
        if u.path == "/led-blink":
            led.state.working = True
            if esp.link.ws is not None:
                led.arm_safety(esp.link.ws)  # backstop: self-clear if no /led-off ever arrives
        elif u.path == "/led-off":           # done → full reset (clears any lingering capture too)
            led.state.working = False
            led.state.capturing = False
            led.cancel_safety()
        elif u.path == "/led-on":
            led.state.capturing = True
        if esp.link.ws is not None:
            await led.render(esp.link.ws, f"http {u.path}")
        else:
            log.info("http %s with no ESP connected", u.path)
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path.startswith("/verify"):
        # SPEAKER ID FOR THE DEVICES THAT UPLOAD A CLIP rather than stream. The Mac and the ESP score
        # every utterance against the voiceprint before it becomes a command; the phone and the watch
        # could not, because the model lives here and they only ever hand the hub a finished file. The
        # hub converts that file to 16k mono WAV, puts it in the voice-clips bucket and asks here.
        #
        # Answers the top-3 cosine as text, and 0 when there is no model or no voiceprint loaded — the
        # hub treats that as "cannot tell", and lets the command through rather than eating it.
        u = urlparse(path)
        q = parse_qs(u.query)
        if not token_ok(http_token(request), config.VERIFY_TOKEN):
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        obj = (q.get("p", [""])[0] or "").strip()
        if not obj or ".." in obj:
            return connection.respond(HTTPStatus.BAD_REQUEST, "p required\n")
        if not voiceid.ready():
            voiceid.load()
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(db.url(f"/storage/v1/object/{obj}"), headers=db.headers())
                r.raise_for_status()
            with wave.open(io.BytesIO(r.content), "rb") as w:   # the hub sends 16k mono, which is what ECAPA is fed
                rate, channels = w.getframerate(), w.getnchannels()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            if channels > 1:
                pcm = pcm[::channels]
            sim = await asyncio.to_thread(voiceid.verify, pcm)
            log.info("verify %s: sim=%.2f (%.1fs @ %dHz)", obj, sim, pcm.size / max(rate, 1), rate)
            return connection.respond(HTTPStatus.OK, f"{sim:.4f}\n")
        except Exception as e:  # noqa: BLE001 — a verification that cannot run must not eat a command
            log.warning("verify %s failed: %s", obj, e)
            return connection.respond(HTTPStatus.OK, "0\n")
    if path.startswith(("/capture-o", "/wakefree-o")):
        if not token_ok(http_token(request), config.LED_TOKEN):
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
    if path.startswith("/capture-o"):     # /capture-off | /capture-on — global push-to-talk mute.
        # Instant fast-path; the SOURCE OF TRUTH is the voice_muted DB flag (read live by settings_sync),
        # which holds the mute exactly as long as push-to-talk is active. No timeout here on purpose: an
        # auto-clear wakes the ESP mid-dictation. If a /capture-on is ever lost, voice_muted (set to 0 on
        # release, and cleared by the listener on boot) un-sticks it.
        modes.state.suppressed = path.startswith("/capture-off")
        log.info("capture %s (push-to-talk)", "MUTED" if modes.state.suppressed else "unmuted")
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path.startswith("/wakefree-o"):    # /wakefree-on | /wakefree-off — global mode, bridged by the Mac listener
        on = path.startswith("/wakefree-on")
        if on and not modes.state.wakefree:
            voiceid.load()
            modes.state.wakefree = True
            log.info("wake-free ON (speaker-ID capture)")
        elif not on and modes.state.wakefree:
            modes.state.wakefree = False
            log.info("wake-free OFF (wake-word capture)")
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path.startswith("/enroll-"):
        # Voiceprint enrollment from the ESP mic (token-gated like /led-*). /enroll-start?n=10 arms it:
        # the next n energy-endpointed utterances become exemplars in the shared bank. /enroll-stop
        # disarms and discards a partial run. /enroll-status reports progress. Arm ONLY in a quiet room —
        # it enrolls whoever talks.
        u = urlparse(path)
        if not token_ok(http_token(request), config.LED_TOKEN):
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        if u.path == "/enroll-start":
            if not modes.state.wakefree:
                return connection.respond(HTTPStatus.CONFLICT, "wake-free mode is off — enable it first\n")
            n = int(parse_qs(u.query).get("n", ["10"])[0])
            enroll.state.left, enroll.state.new = max(1, min(n, 30)), []
            voiceid.load_bg()   # make sure ECAPA + the current bank are loaded
            log.info("enroll: ARMED for %d phrases (speak near the ESP; LED blinks while armed)", enroll.state.left)
        elif u.path == "/enroll-stop":
            log.info("enroll: stopped with %d collected (discarded)", len(enroll.state.new))
            enroll.state.left, enroll.state.new = 0, []
        elif u.path == "/enroll-status":
            body = f'{{"left": {enroll.state.left}, "collected": {len(enroll.state.new)}}}\n'
            return connection.respond(HTTPStatus.OK, body)
        if esp.link.ws is not None:
            await led.render(esp.link.ws, "enroll state")
        return connection.respond(HTTPStatus.OK, "ok\n")
    if path in ("/health", "/"):
        return connection.respond(HTTPStatus.OK, "ok\n")
    return None
