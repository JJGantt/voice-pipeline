"""How a failing realtime link is reported: a link failure (DNS, refused, reset — every Mac wake starts
with a few) is a warning until connects have failed for TRANSPORT_RAISE_S straight, then one error for
the streak; any other failure is an error at once; a SUBSCRIBED ends the streak. realtime-py's own
retry and give-up records are re-levelled to WARNING, because connect() re-raises to the waker. A
background task of realtime-py's own that dies unawaited is an occurrence; any other one stays an error."""

import asyncio
import logging
import socket
import sys


def waker(load, monkeypatch):
    load()
    wk = sys.modules["voice_worker.waker"]
    out = {"error": [], "log": [], "now": 1000.0}
    monkeypatch.setattr(wk, "log_error", lambda m: out["error"].append(m))
    monkeypatch.setattr(wk, "log", lambda m: out["log"].append(m))
    monkeypatch.setattr(wk.time, "monotonic", lambda: out["now"])
    monkeypatch.setattr(wk.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda s: None})())
    return wk.RealtimeWaker(), out


def gai():
    return socket.gaierror(8, "nodename nor servname provided, or not known")


def test_link_failure_warns_until_the_streak_is_long_then_errors_once(load, monkeypatch):
    w, out = waker(load, monkeypatch)
    w._report(gai())
    out["now"] += 299
    w._report(gai())
    assert out["error"] == []
    out["now"] += 2
    w._report(gai())
    out["now"] += 60
    w._report(gai())
    assert len(out["error"]) == 1 and "failing for 301s" in out["error"][0]


def test_subscribed_ends_the_streak(load, monkeypatch):
    w, out = waker(load, monkeypatch)
    w._report(gai())
    out["now"] += 200
    w._on_sub("RealtimeSubscribeStates.SUBSCRIBED")
    out["now"] += 200
    w._report(gai())
    assert out["error"] == []


def test_a_failure_that_is_not_the_link_errors_at_once(load, monkeypatch):
    w, out = waker(load, monkeypatch)
    w._report(ValueError("bad channel config"))
    assert len(out["error"]) == 1


def test_realtime_retry_and_give_up_records_are_warnings(load):
    h = load()
    for msg, level in [("Connection attempt failed: [Errno 8] x", logging.WARNING),
                       ("Connection failed permanently after 5 attempts. Error: x", logging.WARNING),
                       ("WebSocket connection closed with code: 1006", logging.WARNING),
                       ("something else broke", logging.ERROR)]:
        r = logging.LogRecord("realtime._async.client", logging.ERROR, "", 0, msg, None, None)
        h.w._demote_realtime_retry(r)
        assert r.levelno == level, msg


class AsyncRealtimeClient:   # named as realtime-py's, so its task's coroutine qualname matches
    async def _heartbeat(self):
        raise ValueError("Set of Tasks/Futures is empty.")


def test_unretrieved_task_failure_is_an_occurrence_only_for_the_realtime_client(load, monkeypatch):
    w, out = waker(load, monkeypatch)
    wk = sys.modules["voice_worker.waker"]
    occ = []
    monkeypatch.setattr(wk, "log_occurrence", lambda m: occ.append(m))

    async def other():
        raise ValueError("boom")

    loop = asyncio.new_event_loop()
    default = []
    monkeypatch.setattr(loop, "default_exception_handler", lambda ctx: default.append(ctx))
    try:
        for coro in (AsyncRealtimeClient()._heartbeat(), other()):
            task = loop.create_task(coro)
            loop.run_until_complete(asyncio.wait([task]))
            w._on_loop_exception(loop, {"message": "Task exception was never retrieved",
                                        "exception": task.exception(), "future": task})
    finally:
        loop.close()
    assert len(occ) == 1 and "AsyncRealtimeClient._heartbeat" in occ[0] and "Set of Tasks" in occ[0]
    assert len(default) == 1 and str(default[0]["exception"]) == "boom"
