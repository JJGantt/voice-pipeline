#!/usr/bin/env python3
"""Headless voice-command worker daemon: the entry point.

Claims voice commands from the Supabase queue (`voice_requests`), runs each on the Claude subscription
with the MCP tools attached, and writes the conversation back (`voice_messages`). The same code runs on
Fly (WORKER_ID=fly), the Pi and the Mac; WORKER_PRIORITY orders them, and the highest healthy one takes
each request. Exactly one request is processed at a time.

This file is the main loop, signals and the background threads. The work is in `voice_worker/`, one
module per responsibility: `claim` (who takes a request), `process` (provenance, routing, the turn,
the write-back), `runners` (where a turn runs), `presence` (heartbeats, priority, hand-off), `live`
(settings, focus and today's thread off realtime), `waker` (the realtime subscription), and the rest.
docs/ROUTING.md is the routing contract.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

import httpx

import ops_report as ops

from voice_worker import runstate
from voice_worker.config import HEARTBEAT_INTERVAL_S, HOLD_RETRY_S, HTTP_TIMEOUT_S, HUB_TOKEN, IDLE_POLL_DEGRADED_S, IDLE_POLL_S, STANDBY_HEARTBEAT_S, STANDBY_POLL_S, STATE_DIR, SUPABASE_KEY, WORKER_ID, WORKER_PRIORITY
from voice_worker.db import Supabase
from voice_worker.governor import maybe_auto_reenable_wakefree
from voice_worker.logs import log, log_error
from voice_worker.presence import heartbeat
from voice_worker.process import one_iteration
from voice_worker.runners import SDK
from voice_worker.waker import RealtimeWaker


_waker = None   # the RealtimeWaker, so a signal can cut the main loop's wait short


def _handle_signal(signum, frame):  # noqa: ARG001
    """Stop after the current iteration. Says so in the heartbeat at once, so no worker keeps deferring
    to one on its way out, and wakes the main loop so an idle worker exits in a moment instead of
    sleeping out its poll past the unit's stop timeout (which then SIGKILLs it)."""
    runstate._running, runstate._stopping = False, True
    log(f"received signal {signum}; shutting down after current iteration")

    # Sent HERE, before returning: an idle worker exits within a moment of this handler, and a daemon
    # thread sending it was killed with the process first — the Pi then waited out FAILOVER_S on a Fly
    # worker that was already gone.
    try:
        with httpx.Client(timeout=3) as c:
            heartbeat(Supabase(c))
    except Exception:  # noqa: BLE001
        pass
    if _waker is not None:
        _waker.event.set()


# The ops responder's heartbeat (health/responder) says the WORK loop is alive, not just this process.
# It is sent from the heartbeat thread because the main loop legitimately blocks for a whole turn, so it
# goes out only while the main loop has come round within LOOP_STALL_S: longer than any turn should take,
# so a turn is never mistaken for a hang, and a loop wedged past it goes quiet like a dead worker.
LOOP_STALL_S = float(os.environ.get("LOOP_STALL_S", "1800"))


OPS_BEAT_S = 60


_loop_at = time.monotonic()


def _heartbeat_loop(stop_evt: threading.Event) -> None:
    """Heartbeat on its OWN thread + client so the worker stays 'present' even while a long command blocks
    the main loop. Otherwise its presence goes stale mid-task and a lower-priority worker poaches the
    user's follow-up — running it against a different machine/session and losing the conversation context.
    A rapid follow-up is the user tagging an extra thought onto the same thread, so it MUST queue on the
    same worker and resume the same session. Fires immediately, then every HEARTBEAT_INTERVAL_S (or the
    slower STANDBY_HEARTBEAT_S while this worker is standing by)."""
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as hb_client:
        hb_db = Supabase(hb_client)
        while not stop_evt.is_set():
            try:
                heartbeat(hb_db)
                if time.monotonic() - _loop_at < LOOP_STALL_S:
                    ops.beat(max(OPS_BEAT_S, STANDBY_HEARTBEAT_S if runstate._standby else HEARTBEAT_INTERVAL_S),
                             {"standby": bool(runstate._standby)})
                maybe_auto_reenable_wakefree()   # self-throttled to ~5 min; rides the beat, no extra timer
            except Exception as e:  # noqa: BLE001
                log_error(f"heartbeat thread error: {e}")
            # Standing by → beat slowly (but still < HEARTBEAT_FRESH_S so we never look dead).
            stop_evt.wait(STANDBY_HEARTBEAT_S if runstate._standby else HEARTBEAT_INTERVAL_S)


LISTENER_LABEL = "com.jaredgantt.voice-mac-listener"


