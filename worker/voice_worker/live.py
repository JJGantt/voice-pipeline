"""Live state (settings, focus, today's thread) held in memory off realtime, and the replayed day."""

import os
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import HTTP_TIMEOUT_S, SESSION_IDLE_RESET_S
from .db import Supabase, headers, note_ok, report_failure
from .logs import _parse_ts, log
from .prompts import is_ignore, read_marker


# LIVE STATE: the settings a turn consults, the voice focus, and today's hub thread, held in memory.
# Loaded once each time the realtime socket subscribes (it is recycled every ~5 minutes, which makes
# that reload the backstop behind the events) and kept current by its app_settings / voice_focus /
# voice_messages events, so a command reads none of them from the database. Reading them per command
# cost ~0.7s of round trips before a turn could start.
LIVE_SETTINGS = ("session_reset_s", "voice_reset_at")


_live = {"loaded": False, "settings": {}, "focus": "hub", "thread": {}}


_live_lock = threading.Lock()


_live_db: "Supabase | None" = None   # its own client: loads run on the realtime thread


def _is_thread_row(r: dict) -> bool:
    return r.get("focus") == "hub" and r.get("mode") == "hub" and r.get("role") in ("user", "assistant")


def live_load() -> bool:
    """(Re)load the live state from the database. False, leaving it unloaded, if any read failed."""
    global _live_db
    if _live_db is None:
        _live_db = Supabase(httpx.Client(timeout=HTTP_TIMEOUT_S))

    def get(table, query):
        r = _live_db.c.get(_live_db._url(table, query), headers=headers())
        r.raise_for_status()
        return r.json()

    try:
        keys = ",".join(LIVE_SETTINGS)
        settings = {r["key"]: r.get("value") for r in get("app_settings", f"key=in.({keys})&select=key,value")}
        focus = (get("voice_focus", "id=eq.1&select=focus") or [{}])[0].get("focus") or "hub"
        since = day_start(datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        rows: list = []
        while True:
            page = get("voice_messages",
                       "focus=eq.hub&mode=eq.hub&role=in.(user,assistant)"
                       f"&created_at=gte.{since}&order=created_at.asc&limit=1000&offset={len(rows)}"
                       "&select=id,focus,mode,role,text,created_at,ignored,done,needs_read,label,session_id,session_label")
            rows += page
            if len(page) < 1000:
                break
    except Exception as e:  # noqa: BLE001
        report_failure(f"live state load failed: {e}", e)
        with _live_lock:
            _live["loaded"] = False
        return False
    note_ok()
    with _live_lock:
        _live.update(loaded=True, settings=settings, focus=focus, thread={r["id"]: r for r in rows})
    log(f"live state loaded: {len(rows)} thread rows, focus={focus}")
    return True


def live_event(table: str, kind: str, rec: dict, old: dict) -> None:
    """Apply one realtime change to the live state."""
    with _live_lock:
        if not _live["loaded"]:
            return
        if table == "app_settings":
            key = (rec or old).get("key")
            if key in LIVE_SETTINGS:
                if kind == "DELETE":
                    _live["settings"].pop(key, None)
                else:
                    _live["settings"][key] = rec.get("value")
        elif table == "voice_focus" and kind != "DELETE":
            _live["focus"] = rec.get("focus") or "hub"
        elif table == "voice_messages":
            if kind == "DELETE":
                _live["thread"].pop(old.get("id"), None)
            elif _is_thread_row(rec):
                _live["thread"][rec["id"]] = rec
            else:
                _live["thread"].pop(rec.get("id"), None)


def _live_view() -> dict:
    """The live state, loading it first if no subscribe has done so yet (startup, or a failed load)."""
    if not _live["loaded"]:
        live_load()
    with _live_lock:
        return {"settings": dict(_live["settings"]), "focus": _live["focus"],
                "thread": list(_live["thread"].values())}


def session_reset_s() -> float:
    """Idle budget before the hub conversation resets, from the hub setting `session_reset_s`."""
    try:
        return float(_live_view()["settings"].get("session_reset_s") or SESSION_IDLE_RESET_S)
    except (TypeError, ValueError):
        return SESSION_IDLE_RESET_S


# The hub agent's day: it begins at 4 AM in the owner's own timezone, not at midnight and not in the host's
# zone (the Fly worker runs in UTC). The same boundary the hub uses for every "today" (lib/dates.js).
DAY_TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "America/New_York"))


