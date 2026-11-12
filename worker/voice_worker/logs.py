"""Logging, failure classification and the ops-responder alert."""

from datetime import datetime, timezone

import ops_report as ops
from .config import WORKER_ID


def classify_failure(err: str) -> str:
    e = (err or "").lower()
    if "401" in e or "authenticate" in e or "revoked" in e or "oauth" in e:
        return "auth"
    if "credit balance" in e or "billing" in e or "402" in e:
        return "billing"
    if "429" in e or "rate limit" in e or "overloaded" in e:
        return "rate-limit"
    return "error"


def alert_failure(stage: str, err: str, detail: str = "") -> None:
    """Report a worker failure to the ops responder. `detail` is what the investigating agent is told
    about the request and what became of it."""
    kind = classify_failure(err)
    msg = f"{stage} · {kind} · {(err or '')[:300]}"
    log(f"ALERT {msg}")
    ops.report(msg, context={"detail": detail[:2000]} if detail else None)


def now_iso() -> str:
    """UTC now as an ISO-8601 string (what Supabase timestamptz expects)."""
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"{now_iso()} [{WORKER_ID}] {msg}", flush=True)


def log_occurrence(msg: str) -> None:
    """A failure a retry or fallback is already handling: logged as a warning and sent to the ops
    responder as an occurrence, which is counted against its watch and never an incident by itself."""
    log(f"WARN {msg}")
    ops.occurrence(msg)


def log_error(msg: str) -> None:
    """A failure: logged like any line, and reported to the ops responder (health/responder), where a new
    incident starts an agent. A failure a retry or a fallback is already handling is `log_occurrence`."""
    log(msg)
    ops.report(msg)


def _parse_ts(s: str):
    if not s:
        return None
    try:
        # Supabase returns e.g. 2026-06-20T00:11:00.124643+00:00
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
