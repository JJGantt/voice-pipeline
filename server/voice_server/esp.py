"""The ESP32's link: which socket is the live device, when its audio last arrived, and reporting a link
that stays down or keeps dropping.

The ESP's WiFi link is poor: it drops every few minutes and is back within seconds, and that is its
normal behaviour, not a fault. Two things are reported instead. Being OFFLINE for ESP_OFFLINE_REPORT_S
straight (unplugged, WiFi gone, firmware wedged) — offline_watch. And FLAPPING so often that it is
mostly down even though each drop recovers: more than ESP_DROP_LIMIT drops in ESP_DROP_WINDOW_S, about
one every 20s, against a normal rate of a few per 10 min."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

log = logging.getLogger("voice")

ESP_OFFLINE_REPORT_S = 600.0
ESP_DROP_WINDOW_S = 600.0
ESP_DROP_LIMIT = 30               # more drops than this inside the window opens an incident
# One loss of the link ends the open socket AND fails the reconnect handshakes already in flight, all
# inside the same second or two; counted separately, a single blip reads as three drops.
ESP_DROP_COALESCE_S = 5.0


@dataclass
class Link:
    # The currently-connected ESP, so the HTTP routes can reach it. One device → one ref.
    ws: object = None
    # ESP-mic liveness: stamped on every audio message from the device; the alive beacon publishes it to
    # voice_presence.alive_at so the hub can show 'ESP mic down' when frames stop (device dead, WiFi out,
    # socket wedged) — regardless of whether the websocket object still exists.
    last_frame: float = 0.0
    drops: deque = field(default_factory=deque)
    drops_reported: bool = False  # one report per run of drops; re-armed once the window is clear
    offline_task: asyncio.Task | None = None  # the pending offline report, armed while no ESP32 is connected


link = Link()


def note_drop(kind):
    now = time.monotonic()
    if link.drops and now - link.drops[-1] < ESP_DROP_COALESCE_S:
        return
    link.drops.append(now)
    while link.drops and now - link.drops[0] > ESP_DROP_WINDOW_S:
        link.drops.popleft()
    if len(link.drops) == 1:
        link.drops_reported = False
    if len(link.drops) > ESP_DROP_LIMIT and not link.drops_reported:
        link.drops_reported = True
        log.error("ESP32 keeps dropping: %d drops in %d min (latest: %s)",
                  len(link.drops), int(ESP_DROP_WINDOW_S // 60), kind)


def gone():
    """No ESP32 connected: arm the one-shot offline report, unless it is already armed."""
    if link.offline_task is None or link.offline_task.done():
        link.offline_task = asyncio.create_task(offline_watch())


def back():
    if link.offline_task is not None and not link.offline_task.done():
        link.offline_task.cancel()


async def offline_watch():
    await asyncio.sleep(ESP_OFFLINE_REPORT_S)
    if link.ws is None:
        log.error("ESP32 offline for %d min", int(ESP_OFFLINE_REPORT_S // 60))


def handshake_drop(r):
    """The ESP32 vanishing mid-handshake (WiFi blip, reboot): websockets logs it at ERROR, then the ESP
    reconnects. Counted as a drop, and demoted (server.py)."""
    if r.getMessage() == "opening handshake failed" and r.exc_info and isinstance(
            r.exc_info[1], (websockets.exceptions.ConnectionClosed, EOFError, OSError)):
        note_drop("mid-handshake")
        return True
    return False


async def device_frames(ws, peer):
    """The device's messages, ending quietly when it drops off without a close frame.

    The ESP32 losing WiFi or rebooting ends the TCP stream with no close handshake, which websockets
    raises as ConnectionClosedError. That is an ordinary disconnect (the ESP reconnects within a
    second), not a fault: left uncaught it escapes the handler, websockets logs it at ERROR (an ops
    incident), and the handler's own cleanup after the loop never runs. A run of drops is reported
    (note_drop).
    """
    try:
        async for msg in ws:
            yield msg
    except websockets.exceptions.ConnectionClosedError as e:
        log.info("ESP32 dropped without a close frame: %s (%s)", peer, e)
        note_drop("no close frame")
