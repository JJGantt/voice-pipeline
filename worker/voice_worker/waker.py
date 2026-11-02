"""Realtime: the voice_requests subscription that wakes the main loop, and the live-state feeds.

A background asyncio loop holds a Supabase realtime subscription and sets a threading.Event the instant
a row becomes claimable, so the main loop claims at once instead of waiting out the idle poll. The poll
stays as the backstop: while the socket is healthy the event fires first; when it drops, the wait times
out at the tighter degraded cadence. Requires voice_requests in the supabase_realtime publication.
"""

import asyncio
import os
import threading
import time

from . import runstate
from .config import FALLBACK_WORKER, IDLE_POLL_DEGRADED_S, SUPABASE_KEY, SUPABASE_URL, WORKER_ID
from .live import live_event, live_load
from .logs import log, log_error
from .transcribe import local_whisper_ready, sweep_untranscribed, transcribe_local


REALTIME_ENABLED = os.environ.get("REALTIME", "1").strip().lower() not in ("0", "false", "no", "off")


class RealtimeWaker:
    def __init__(self):
        self.event = threading.Event()
        self.healthy = False   # True only while the voice_requests subscription is live — drives the poll cadence
        self._loop = None
        self._sock = None
        self._started = False

    def start(self) -> None:
        if self._started or not REALTIME_ENABLED:
            if not REALTIME_ENABLED:
                log("realtime listener disabled by env (REALTIME=0); polling only")
            return
        try:
            import realtime  # noqa: F401 — probe availability before spawning the thread
        except Exception as e:  # noqa: BLE001 — missing lib must not break the worker; just poll
            log(f"realtime listener unavailable ({e}); falling back to {IDLE_POLL_DEGRADED_S}s polling")
            return
        self._started = True
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, name="rt-waker", daemon=True).start()
        log("realtime listener starting (event-driven claim on voice_requests → pending)")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            log(f"realtime listener loop exited: {e}")

    async def _serve(self) -> None:
        from realtime import AsyncRealtimeClient
        # Outer loop rebuilds the client if connect()/subscribe() itself fails (e.g. no network at
        # boot); auto_reconnect handles transient drops once connected. Never raises out — the worker
        # keeps polling regardless.
        while runstate._running:
            try:
                self._sock = AsyncRealtimeClient(f"{SUPABASE_URL}/realtime/v1", SUPABASE_KEY,
                                                 auto_reconnect=True)
                await self._sock.connect()
                ch = self._sock.channel("voice-worker")
                # "*" (not INSERT) — and ONE binding, not two: realtime-py is unreliable with two
                # bindings on the same table (same note in mac-listener/listener.py).
                ch.on_postgres_changes("*", callback=self._on_change,
                                       schema="public", table="voice_requests")
                for t in ("app_settings", "voice_focus", "voice_messages"):   # the live state's feeds
                    ch.on_postgres_changes("*", callback=self._on_live, schema="public", table=t)
                await ch.subscribe(self._on_sub)
                # Recycle the socket every ~5 min even while it still reports is_connected: a HALF-OPEN
                # subscription stays "connected" but silently stops delivering INSERTs, stranding commands on
                # the slow backstop poll (the ~60s pickup lag). Tear down + rebuild on a fixed cadence so
                # events keep flowing — the same guard the Fly server, mac-listener, and litter watcher use.
                started = time.monotonic()
                while runstate._running and self._sock.is_connected and (time.monotonic() - started) < 300.0:
                    await asyncio.sleep(1.0)
                if runstate._running and self._sock.is_connected:
                    log("realtime waker recycling socket (proactive half-open guard)")
            except Exception as e:  # noqa: BLE001
                log_error(f"realtime listener error: {e}; retrying in 5s (poll fallback active)")
            self.healthy = False   # disconnected (or never subscribed) → main loop tightens its poll
            try:
                if self._sock:
                    await self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            if runstate._running:
                await asyncio.sleep(5.0)

    def _on_change(self, payload) -> None:
        """Wake on any voice_requests change that leaves a row CLAIMABLE (status 'pending').

        Binding INSERT alone missed every real command. BOTH capture paths (ESP `server.py` and Mac
        `wakefree.py`) insert a placeholder row as 'transcribing' the instant the capture closes, then
        UPDATE it to 'pending' when the transcript lands. So the INSERT woke the worker with nothing
        yet to claim, and the UPDATE that actually made it claimable fired no event it listened for —
        every command then waited out the full IDLE_POLL_S backstop. Measured: a consistent 14-16s of
        dead air between "transcribed" and "claimed" on three consecutive commands.

        Filtered to 'pending' so the worker's own claimed/done writes can't wake it in a loop.
        """
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        status = str(rec.get("status") or "")
        if status == "pending" or (status == "fallback" and WORKER_ID == FALLBACK_WORKER):
            self.event.set()
        elif status == "untranscribed" and rec.get("audio") and local_whisper_ready():
            threading.Thread(target=transcribe_local, args=(rec,), daemon=True).start()

    def _on_live(self, payload) -> None:
        try:
            d = payload.get("data") or {}
            live_event(str(d.get("table") or ""), str(d.get("type") or "").split(".")[-1].upper(),
                       d.get("record") or {}, d.get("old_record") or {})
        except Exception as e:  # noqa: BLE001 — a bad frame must not take the listener down
            log_error(f"live event failed: {e}")

    def _on_sub(self, state, err=None) -> None:
        self.healthy = "SUBSCRIBED" in str(state).upper()   # any other state (TIMED_OUT/CLOSED/ERROR) → degraded poll
        if self.healthy:   # a fresh subscription missed whatever happened while it was down: reload
            threading.Thread(target=live_load, name="live-load", daemon=True).start()
            threading.Thread(target=sweep_untranscribed, name="untranscribed-sweep", daemon=True).start()
        log(f"realtime subscription: {state}{(' err=' + str(err)) if err else ''}")

    def wait(self, timeout: float) -> bool:
        """Block until a row goes 'pending' (instant) or `timeout` elapses (poll fallback)."""
        fired = self.event.wait(timeout)
        self.event.clear()
        return fired
