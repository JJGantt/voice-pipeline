"""A command's voice_requests row: opened at capture-accept ('transcribing'), filled with the transcript
('pending', the handoff to the workers), or closed as an error. `handle_transcript` is the handoff seam."""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from . import config, db

log = logging.getLogger("voice")


async def notify_transcribe_failure() -> None:
    """A capture was confirmed as a command but transcription failed even after the retry. Say so in the
    hub conversation — a spoken command silently vanishing is the worst failure mode this pipeline has."""
    if not db.configured():
        return
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(db.url("/rest/v1/voice_messages"), headers=db.headers("return=minimal"),
                         json={"focus": "hub", "mode": "hub", "role": "assistant", "done": True,
                               "text": "⚠️ Heard you, but speech-to-text failed twice — say that again?"})
    except Exception:  # noqa: BLE001 — the log line above is the floor
        pass


async def open_request(cap_start: str | None = None, cap_end: str | None = None,
                       always_on: bool = True) -> str | None:
    """Open the command's lifecycle row at CAPTURE-ACCEPT — status 'transcribing', no transcript yet.
    This is the moment the working indicators turn on (ESP blink here, floor lamp via pi-audio's
    realtime sub on this insert): the earliest instant the system has committed to processing the
    command. Returns the row id, or None (no creds / insert failed → caller falls back to the old
    insert-at-enqueue behavior)."""
    if not db.configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(
                db.url("/rest/v1/voice_requests"), headers=db.headers("return=representation"),
                json={"status": "transcribing", "source": "fly", "focus": "hub", "always_on": always_on,
                      **({"capture_start": cap_start} if cap_start else {}),
                      **({"capture_end": cap_end} if cap_end else {})},
            )
            rows = r.json()
            return rows[0]["id"] if isinstance(rows, list) and rows else None
    except Exception as e:  # noqa: BLE001
        log.error("open-request failed: %s", e)
        return None


async def close_request_error(rid: str, err: str) -> None:
    """Close a lifecycle row whose capture never became a runnable command (empty/failed STT) — the
    error status is what turns the working indicators back off."""
    if not (db.configured() and rid):
        return
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.patch(
                db.url(f"/rest/v1/voice_requests?id=eq.{rid}"), headers=db.headers("return=minimal"),
                json={"status": "error", "error": err,
                      "updated_at": datetime.now(timezone.utc).isoformat()},
            )
    except Exception as e:  # noqa: BLE001
        log.error("close-request failed: %s", e)


async def handle_transcript(text: str, duration_s: float, truncated: bool,
                            cap_start: str | None = None, cap_end: str | None = None,
                            rid: str | None = None, alt: str | None = None, always_on: bool = True) -> bool:
    """HANDOFF SEAM — the rest of the system hooks in here.

    `duration_s` is the captured length; `truncated` is True if the capture ended
    by hitting MAX_UTTERANCE_S while speech was still active (the hub surfaces this).
    With `rid` (the lifecycle row opened at capture-accept) the transcript PATCHes that row to
    'pending'; without one it falls back to inserting the row here.
    """
    log.info("TRANSCRIPT (%.0fs%s): %r", duration_s, ", TRUNCATED" if truncated else "", text)
    # Strip a leading wake phrase the STT often captures ("Hey Rhasspy, …" / "Hey Jarvis, …") — it's the
    # trigger word, not part of the command. Tolerant of Whisper mishears (raspy/rhaspy/raspberry, jarvis/jarvas; "very").
    wake = r"^\W*(hey|hi|ok|okay|very)?\W*(rhasspy|rhaspy|raspy|rasbee|rasby|raspberry|rasp|aspie|aspy|asbie|aspen|jarvis|jarvas|jervis)\b[\s,.!:;-]*"
    text = re.sub(wake, "", text or "", count=1, flags=re.IGNORECASE).strip()
    alt = re.sub(wake, "", alt or "", count=1, flags=re.IGNORECASE).strip()
    words = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()  # noqa: E731
    alt_field = {"alt_transcript": alt} if alt and words(alt) != words(text) else {}
    if alt_field:
        log.info("transcribers disagree: %r vs %s %r", text[:120], config.ALT_GROQ_MODEL, alt[:120])
    if not (db.configured() and text):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            if rid:
                now = datetime.now(timezone.utc).isoformat()
                # Checked and retried: an unchecked PATCH that failed left the row 'transcribing' with
                # the transcript in hand, until the worker's sweep closed it as a dead capturer. Guarded
                # on 'transcribing' so a late write cannot revive a row the sweep already closed.
                for pause in (0.5, 1.0, 2.0, None):
                    try:
                        r = await c.patch(
                            db.url(f"/rest/v1/voice_requests?id=eq.{rid}&status=eq.transcribing"),
                            headers=db.headers("return=representation"),
                            # transcribed_at marks STT-done (transcribing→pending) so the created→claimed gap
                            # splits into STT (created→transcribed_at) vs worker pickup (transcribed_at→claimed_at).
                            json={"transcript": text, "status": "pending", **alt_field,
                                  "transcribed_at": now, "updated_at": now},
                        )
                        r.raise_for_status()
                        if not r.json():
                            log.warning("lifecycle row %s is no longer transcribing; transcript dropped", rid)
                            return False
                        break
                    except Exception as e:  # noqa: BLE001
                        if pause is None:
                            raise
                        log.warning("enqueue patch failed (%s); retrying in %.1fs", e, pause)
                        await asyncio.sleep(pause)
            else:
                # Fallback (no lifecycle row): insert at enqueue. Checked, so a refused insert is an error.
                r = await c.post(
                    db.url("/rest/v1/voice_requests"), headers=db.headers("return=minimal"),
                    json={"transcript": text, "source": "fly", "always_on": always_on, **alt_field,
                          **({"capture_start": cap_start} if cap_start else {}),
                          **({"capture_end": cap_end} if cap_end else {})},
                )
                r.raise_for_status()
        return True
    except Exception as e:
        log.error("supabase enqueue failed: %s", e)
        return False
