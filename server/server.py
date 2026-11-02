"""Voice pipeline backend (Fly app jared-voice): the entry point.

Receives a continuous 16 kHz / 16-bit / mono PCM audio stream from the ESP32 over a WebSocket, runs
openWakeWord on it (or, in wake-free mode, speech energy plus speaker ID), captures the utterance and
transcribes it with Groq's Whisper. The pipeline stops at the transcript: `lifecycle.handle_transcript()`
is the handoff seam, enqueueing it into Supabase `voice_requests` for the workers.

Every HTTP control route (/led-*, /enroll-*, /capture-*, /wakefree-*) requires LED_TOKEN and /verify
requires VERIFY_TOKEN, in an X-Token (or Authorization: Bearer) header; the WebSocket requires
VOICE_DEVICE_TOKEN in an X-Device-Token / Authorization header. The server refuses to start without
each of them.

The parts live in the `voice_server` package; README § Layout maps them.
"""

import asyncio
import logging

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

# Errors (log.error and anything uncaught) go to the ops responder (health/responder) through a copy of
# its reporter (edit it there). A warning is for a failure a retry or fallback is already handling.
import ops_report as ops  # noqa: E402
ops.install(machine="fly", service="voice-server")

from voice_server import config, esp, presence, settings_sync  # noqa: E402
from voice_server.routes import process_request  # noqa: E402
from voice_server.session import connection  # noqa: E402
from voice_server.tasks import spawn  # noqa: E402


def _demote(logger_name, test):
    """Re-level a library's ERROR record to WARNING when `test(record)` says it is a recovered event, so it
    stays in `fly logs` but does not open an ops incident (the reporter forwards ERROR and above)."""
    def f(record):
        if record.levelno == logging.ERROR and test(record):
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True
    logging.getLogger(logger_name).addFilter(f)


_demote("websockets.server", esp.handshake_drop)
# realtime-py logs each failed connect attempt and each socket close at ERROR and then retries / reconnects
# by itself. Giving up still arrives as an ERROR ("Connection failed permanently…"), which is not demoted.
_demote("realtime._async.client", lambda r: r.getMessage().startswith(
    ("Connection attempt failed:", "WebSocket connection closed with code:")))


async def _ops_beat():
    """The server is event-driven, so the event loop is its work loop: this beat stops when the loop is
    blocked or the process is gone (health/responder)."""
    while True:
        ops.beat(60)
        await asyncio.sleep(60)


async def main():
    missing = [n for n in ("LED_TOKEN", "VERIFY_TOKEN", "VOICE_DEVICE_TOKEN", "SUPABASE_SERVICE_KEY")
               if not getattr(config, n)]
    if missing:
        log.error("refusing to start: %s not set", ", ".join(missing))
        raise SystemExit(2)
    log.info("voice backend up on :%d  wake=%s  stt=%s", config.PORT, config.WAKE_MODEL, config.GROQ_MODEL)
    spawn(_ops_beat())
    spawn(settings_sync.listen())   # Fly owns its own state (app_settings: boot + live)
    spawn(presence.alive_beacon())  # ESP-mic liveness → voice_presence.alive_at
    esp.gone()                      # nothing is connected yet; reported if none arrives
    # ping_interval=None: the ESP32 is a continuous PCM pump and doesn't reliably answer
    # WebSocket keepalive pings, so server-initiated pings were closing the connection mid-
    # utterance ("keepalive ping timeout"). The constant audio stream keeps the socket live;
    # if the ESP drops, the recv loop ends on its own.
    async with websockets.serve(connection, "0.0.0.0", config.PORT, max_size=None,
                                ping_interval=None, close_timeout=5,
                                process_request=process_request):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:   # Fly stops the machine with SIGINT on every deploy/restart: a clean exit
        log.info("stopped (SIGINT)")
