"""This ESP's row in the shared voice_presence table: the mic-liveness beacon, the 'heard the speaker'
stamp, and reading the other mics' stamps."""

import asyncio
import time
from datetime import datetime, timezone

import httpx

from . import db, esp


async def alive_beacon() -> None:
    """Mic-liveness beacon: stamp voice_presence.alive_at (source 'esp') every 30s WHILE audio frames
    are arriving from the device. Gated on the link's last_frame — real audio flow, not process/socket
    existence — so a stale beacon always means the ESP mic is genuinely down."""
    if not db.configured():
        return
    while True:
        if esp.link.last_frame and time.monotonic() - esp.link.last_frame < 10:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.post(db.url("/rest/v1/voice_presence?on_conflict=source"),
                                 headers=db.headers("resolution=merge-duplicates,return=minimal"),
                                 json={"source": "esp",
                                       "alive_at": datetime.now(timezone.utc).isoformat()})
            except Exception:  # noqa: BLE001 — best-effort; a missed beat reads as one late beacon
                pass
        await asyncio.sleep(30)


async def publish(jared: bool) -> None:
    """Upsert this ESP's row in the shared voice_presence — the system-wide 'who is hearing the speaker' truth.
    Fire-and-forget on each confirmed check; any mic's fresh heard_jared_at makes the whole system
    'Hearing the speaker' (drives every indicator + lets the other mic suppress a partial capture)."""
    if not db.configured():
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    body = {"source": "esp", "updated_at": now_iso, "heard_sound_at": now_iso}
    if jared:
        body["heard_jared_at"] = now_iso
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(db.url("/rest/v1/voice_presence?on_conflict=source"),
                         headers=db.headers("resolution=merge-duplicates,return=minimal"), json=body)
    except Exception:  # noqa: BLE001 — presence is best-effort
        pass


async def other_heard_jared():
    """Most recent time a mic OTHER than this ESP confirmed the speaker (datetime, or None). Used at capture
    close to suppress a partial: if the Mac still has you, the ESP drops its fragment and the Mac sends
    the whole utterance."""
    if not db.configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(db.url("/rest/v1/voice_presence"),
                            params={"source": "neq.esp", "select": "heard_jared_at"}, headers=db.headers())
        stamps = [datetime.fromisoformat(row["heard_jared_at"]) for row in r.json() if row.get("heard_jared_at")]
        return max(stamps) if stamps else None
    except Exception:  # noqa: BLE001
        return None
