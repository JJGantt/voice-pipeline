"""Supabase REST access with the service key, shared by every module that reads or writes a row."""

import logging

import httpx

from . import config

log = logging.getLogger("voice")


def configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY)


def url(path: str) -> str:
    return f"{config.SUPABASE_URL}{path}"


def headers(prefer: str | None = None) -> dict:
    """Service-key headers; a write (`prefer` given) also carries the JSON content type."""
    key = config.SUPABASE_SERVICE_KEY
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if prefer:
        h.update({"Content-Type": "application/json", "Prefer": prefer})
    return h


def get_settings(keys) -> dict:
    """Read app_settings values: the shared voiceprint, wake-free thresholds and mode flags. Synchronous,
    because voice ID loads in a worker thread; from the event loop call it through asyncio.to_thread."""
    if not configured():
        return {}
    try:
        r = httpx.get(url("/rest/v1/app_settings"),
                      params={"key": f"in.({','.join(keys)})", "select": "key,value"},
                      headers=headers(), timeout=15)
        return {row["key"]: row["value"] for row in r.json()}
    except Exception as e:  # noqa: BLE001
        log.error("settings read failed: %s", e)
        return {}
