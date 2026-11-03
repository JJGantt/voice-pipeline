"""The Supabase REST wrapper. It never raises; a network-level failure marks this worker degraded."""

import time

import httpx

from .config import NET_DEGRADED_S, SUPABASE_KEY, SUPABASE_URL
from .logs import log, log_error


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


# A transport error or 5xx is how the first request after the Mac wakes always goes (a pooled socket
# the server closed while it slept, or Wi-Fi not back yet), and how an occasional Supabase gateway blip
# goes. The loops retry on their own, so one is a warning; only a link failing for TRANSPORT_RAISE_S
# straight is an error. Any successful request ends the streak.
TRANSPORT_RAISE_S = 300.0
_streak_since = 0.0   # monotonic start of the current run of link failures; 0 = none
_streak_raised = False


def is_link_failure(e: Exception) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500
    return isinstance(e, (httpx.TransportError, OSError))


def note_ok() -> None:
    global _streak_since, _streak_raised
    _streak_since, _streak_raised = 0.0, False


def report_failure(msg: str, e: Exception) -> None:
    """Log a failed Supabase call: a link failure as a warning until it has lasted TRANSPORT_RAISE_S,
    then once as an error for the streak; anything else (a bad query, a bug) as an error right away."""
    global _streak_since, _streak_raised
    _note_failure(e)
    if not is_link_failure(e):
        log_error(msg)
        return
    now = time.monotonic()
    if not _streak_since:
        _streak_since = now
    if not _streak_raised and now - _streak_since >= TRANSPORT_RAISE_S:
        _streak_raised = True
        log_error(f"{msg} (Supabase unreachable for {int(now - _streak_since)}s)")
    else:
        log(f"WARN {msg} (will retry)")


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
            note_ok()
            return r.json()
        except Exception as e:  # noqa: BLE001 — defensive on purpose
            report_failure(f"GET {table}?{query} failed: {e}", e)
            return []

    def post(self, table: str, body, extra_headers: dict | None = None) -> list | None:
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.post(self._url(table), headers=h, json=body)
            r.raise_for_status()
            note_ok()
            # return=representation gives JSON back; plain inserts give 201 + empty body
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            report_failure(f"POST {table} failed: {e}", e)
            return None

    def patch(self, table: str, query: str, body, extra_headers: dict | None = None):
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.patch(self._url(table, query), headers=h, json=body)
            r.raise_for_status()
            note_ok()
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            report_failure(f"PATCH {table}?{query} failed: {e}", e)
            return None

    def upsert(self, table: str, body, on_conflict: str):
        try:
            r = self.c.post(
                self._url(table, f"on_conflict={on_conflict}"),
                headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                json=body,
            )
            r.raise_for_status()
            note_ok()
            return True
        except Exception as e:  # noqa: BLE001
            report_failure(f"UPSERT {table} failed: {e}", e)
            return False