DAY_STARTS_HOUR = 4


def day_start(now: datetime) -> datetime:
    """The start of the day `now` belongs to, as an aware datetime."""
    local = now.astimezone(DAY_TZ)
    start = local.replace(hour=DAY_STARTS_HOUR, minute=0, second=0, microsecond=0)
    return start - timedelta(days=1) if local < start else start


def recent_context() -> str:
    """THE WHOLE DAY, replayed into a FRESH claude run instead of using --resume. We keep our own clean
    transcript in voice_messages (each turn's user prompt + final assistant reply), so a fresh process
    gets exactly the conversation and NONE of the CLI-session baggage — tool schemas, thinking blocks,
    that makes a resumed session drift from what was actually said.

    Everything since the day began (4 AM, `day_start`), or since a manual reset from the hub if that is
    later, oldest first, each line with its time — so the user can refer back to anything said earlier today,
    from any surface. An idle gap does not cut it: a quiet afternoon is still today. A day is small: a
    heavy one measured ~4.5k tokens of real exchanges plus ~6k of marked room chatter. Tool RESULTS are
    deliberately omitted — a follow-up that needs live data just re-fetches with a tool."""
    now = datetime.now(timezone.utc)
    since = day_start(now)
    live = _live_view()
    reset = _parse_ts(live["settings"].get("voice_reset_at") or "")
    if reset and reset > since:
        since = reset
    rows = sorted((r for r in live["thread"]
                   if r.get("done") and (_parse_ts(r.get("created_at") or "") or since) >= since),
                  key=lambda r: r.get("created_at") or "")
    # An ignored turn is marked rather than hidden. Unmarked, room chatter would replay as things the user
    # said TO the assistant; marked, the user can point at one — "do the one you ignored" — and the model can
    # see both what was said and that nothing was done about it.
    def _line(r):
        ts = _parse_ts(r.get("created_at") or "")
        when = ts.astimezone(DAY_TZ).strftime("%-I:%M %p") if ts else "?"
        who = 'You' if r.get('role') == 'user' else 'Assistant'
        if r.get('role') == 'assistant' and r.get('session_id'):
            # A session's reply posted into the hub thread. Named, so a reply from the user that follows it can
            # be sent back to that session with follow_up rather than answered as if the hub had said it.
            who = f"Session \"{r.get('session_label') or r['session_id']}\" ({r['session_id']})"
        txt = (r.get('text') or '').strip()
        if r.get('ignored'):
            return f"[{when}] You (ignored as room noise, not acted on): {txt}"
        # A relayed session reply is flagged too (it is in the inbox by being relayed), but it never
        # carried the marker, so the replay leaves it off rather than teach that sessions write one.
        # A flagged reply with NO label replays with no marker at all: a bare [[read]] in the history is
        # copied by the next turn, which then writes one bare too, and the day never gets a label again.
        if r.get('needs_read') and r.get('role') == 'assistant' and not r.get('session_id') and r.get('label'):
            txt = f"{txt} {read_marker(r['label'])}"
        return f"[{when}] {who}: {txt}"
    # A bare sentinel reply is dropped from the replay. It is never shown as a reply — but a deliberate
    # turn that emitted one anyway was saved as `Assistant: __IGNORE__`, and replaying that line taught
    # every later button press the sentinel by example, so they answered with it too.
    lines = [_line(r) for r in rows
             if (r.get('text') or '').strip() and not (r.get('role') == 'assistant' and is_ignore(r.get('text')))]
    if not lines:
        return ""
    now_local = now.astimezone(DAY_TZ).strftime("%-I:%M %p")
    return ("[Today so far: everything said between you since the day began, oldest first, each line "
            "with its time. Context only; tool RESULTS are not included, so call a tool if you need "
            f"current data. It is now {now_local}.\n" + "\n".join(lines) + "]\n\n")


def get_focus() -> str:
    return _live_view()["focus"]
