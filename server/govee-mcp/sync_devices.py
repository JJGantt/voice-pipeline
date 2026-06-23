#!/usr/bin/env python3
"""Sync the Govee light inventory from the cloud API into Supabase `govee_devices`.

Pulls /user/devices, keeps real lights (type=light + a brightness capability — which drops phantom
'powerSwitch-only' ghosts), slugifies names into stable keys (deduping same-named devices), and
upserts. Devices no longer on the account are removed. The `hidden` flag is preserved across syncs.

Run once to seed, or with --loop on the always-on machine to refresh every ~30 min.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

GOVEE_API_KEY = os.environ.get("GOVEE_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
INTERVAL_S = int(os.environ.get("GOVEE_SYNC_INTERVAL_S", "1800"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or "light"


def _sb(extra=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def discover() -> list:
    r = httpx.get("https://openapi.api.govee.com/router/api/v1/user/devices",
                  headers={"Govee-API-Key": GOVEE_API_KEY, "Content-Type": "application/json"}, timeout=20)
    r.raise_for_status()
    out, seen = [], set()
    for d in (r.json().get("data") or []):
        if d.get("type") != "devices.types.light":
            continue
        caps = {c.get("instance") for c in (d.get("capabilities") or [])}
        if "brightness" not in caps:          # real lights have brightness; ghosts (powerSwitch-only) don't
            continue
        base = _slug(d.get("deviceName"))
        key, n = base, 1
        while key in seen:
            n += 1
            key = f"{base}_{n}"
        seen.add(key)
        out.append({"key": key, "device_id": d.get("device"), "sku": d.get("sku"),
                    "display_name": d.get("deviceName")})
    return out


def sync() -> None:
    lights = discover()
    now = datetime.now(timezone.utc).isoformat()
    for L in lights:   # upsert; omit `hidden` so an existing flag is preserved, new rows default false
        httpx.post(f"{SUPABASE_URL}/rest/v1/govee_devices?on_conflict=key",
                   headers=_sb({"Prefer": "resolution=merge-duplicates"}),
                   json={**L, "updated_at": now}, timeout=20)
    keys = {L["key"] for L in lights}
    existing = httpx.get(f"{SUPABASE_URL}/rest/v1/govee_devices?select=key", headers=_sb(), timeout=20).json()
    for row in existing:                # prune devices no longer on the account
        if row["key"] not in keys:
            httpx.delete(f"{SUPABASE_URL}/rest/v1/govee_devices?key=eq.{row['key']}", headers=_sb(), timeout=20)
    print(f"{now} synced {len(lights)} lights: {', '.join(L['key'] for L in lights)}", flush=True)


if __name__ == "__main__":
    if not (GOVEE_API_KEY and SUPABASE_KEY):
        print("FATAL: need GOVEE_API_KEY + SUPABASE_KEY", file=sys.stderr)
        sys.exit(2)
    if "--loop" in sys.argv:
        while True:
            try:
                sync()
            except Exception as e:  # noqa: BLE001
                print(f"sync failed: {e}", flush=True)
            time.sleep(INTERVAL_S)
    else:
        sync()
