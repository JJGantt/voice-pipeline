"""The Supabase REST wrapper. It never raises; a network-level failure marks this worker degraded."""

import time

import httpx

from .config import NET_DEGRADED_S, SUPABASE_KEY, SUPABASE_URL
from .logs import log_error


def headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


_net_fail_at = 0.0   # monotonic time of this worker's last network-level Supabase failure


def _note_failure(e: Exception) -> None:
    """Record a failure that says the LINK is bad: transport errors and 5xx. A 4xx is a bad query, which
    says nothing about the network, and must not take a healthy worker out of rotation."""
    global _net_fail_at
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
        return
    _net_fail_at = time.monotonic()


def degraded() -> bool:
    return _net_fail_at > 0 and time.monotonic() - _net_fail_at < NET_DEGRADED_S


class Supabase:
    def __init__(self, client: httpx.Client):
        self.c = client

    def _url(self, table: str, query: str = "") -> str:
        u = f"{SUPABASE_URL}/rest/v1/{table}"
        return f"{u}?{query}" if query else u

    def get(self, table: str, query: str = "") -> list:
        try:
            r = self.c.get(self._url(table, query), headers=headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 — defensive on purpose
            _note_failure(e)
            log_error(f"GET {table}?{query} failed: {e}")
            return []

    def post(self, table: str, body, extra_headers: dict | None = None) -> list | None:
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.post(self._url(table), headers=h, json=body)
            r.raise_for_status()
            # return=representation gives JSON back; plain inserts give 201 + empty body
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            _note_failure(e)
            log_error(f"POST {table} failed: {e}")
            return None

    def patch(self, table: str, query: str, body, extra_headers: dict | None = None):
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.patch(self._url(table, query), headers=h, json=body)
            r.raise_for_status()
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            _note_failure(e)
            log_error(f"PATCH {table}?{query} failed: {e}")
            return None

    def upsert(self, table: str, body, on_conflict: str):
        try:
            r = self.c.post(
                self._url(table, f"on_conflict={on_conflict}"),
                headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                json=body,
            )
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            _note_failure(e)
            log_error(f"UPSERT {table} failed: {e}")
            return False
