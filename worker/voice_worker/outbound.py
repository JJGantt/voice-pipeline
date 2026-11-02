"""Best-effort pushes to the hub's activity bar, the ESP light, and the Mac orchestrator's pty."""

import threading

import httpx

from . import runstate
from .config import HUB_HEADERS, HUB_PUSH_URL, LED_BASE_URL, LED_TOKEN, ORCH_TOKEN, ORCH_URL


def push_activity(text: str, state: str = "tool") -> None:
    """Fire a transient activity event to the hub (best-effort; never raises)."""
    if not HUB_PUSH_URL or runstate._hub_unreachable:
        return
    try:
        httpx.post(HUB_PUSH_URL, json={"text": text, "state": state, "focus": "hub"}, headers=HUB_HEADERS,
                   timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


def ping_esp_led(state: str) -> None:
    """Set the ESP light: state ∈ {off, blink, on}. Best-effort; no-op if the ESP server/light is absent."""
    if not LED_BASE_URL:
        return

    def _go():
        try:
            httpx.get(f"{LED_BASE_URL}/led-{state}",
                      headers={"X-Token": LED_TOKEN} if LED_TOKEN else None, timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_go, daemon=True).start()


def inject_to_terminal(text: str) -> tuple[bool, str | None]:
    """Send a spoken line to the focused Mac session's wall terminal (the
    orchestrator writes it to the live pty and presses Enter). Best-effort."""
    try:
        r = httpx.post(
            f"{ORCH_URL}/inject",
            headers={"X-Token": ORCH_TOKEN},
            json={"text": text, "submit": True},
            timeout=10.0,
        )
        if r.status_code < 300:
            return True, None
        return False, f"orchestrator /inject HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"orchestrator unreachable: {e}"
