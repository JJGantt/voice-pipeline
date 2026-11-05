"""Heartbeats and priority: which worker should take a request, and handing one on."""

import shutil
import time
import uuid
from datetime import datetime, timezone

from . import runstate
from .config import CLAUDE_BIN, HEARTBEAT_FRESH_S, WORKER_ID, WORKER_PRIORITY
from .db import Supabase, degraded
from .logs import _parse_ts, alert_failure, log, now_iso
from .outbound import push_activity


# This PROCESS, as opposed to this host. Claims carry it (`claim_epoch`) and the heartbeat publishes
# it, so a claim left by a process that died and came back under the same WORKER_ID — a restart
# mid-run, a deploy, a SIGKILL at the unit's stop timeout — is recognisable as orphaned even though
# the host's heartbeat is fresh.
WORKER_EPOCH = uuid.uuid4().hex[:12]


def heartbeat(db: Supabase) -> None:
    info = {"epoch": WORKER_EPOCH}
    if degraded():
        info["degraded"] = True
    if runstate._stopping:
        info["stopping"] = True
    if runstate._lease_deadline and time.monotonic() > runstate._lease_deadline:
        info["stalled"] = True   # the turn in flight stopped renewing its lease: hung, though this beats
    db.upsert("worker_presence", {"worker": WORKER_ID, "last_seen": now_iso(), "info": info},
              on_conflict="worker")


# worker_presence reads are cached briefly: heartbeats only update every HEARTBEAT_INTERVAL_S (30s),
# so a fresher read returns identical data, and every consumer tolerates HEARTBEAT_FRESH_S (90s) of
# staleness — reading it on every claim iteration was just egress noise.
_presence_cache = {"rows": None, "at": 0.0}


PRESENCE_CACHE_S = 30.0


def presence_rows(db: Supabase) -> list:
    now = time.monotonic()
    if _presence_cache["rows"] is None or now - _presence_cache["at"] > PRESENCE_CACHE_S:
        _presence_cache["rows"] = db.get("worker_presence", "select=worker,last_seen,info") or []
        _presence_cache["at"] = now
    return _presence_cache["rows"]


def _healthy(row: dict, window_s: float = HEARTBEAT_FRESH_S) -> bool:
    """A presence row that is fresh AND not reporting a degraded link, a shutdown or a stalled turn."""
    info = row.get("info") or {}
    if info.get("degraded") or info.get("stopping") or info.get("stalled"):
        return False
    ts = _parse_ts(row.get("last_seen", ""))
    return bool(ts) and (datetime.now(timezone.utc) - ts).total_seconds() <= window_s


def other_worker_healthy(db: Supabase, exclude: "set | frozenset" = frozenset()) -> bool:
    """Whether another VOICE worker (not a desk session host, which beats into the same table) is healthy
    and not in `exclude` — the workers a request has already failed on."""
    return bool(untried_workers(db, {WORKER_ID} | set(exclude)))


def higher_priority_target(db: Supabase, exclude: "set | frozenset" = frozenset()) -> str | None:
    """The highest-ranked healthy worker ABOVE me in WORKER_PRIORITY, not in `exclude`, or None."""
    try:
        my_rank = WORKER_PRIORITY.index(WORKER_ID)
    except ValueError:
        # I'm not in the priority list — assume lowest, defer to nobody specific.
        my_rank = len(WORKER_PRIORITY)
    above = [w for w in WORKER_PRIORITY[:my_rank] if w not in exclude]
    if not above:
        return None   # nobody to defer to: the primary decides without reading presence at all
    # Fresh, not the 30s cache: this runs only while a request is waiting on a worker below the top, and
    # a stale view is exactly what keeps it waiting on a primary that has just announced it is stopping
    # (measured: 9.1s to fail over from a stopped Fly worker, all of it the cache and FAILOVER_S).
    _presence_cache["rows"] = None
    healthy = {r.get("worker") for r in presence_rows(db) if _healthy(r)}
    return next((w for w in above if w in healthy), None)


def defer_target(db: Supabase, tried: "set | frozenset" = frozenset()) -> str | None:
    """The worker THIS one should leave a request to, or None to claim it: priority ordering. A request
    other workers have already failed on (`tried`) goes to the highest-ranked worker that has not tried it.
    Every worker holds the day's thread in its live state, so which one claims a turn does not matter."""
    return higher_priority_target(db, tried)


def should_defer(db: Supabase) -> bool:
    return defer_target(db) is not None


# Tools that only read. Running a turn again after any of these repeats nothing, so a failure after
# them is still handed on; any other tool marks the turn as having acted.
REPLAY_SAFE = {"WebSearch", "WebFetch", "Read", "feeder_status", "food_level"}


def replay_safe(tool: str) -> bool:
    return tool.startswith(("get_", "list_", "search_", "find_")) or tool in REPLAY_SAFE


def claude_unready() -> str | None:
    """Why this host cannot start a turn, or None when it can. A local check, made per turn: an npm
    self-update of the CLI that dies half way leaves the link dangling or the binary a non-executable
    placeholder, and every turn here then fails the same way until someone repairs the install."""
    if shutil.which(CLAUDE_BIN) is None:
        return f"claude CLI not runnable on {WORKER_ID}: {CLAUDE_BIN}"
    return None


def untried_workers(db: Supabase, tried: set) -> list:
    """Healthy workers in WORKER_PRIORITY that are not in `tried`."""
    return [r.get("worker") for r in presence_rows(db)
            if r.get("worker") in WORKER_PRIORITY and r.get("worker") not in tried and _healthy(r)]


def hand_off(db: Supabase, req: dict, err: str) -> bool:
    """Put a request this worker failed back to 'pending' for a worker that has not tried it yet.

    Only for a turn that called no tool, so running it again cannot repeat anything it did. The workers
    tried so far ride in `failed_on`; none of them claims it again, and the rest skip
    priority for it (`claim_one`), so the next healthy one takes it at once. False when no healthy
    worker is left untried — the caller then closes it as an error, as before."""
    tried = sorted(set(req.get("failed_on") or []) | {WORKER_ID})
    left = untried_workers(db, set(tried))
    if not left:
        return False
    got = db.patch(
        "voice_requests",
        f"id=eq.{req['id']}&status=eq.claimed&claimed_by=eq.{WORKER_ID}&claim_epoch=eq.{WORKER_EPOCH}",
        {"status": "pending", "claimed_by": None, "claimed_at": None, "claim_epoch": None, "lease_until": None,
         "failed_on": tried, "updated_at": now_iso()},
        extra_headers={"Prefer": "return=representation"},
    )
    if not got:
        return False
    log(f"handed off request {req['id']} after failing here ({err}); untried and up: {left}")
    push_activity(f"Handing off from {WORKER_ID}", "tool")
    alert_failure("request", err, f"Request {req['id']} ({(req.get('transcript') or '')[:200]!r}) failed "
                  f"here before calling any tool and was handed to another worker (untried and up: {left}).")
    return True
