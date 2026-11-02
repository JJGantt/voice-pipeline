"""The always-on governor: switch wake-free off on a run of noise, back on once the room is quiet."""

import time
from datetime import datetime, timezone

import httpx

from .config import SUPABASE_URL
from .db import headers
from .logs import log, log_error, now_iso


AO_IGNORE_STREAK_OFF = 3    # consecutive ignored turns that put the brakes on… (Jared's numbers)


AO_IGNORE_WINDOW_S = 300    # …when they all land within this window — a rate, not a lifetime count


def maybe_auto_disable_wakefree() -> None:
    """A run of nothing-but-noise means nobody is talking TO the assistant: switch always-on off and let
    the wake words take over (they stay live when wake-free is off, so voice never goes dark). The streak
    is DERIVED from the last hub rows rather than counted in memory, so it reads the same whichever
    worker judged each turn; any acted-on command breaks it, and a streak that straddles a quiet gap
    (older than the window) doesn't count — only a currently-noisy room flips the mode."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_messages",
                      params={"select": "ignored,created_at", "focus": "eq.hub", "role": "eq.user",
                              # id is a UUID — ordering by it is lexical, not recency. created_at only.
                              "order": "created_at.desc", "limit": str(AO_IGNORE_STREAK_OFF)},
                      headers=headers(), timeout=5)
        rows = r.json()
        if len(rows) < AO_IGNORE_STREAK_OFF or not all(row.get("ignored") for row in rows):
            return
        oldest = datetime.fromisoformat(str(rows[-1]["created_at"]).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - oldest).total_seconds() > AO_IGNORE_WINDOW_S:
            return
        g = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "eq.wakefree_enabled", "select": "value"},
                      headers=headers(), timeout=5)
        grows = g.json()
        if not (grows and str(grows[0].get("value")).strip().lower() in ("1", "true", "on", "yes")):
            return   # already off — nothing to flip
        # Marker FIRST, then the flip: an auto-off must stay distinguishable from Jared turning
        # always-on off by voice — only a MARKED off self-heals after the cooldown below.
        httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                   headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                   json={"key": "wakefree_autooff_at", "value": now_iso(), "updated_at": now_iso()},
                   timeout=10)
        httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                   headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                   json={"key": "wakefree_enabled", "value": "0", "updated_at": now_iso()},
                   timeout=10)
        log(f"always-on auto-disabled: last {AO_IGNORE_STREAK_OFF} turns all noise within the hour")
    except Exception as e:  # noqa: BLE001 — the governor must never take a turn down
        log_error(f"wakefree auto-off check failed: {e}")


AO_AUTOOFF_COOLDOWN_S = 300     # minimum brake hold before a re-arm is even considered


AO_REARM_QUIET_S = 180          # …and every mic's heard_jared_at must be at least this stale (room died down)


_ao_reenable_check = {"t": 0.0, "interval": 300.0}


def maybe_auto_reenable_wakefree() -> None:
    """The governor's auto-off holds until the ROOM GOES QUIET, then re-arms always-on. During red the
    capture layer keeps ECAPA warm as a passive sensor and stamps voice_presence.heard_jared_at on
    confirmed voice — no model judgments — so an ongoing conversation keeps the brake on and a
    died-out one releases it: re-arm requires the minimum hold AND AO_REARM_QUIET_S of silence from
    every mic. A stale/absent stamp reads as quiet, so a dead sensor can only re-arm EARLIER, never
    wedge it red (and a still-noisy room then just re-trips on the next ignored streak). A MANUAL off
    (spoken command → set_always_on) writes no marker and never self-heals — only the governor's
    marked flips do. Runs from the heartbeat thread on every worker: one check per ~5 min normally,
    tightened to ~60s while a brake is active (idempotent across workers)."""
    now = time.monotonic()
    if now - _ao_reenable_check["t"] < _ao_reenable_check["interval"]:
        return
    _ao_reenable_check["t"] = now
    _ao_reenable_check["interval"] = 300.0
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "in.(wakefree_enabled,wakefree_autooff_at)", "select": "key,value"},
                      headers=headers(), timeout=5)
        s = {row["key"]: row["value"] for row in r.json()}
        marker = (s.get("wakefree_autooff_at") or "").strip()
        enabled = str(s.get("wakefree_enabled", "0")).strip().lower() in ("1", "true", "on", "yes")

        def _set(k, v):
            httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                       headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                       json={"key": k, "value": v, "updated_at": now_iso()}, timeout=10)

        if enabled:
            if marker:
                _set("wakefree_autooff_at", "")   # re-enabled by hand before the cooldown — clear the stale marker
            return
        if not marker:
            return   # manual off — stays off until Jared says otherwise
        _ao_reenable_check["interval"] = 60.0   # brake active → watch for the release more closely
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(marker.replace("Z", "+00:00"))).total_seconds()
        if age < AO_AUTOOFF_COOLDOWN_S:
            return
        p = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_presence",
                      params={"select": "heard_jared_at"}, headers=headers(), timeout=5)
        stamps = []
        for row in p.json():
            v = row.get("heard_jared_at")
            if v:
                try:
                    stamps.append(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
                except ValueError:
                    pass
        if stamps and (datetime.now(timezone.utc) - max(stamps)).total_seconds() < AO_REARM_QUIET_S:
            return   # the room is still talking — hold the brake
        _set("wakefree_enabled", "1")
        _set("wakefree_autooff_at", "")
        log(f"always-on auto-re-enabled: quiet after {int(age)}s braked")
    except Exception as e:  # noqa: BLE001 — never take the heartbeat down
        log_error(f"wakefree auto-reenable check failed: {e}")