def _listener_supervisor(stop_evt: threading.Event) -> None:
    """Mac only: revive the wake-word listener whenever its launchd job is dead. launchd's KeepAlive is
    NOT sufficient on this macOS — it parks the respawn of an exited agent ("pended nondemand spawn =
    inefficient"), which left the Mac mic silently dead for 24h after the Jul 3 CoreAudio-wedge exit.
    A demand spawn (launchctl kickstart) always works, so the always-on worker checks the job every 60s
    and kicks it if it has no PID. The listener's own wedge path now re-execs in place (no exit at all);
    this covers everything else: hard crashes, jetsam kills, and login-time RunAtLoad spawns that launchd
    also pends."""
    while not stop_evt.is_set():
        try:
            out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[2] == LISTENER_LABEL and parts[0] == "-":
                    log(f"listener job dead (last status {parts[1]}) — kickstarting")
                    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LISTENER_LABEL}"],
                                   capture_output=True, timeout=10)
        except Exception as e:  # noqa: BLE001
            log_error(f"listener supervisor error: {e}")
        stop_evt.wait(60)


def _demote_realtime_retry(record):
    if record.levelno == logging.ERROR and record.getMessage().startswith(
            ("Connection attempt failed:", "WebSocket connection closed with code:")):
        record.levelno, record.levelname = logging.WARNING, "WARNING"
    return True


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY is required (service_role key). See README.",
              file=sys.stderr)
        return 2
    if not HUB_TOKEN:
        log_error("HUB_TOKEN is not set: tool-call activity will not reach the hub")

    once = "--once" in sys.argv

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    ops.install(machine=WORKER_ID, service="voice-worker")
    # realtime-py logs each failed connect attempt and each socket close at ERROR, then retries by itself;
    # with the socket recycled every 5 min, one slow handshake would open an incident for a reconnect that
    # lands seconds later. Re-level those to WARNING (the reporter forwards ERROR and above). Giving up
    # ("Connection failed permanently…") is not demoted, and RealtimeWaker reports its own failures.
    logging.getLogger("realtime._async.client").addFilter(_demote_realtime_retry)
    log(f"starting (priority={WORKER_PRIORITY}, state_dir={STATE_DIR}, once={once})")

    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        db = Supabase(client)

        if once:
            heartbeat(db)
            processed = one_iteration(db)
            log(f"--once complete (processed={processed})")
            return 0

        global _waker
        waker = _waker = RealtimeWaker()
        waker.start()   # event-driven claim; no-op fallback to polling if realtime is unavailable
        threading.Thread(target=SDK.warm, name="sdk-warm", daemon=True).start()   # first turn starts warm

        # Heartbeat on its own thread so a long command (a multi-minute agent run) never lets this
        # worker's presence go stale — which would let a lower-priority worker poach the user's follow-up
        # onto a different session. The main loop is then free to block on process_request as long as a
        # command takes, and the queued follow-up runs HERE, next, resuming the same conversation.
        hb_stop = threading.Event()
        threading.Thread(target=_heartbeat_loop, args=(hb_stop,), name="heartbeat", daemon=True).start()
        if WORKER_ID == "mac":   # the Mac mic's keeper — see _listener_supervisor
            threading.Thread(target=_listener_supervisor, args=(hb_stop,), name="listener-supervisor", daemon=True).start()

        try:
            global _loop_at
            while runstate._running:
                _loop_at = time.monotonic()
                try:
                    processed = one_iteration(db)
                except Exception as e:  # noqa: BLE001 — a failed claim must not end the worker
                    log_error(f"iteration failed: {e}")
                    processed = False
                # Loop quickly if we just worked (drain the queue); else wait for the next INSERT.
                # waker.wait wakes instantly on a realtime INSERT; the timeout is only the missed-event
                # backstop — slow while the subscription is healthy, tight when realtime is down.
                if not processed:
                    # Cadence, standby FIRST: a standing-by worker polls rarely regardless of realtime
                    # health — it isn't the one claiming, realtime wakes it on real commands, and it ramps
                    # back the instant the primary goes silent (should_defer flips false). Only the ACTIVE
                    # worker tightens to the degraded 2s poll when realtime drops; otherwise healthy backstop.
                    if runstate._holding:
                        wait_s = HOLD_RETRY_S   # the speaker is still talking; look again shortly
                    elif runstate._standby:
                        wait_s = (max(0.5, runstate._failover_at - time.monotonic()) if runstate._failover_at
                                  else STANDBY_POLL_S)
                    elif not waker.healthy:
                        wait_s = IDLE_POLL_DEGRADED_S
                    else:
                        wait_s = IDLE_POLL_S
                    waker.wait(wait_s)
        finally:
            hb_stop.set()

    log("stopped")
    return 0


# The modules of voice_worker, searched in order by attribute access on this module, so `worker.<name>`
# keeps answering for tool-check.py and the tests whichever module a name now lives in.
_MODULES = ("runstate", "config", "prompts", "logs", "db", "outbound", "presence", "transcribe", "live",
            "governor", "runners", "claim", "process", "waker")


def __getattr__(name):
    import importlib
    for m in _MODULES:
        mod = importlib.import_module(f"voice_worker.{m}")
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(name)


if __name__ == "__main__":
    sys.exit(main())
