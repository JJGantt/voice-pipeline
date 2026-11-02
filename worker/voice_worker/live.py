"""Live state (settings, focus, today's thread) held in memory off realtime, and the replayed day."""

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx

from .config import AGENT_SESSION_FILE, HTTP_TIMEOUT_S, HUB_SESSION_FILE, SESSION_IDLE_RESET_S, STATE_DIR
from .db import Supabase, _note_failure, headers
from .logs import _parse_ts, log, log_error, now_iso
from .prompts import READ_MARKER, is_ignore


def session_file(agent: bool) -> Path:
    """Hub mode and full-agent mode keep SEPARATE, independently-persisted Claude sessions, so switching
    between them never carries over or clobbers the other's context."""
    return AGENT_SESSION_FILE if agent else HUB_SESSION_FILE


def read_session(agent: bool) -> dict | None:
    f = session_file(agent)
    try:
        if f.exists():
            return json.loads(f.read_text())
    except Exception as e:  # noqa: BLE001
        log_error(f"read {f.name} failed: {e}")
    return None


def write_session(agent: bool, session_id: str) -> None:
    f = session_file(agent)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps({"session_id": session_id, "last_activity_iso": now_iso()})
        )
    except Exception as e:  # noqa: BLE001
        log_error(f"write {session_file(agent).name} failed: {e}")


# LIVE STATE: the settings a turn consults, the voice focus, and today's hub thread, held in memory.
# Loaded once each time the realtime socket subscribes (it is recycled every ~5 minutes, which makes
# that reload the backstop behind the events) and kept current by its app_settings / voice_focus /
# voice_messages events, so a command reads none of them from the database. Reading them per command
# cost ~0.7s of round trips before a turn could start.
LIVE_SETTINGS = ("agent_mode", "agent_cwd", "session_reset_s", "voice_reset_at")


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
                       "&select=id,focus,mode,role,text,created_at,ignored,done,needs_read,session_id,session_label")
            rows += page
            if len(page) < 1000:
                break
    except Exception as e:  # noqa: BLE001
        _note_failure(e)
        log_error(f"live state load failed: {e}")
        with _live_lock:
            _live["loaded"] = False
        return False
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


def agent_settings() -> dict:
    """Full-agent-mode knobs from app_settings: `agent_mode` (bool — upgrade the subscription path to the
    unrestricted Claude Code agent) and `agent_cwd` (where that agent runs; default $HOME)."""
    s = _live_view()["settings"]
    cwd = (s.get("agent_cwd") or "").strip()
    return {"mode": str(s.get("agent_mode", "")).strip().lower() in ("1", "true", "on", "yes"),
            "cwd": os.path.expanduser(cwd) if cwd else os.path.expanduser("~")}


def should_reset_session(state: dict | None, agent: bool = False) -> bool:
    """Reset (start fresh) if: no state, idle > the reset budget, or it's a new calendar day. In full-agent
    mode the conversation resets MANUALLY only — a long coding session shouldn't be wiped by an idle gap or a
    midnight rollover (switching the mode off triggers a reset via the hub), so only a missing session forces
    a fresh start."""
    if not state or not state.get("session_id"):
        return True
    if agent:
        return False
    last = _parse_ts(state.get("last_activity_iso", ""))
    if not last:
        return True
    now = datetime.now(timezone.utc)
    if (now - last).total_seconds() > session_reset_s():
        return True
    if last.astimezone().date() != now.astimezone().date():
        return True
    return False


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
        if r.get('needs_read') and r.get('role') == 'assistant':
            txt = f"{txt} {READ_MARKER}"
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
