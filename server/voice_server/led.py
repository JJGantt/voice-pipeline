"""The ESP's one LED, owned here.

The LED must reflect ONE coherent state, never a race between code paths. Two flags drive it with strict
precedence: working (the worker is processing a command) > capturing (a mic is actively recording) >
idle. EVERY LED change goes through render() — nothing calls ws.send("LED_*") directly — so the local
capture machine and the worker's universal pings cannot stomp each other last-writer-wins."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from . import enroll, esp
from .tasks import spawn

log = logging.getLogger("voice")

LED_SAFETY_S = 180.0  # clear the 'working' blink after this long if no done ever arrives. Longer than any
                      # command actually runs (Opus turns reach ~70s), because the lifecycle row is the real
                      # off-switch — this only catches a lost ping AND a lost realtime event together.
PRESENCE_FRESH_S = 3.0  # a heard_jared_at younger than this (other-mic clock + realtime hop) counts as "now"


@dataclass
class Led:
    working: bool = False          # set by the worker's /led-blink, cleared by /led-off (universal, any source)
    capturing: bool = False        # set while this ESP is recording an utterance
    other_hearing: bool = False    # another mic (Mac) currently hears the speaker → keep this ESP's LED solid too
    other_clear_task: asyncio.Task | None = None  # timer that clears the above when the other mic goes stale
    safety_task: asyncio.Task | None = None       # the ONE working-blink backstop timer (re-armed, never stacked)
    last_sent: str | None = None   # last command actually sent to the device — the log records only changes


state = Led()


def cmd() -> str:
    # Precedence: ENROLLING (armed, waiting for phrases) blinks above all else — unambiguous 'the mic is
    # in enroll mode' at the device. Then HEARING THE SPEAKER (this ESP confirmed it's you, OR any other mic)
    # wins over working — so it stays SOLID blue while you're talking even if a command is processing in
    # the background. You stop → blink (working). Idle → off. Solid is gated on CONFIRMED you.
    if enroll.state.left > 0:
        return "LED_BLINK"
    return "LED_ON" if (state.capturing or state.other_hearing) else ("LED_BLINK" if state.working else "LED_OFF")


async def render(ws, why: str = "") -> None:
    """The ONE place the LED is set, from the precedence state machine (working > capturing > idle).
    Every CHANGE of the device's state is logged with the reason, and a failed send is logged rather than
    swallowed: an indicator that fails silently is indistinguishable from one that was never asked."""
    c = cmd()
    try:
        await ws.send(c)
    except Exception as e:  # noqa: BLE001
        log.warning("LED %s NOT sent (%s): %s", c, why, e)
        return
    if c != state.last_sent:
        log.info("LED %s (%s)", c, why)
        state.last_sent = c


def render_active(why: str) -> None:
    """Repaint the connected ESP, if any, without waiting (for sync callers: realtime handlers)."""
    if esp.link.ws is not None:
        spawn(render(esp.link.ws, why))


def arm_safety(ws) -> None:
    """Start the ONE working-blink backstop, replacing any running one. Stacking was the old shape: every
    blink ping started its own timer, so command A's timer switched the light off while command B was
    still running."""
    if state.safety_task is not None:
        state.safety_task.cancel()
    state.safety_task = asyncio.create_task(_safety(ws))


def cancel_safety() -> None:
    if state.safety_task is not None:
        state.safety_task.cancel()
        state.safety_task = None


async def _safety(ws):
    """Backstop: clear the working blink if neither a done-ping nor the lifecycle row's done ever arrives."""
    try:
        await asyncio.sleep(LED_SAFETY_S)
        state.working = False
        state.capturing = False
        await render(ws, "safety timeout — no done arrived")
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        pass


async def _clear_other_hearing_after(delay: float = 1.6) -> None:
    """Drop the 'other mic hears the speaker' flag once that mic stops publishing (it goes stale), then repaint."""
    try:
        await asyncio.sleep(delay)
        state.other_hearing = False
        if esp.link.ws is not None:
            await render(esp.link.ws, "other mic went quiet")
    except asyncio.CancelledError:
        pass


def on_presence(payload) -> None:
    """Another mic's voice_presence changed → reflect the shared 'Hearing the speaker' on this ESP's LED, so it
    stays solid blue while the Mac still has you even when the ESP itself isn't capturing."""
    try:
        rec = (payload.get("data") or {}).get("record") or {}
    except AttributeError:
        return
    if rec.get("source") == "esp":
        return                                   # our own row — ignore
    hj = rec.get("heard_jared_at")
    try:
        fresh = bool(hj) and (datetime.now(timezone.utc) - datetime.fromisoformat(hj)).total_seconds() <= PRESENCE_FRESH_S
    except Exception:  # noqa: BLE001
        fresh = False
    if not fresh:
        return
    state.other_hearing = True
    render_active(f"{rec.get('source')} hears Jared")
    if state.other_clear_task:
        state.other_clear_task.cancel()
    state.other_clear_task = asyncio.create_task(_clear_other_hearing_after())


def on_request(payload) -> None:
    """Authoritative LED 'working' state from the command lifecycle itself: in flight (transcribing /
    pending / claimed) → blink; done/error → off. The HTTP pings (capturers at capture-accept, worker at
    claim/done) remain as the fast edge; this keeps the LED truthful even when a ping is lost or a capture
    dies mid-transcription — same source of truth as the floor lamp (pi-audio watches these very rows)."""
    try:
        rec = (payload.get("data") or {}).get("record") or {}
    except AttributeError:
        return
    status = str(rec.get("status") or "")
    if status in ("transcribing", "pending", "claimed"):
        state.working = True
        if esp.link.ws is not None:
            arm_safety(esp.link.ws)
    elif status in ("done", "error"):
        state.working = False
        state.capturing = False
        cancel_safety()
    else:
        return
    render_active(f"request {status}")
