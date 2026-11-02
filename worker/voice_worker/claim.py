"""Claiming: sweeps, orphan recovery, holding while a mic hears the speaker, and the compare-and-swap claim."""

import time
from datetime import datetime, timezone, timedelta

from . import runstate
from .config import BUSY_WAIT_S, DEDUP_TOL_S, DEDUP_WINDOW_S, FAILOVER_S, FALLBACK_WORKER, HEARTBEAT_FRESH_S, MAX_HOLD_S, MIC_QUIET_S, RECLAIM_SWEEP_S, STANDBY_CHECK_S, UNTRANSCRIBED_MAX_S, WORKER_ID
from .db import Supabase, degraded
from .live import session_reset_s
from .logs import _parse_ts, alert_failure, log, log_error, now_iso
from .presence import WORKER_EPOCH, claude_unready, defer_target, other_worker_healthy, presence_rows, should_defer, untried_workers


_last_transcribing_sweep = 0.0


_last_reclaim_sweep = 0.0


_last_standby_check = 0.0


def refresh_standby(db: Supabase) -> None:
    """Re-evaluate standby on an IDLE cycle, rarely.

    `runstate._standby` only chooses this worker's poll cadence — the claim decision is made fresh in
    `claim_one` the moment something is actually pending, so a minute-stale flag here can cost nothing
    but a cycle at the wrong interval. Asking every cycle meant a `voice_requests` read (via
    `context_owner`) plus sometimes a `worker_presence` read, for ever, to learn nothing."""
    global _last_standby_check
    now_mono = time.monotonic()
    if now_mono - _last_standby_check < STANDBY_CHECK_S:
        return
    _last_standby_check = now_mono
    runstate._standby = should_defer(db)


def sweep_stuck_transcribing(db: Supabase) -> None:
    """Close rows stuck in 'transcribing' — the capturer opened the lifecycle row at capture-accept and
    then died before STT finished. The transcription budget is bounded (~61s: one 30s Groq call + one
    retry), so >90s in this state can only mean a dead capturer; erroring the row is what releases the
    working indicators (ESP blink + floor lamp) that turned on at accept. Throttled to one sweep/30s."""
    global _last_transcribing_sweep
    now_mono = time.monotonic()
    if now_mono - _last_transcribing_sweep < 30:
        return
    _last_transcribing_sweep = now_mono
    try:
        rows = db.get("voice_requests",
                      "status=eq.transcribing&select=id,created_at&order=created_at.asc&limit=5")
        now = datetime.now(timezone.utc)
        for r in rows or []:
            ts = _parse_ts(r.get("created_at") or "")
            if ts and (now - ts).total_seconds() > 90:
                db.patch("voice_requests", f"id=eq.{r['id']}&status=eq.transcribing",
                         {"status": "error", "error": "transcription never completed (capturer died)",
                          "updated_at": now_iso()})
                log(f"closed stuck transcribing request {r['id']}")
    except Exception as e:  # noqa: BLE001
        log_error(f"transcribing sweep failed: {e}")


def reclaim_stale(db: Supabase) -> None:
    """Release requests no live process is working on, so a worker takes them.

    An ORPHAN is a claimed row whose process is gone: its owner's heartbeat is stale, or the owner is up
    under a different `claim_epoch` — a worker that died mid-run and came back under the same name, which
    a heartbeat alone can never show. One that had not acted (`acted_at` empty) goes back to pending with
    the owner in `failed_on`, and the realtime UPDATE sends the next worker to it. One that HAD acted may
    be half-done, so it is not replayed: it ends as an error and is investigated. So does an orphan older
    than the conversation window, which would be stale to run.

    A STRANDED row is pending with every healthy worker already in `failed_on` — the one it was handed
    to died before claiming it. Its `failed_on` is cleared so everyone may try again; each failure hands
    on until none is left and the last one ends it, so this cannot loop.

    The sweep is a backstop on its own clock (RECLAIM_SWEEP_S, and once at startup), run whether or not
    anything is pending."""
    global _last_reclaim_sweep
    now_mono = time.monotonic()
    if _last_reclaim_sweep and now_mono - _last_reclaim_sweep < RECLAIM_SWEEP_S:
        return
    _last_reclaim_sweep = now_mono
    now = datetime.now(timezone.utc)
    presence = {r.get("worker"): r for r in presence_rows(db)}
    for req in db.get("voice_requests", "status=eq.claimed&order=created_at.asc&limit=20"
                      "&select=id,transcript,claimed_by,claim_epoch,claimed_at,created_at,acted_at,failed_on") or []:
        owner = req.get("claimed_by") or ""
        if owner == WORKER_ID:
            alive = req.get("claim_epoch") == WORKER_EPOCH
        else:
            p = presence.get(owner) or {}
            ts = _parse_ts(p.get("last_seen", ""))
            fresh = bool(ts) and (now - ts).total_seconds() <= HEARTBEAT_FRESH_S
            epoch = (p.get("info") or {}).get("epoch")
            claimed_at = _parse_ts(req.get("claimed_at") or "")
            # A different epoch proves the claimer died only if the owner has heartbeated SINCE the claim.
            # A presence row older than the claim (cached here up to PRESENCE_CACHE_S, or simply written
            # before a just-restarted process's first beat) still carries the previous process's epoch,
            # and would orphan a live claim the instant it was made.
            epoch_after_claim = bool(ts) and bool(claimed_at) and ts > claimed_at
            alive = fresh and (not req.get("claim_epoch") or not epoch or epoch == req.get("claim_epoch")
                               or not epoch_after_claim)
            if not alive and fresh is False and claimed_at and (now - claimed_at).total_seconds() <= HEARTBEAT_FRESH_S:
                continue   # stale heartbeat but a claim newer than the window: give the owner its chance
        if alive:
            continue
        guard = f"id=eq.{req['id']}&status=eq.claimed&claimed_by=eq.{owner}"
        created = _parse_ts(req.get("created_at") or "")
        what = f"Request {req['id']} ({(req.get('transcript') or '')[:200]!r}) was orphaned by '{owner}'"
        if req.get("acted_at") or (created and (now - created).total_seconds() > session_reset_s()):
            why = ("it had already acted, so it may be half-done and was not replayed" if req.get("acted_at")
                   else "it is too old to run now")
            if db.patch("voice_requests", guard,
                        {"status": "error", "error": f"orphaned: '{owner}' died mid-run; {why}", "updated_at": now_iso()},
                        extra_headers={"Prefer": "return=representation"}):
                log(f"closed orphaned request {req['id']} from '{owner}': {why}")
                alert_failure("orphan", f"'{owner}' died mid-run", f"{what}; {why}. It ended as an error.")
            continue
        if db.patch("voice_requests", guard,
                    {"status": "pending", "claimed_by": None, "claimed_at": None, "claim_epoch": None,
                     "failed_on": sorted(set(req.get("failed_on") or []) | {owner}), "updated_at": now_iso()},
                    extra_headers={"Prefer": "return=representation"}):
            log(f"released orphaned request {req['id']} from '{owner}' back to pending")
            alert_failure("orphan", f"'{owner}' died mid-run",
                          f"{what} before it acted, and was put back to pending for another worker.")
    for req in db.get("voice_requests", "status=eq.untranscribed&select=id,created_at,transcript") or []:
        created = _parse_ts(req.get("created_at") or "")
        if created and (now - created).total_seconds() > UNTRANSCRIBED_MAX_S:
            if db.patch("voice_requests", f"id=eq.{req['id']}&status=eq.untranscribed",
                        {"status": "error", "error": "every transcriber failed and the Mac did not take it",
                         "updated_at": now_iso()}, extra_headers={"Prefer": "return=representation"}):
                log(f"closed untranscribed request {req['id']}: nobody transcribed it")
                alert_failure("transcription", "every transcriber failed",
                              f"Request {req['id']}: Groq, OpenAI and the Mac's local Whisper all failed to "
                              f"produce a transcript within {UNTRANSCRIBED_MAX_S:.0f}s; it ended as an error.")
    for req in db.get("voice_requests", "status=eq.pending&failed_on=neq.{}&select=id,failed_on,created_at") or []:
        created = _parse_ts(req.get("created_at") or "")
        if created and (now - created).total_seconds() > FAILOVER_S and \
                not untried_workers(db, set(req.get("failed_on") or [])):
            if db.patch("voice_requests", f"id=eq.{req['id']}&status=eq.pending",
                        {"failed_on": [], "updated_at": now_iso()}, extra_headers={"Prefer": "return=representation"}):
                log(f"request {req['id']} was stranded (every healthy worker in failed_on) — cleared for a retry")


def _mics_hearing(db: Supabase):
    """The most recent moment ANY mic reported hearing the speaker, or None.

    `voice_presence.heard_jared_at` is already written by every capture source on each confirmed check
    — it drives the HUD and the ESP light. Nothing used it to decide WHEN TO ACT, which is what let a
    fragment run while another mic was still recording the same sentence.
    """
    stamps = [_parse_ts(r.get("heard_jared_at") or "")
              for r in db.get("voice_presence", "select=heard_jared_at")]
    stamps = [t for t in stamps if t]
    return max(stamps) if stamps else None


def _same_utterance(rows: list, first: dict) -> list:
    """The pending rows that are the SAME utterance as `first` — one spoken thing, several mics.

    Grown transitively over the union of recording windows, because that is the shape the failure had:
    the Mac cut one dictation into two fragments while the ESP held the whole 65 seconds, so the
    fragments only meet each other THROUGH the long window that contains them both.

    A row with no window (typed input) is nobody's duplicate and stands alone.
    """
    tol = timedelta(seconds=DEDUP_TOL_S)
    win = (_parse_ts(first.get("capture_start") or ""), _parse_ts(first.get("capture_end") or ""))
    if not (win[0] and win[1]):
        return [first]
    group, lo, hi = [first], win[0], win[1]
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r in group:
                continue
            s2, e2 = _parse_ts(r.get("capture_start") or ""), _parse_ts(r.get("capture_end") or "")
            if not (s2 and e2):
                continue
            if s2 <= hi + tol and lo <= e2 + tol:
                group.append(r)
                lo, hi = min(lo, s2), max(hi, e2)
                changed = True
    return group


def failover_wait(db: Supabase, waited_s: float, target: str) -> float:
    """Seconds still to leave a pending request to `target` before taking it; 0 means take it now.

    FAILOVER_S from its creation: a healthy target claims in about a second, so one that has not is not
    going to. Past that, the only reason to keep waiting is that `target` ITSELF is mid-run — a follow-up
    queues behind the run it follows — and only for BUSY_WAIT_S of that run, because a run that hangs
    would otherwise hold every command behind it for as long as it hangs. Another worker's run is no
    reason to wait at all."""
    if waited_s < FAILOVER_S:
        return FAILOVER_S - waited_s
    busy = db.get("voice_requests",
                  f"status=eq.claimed&claimed_by=eq.{target}&select=claimed_at&order=claimed_at.desc&limit=1")
    at = _parse_ts((busy or [{}])[0].get("claimed_at") or "") if busy else None
    if at:
        left = BUSY_WAIT_S - (datetime.now(timezone.utc) - at).total_seconds()
        if left > 0:
            return left
    log(f"pending {waited_s:.0f}s and {target} has not taken it — taking it")
    return 0.0


def claim_one(db: Supabase) -> dict | None:
    """Find one pending request and atomically claim it. Returns the row or None."""
    sweep_stuck_transcribing(db)

    # PENDING FIRST. Deferring is about who claims a request, so with nothing to claim the answer
    # changes nothing — and asking cost a voice_requests read on every idle cycle for ever. While idle
    # the standby flag is refreshed on its own slow clock, which is all its consumer (the poll cadence)
    # needs; the instant there IS something pending, it is decided fresh, before any claim.
    # The fallback worker reads `fallback` rows in the SAME query, so the watch's way in costs no extra
    # read per cycle. Every other worker's filter stays `pending`, which is what keeps them off it.
    statuses = "in.(pending,fallback)" if WORKER_ID == FALLBACK_WORKER else "eq.pending"
    pending = db.get(
        "voice_requests",
        f"status={statuses}&failed_on=not.cs.{{{WORKER_ID}}}&order=created_at.asc&limit=8"
        "&select=id,transcript,source,status,created_at,updated_at,capture_start,capture_end,failed_on",
    )
    fb = next((r for r in pending or [] if r.get("status") == "fallback"), None)
    if fb and runstate._running:
        # No deferring, holding or grouping: it came from one watch because the hub did not answer, and
        # nobody else will take it.
        got = db.patch("voice_requests", f"id=eq.{fb['id']}&status=eq.fallback",
                       {"status": "claimed", "claimed_by": WORKER_ID, "claim_epoch": WORKER_EPOCH,
                        "claimed_at": now_iso(), "assigned_to": WORKER_ID, "updated_at": now_iso()},
                       extra_headers={"Prefer": "return=representation"})
        return got[0] if got else None
    pending = [r for r in pending or [] if r.get("status") == "pending"]
    # SPOKEN ORDER, not arrival order. A watch that loses the hub holds its clips and sends them together
    # when it gets back, and the uploads land in whatever order they finish: a correction ("I said 11")
    # then runs before the command it corrects and guesses at it. A row with no capture window (typed)
    # sorts by when it arrived. Both are Postgres UTC timestamps, so they compare as strings.
    pending.sort(key=lambda r: r.get("capture_start") or r.get("created_at") or "")
    reclaim_stale(db)   # orphans and stranded rows, on its own clock, whether or not anything is pending
    if not pending:
        runstate._holding = False
        refresh_standby(db)
        return None

    # A host that cannot start a turn does not claim: it marks what is pending as tried here, one write
    # each, and that UPDATE is the realtime wake that sends the next worker to it. Claiming first and
    # handing off after cost 1.17s of reads on the Pi before the check was reached; the check is ~13µs.
    # A row no other healthy worker is left to try is claimed instead, and ends as an error the normal way.
    unready = claude_unready()
    if unready:
        passed = [r for r in pending
                  if untried_workers(db, set(r.get("failed_on") or []) | {WORKER_ID})]
        for r in passed:
            db.patch("voice_requests", f"id=eq.{r['id']}&status=eq.pending",
                     {"failed_on": sorted(set(r.get("failed_on") or []) | {WORKER_ID}), "updated_at": now_iso()})
        if passed:
            log(f"passed {len(passed)} pending request(s) on unclaimed: {unready}")
            alert_failure("request", unready, f"This host cannot start a turn, so it passed {len(passed)} "
                          f"pending request(s) to the other workers unclaimed: {[r['id'] for r in passed]}.")
        pending = [r for r in pending if r not in passed]
        if not pending:
            return None

    oldest_at = _parse_ts(min(r.get("created_at") or "" for r in pending))
    # A row handed on or released waits from when it became claimable again, not from its creation —
    # otherwise its preferred untried worker gets no head start and the lowest-ranked one races it.
    back_at = _parse_ts(pending[0].get("updated_at") or "") if pending[0].get("failed_on") else None
    since = max(t for t in (oldest_at, back_at) if t) if (oldest_at or back_at) else None
    waited_s = (datetime.now(timezone.utc) - since).total_seconds() if since else 0.0
    tried = set(pending[0].get("failed_on") or [])
    if degraded() and other_worker_healthy(db, tried):
        # My own link is failing: a claim here could take minutes to answer while a healthy worker sits idle.
        runstate._standby, runstate._failover_at = True, 0.0
        return None
    target = defer_target(db, tried)
    left = failover_wait(db, waited_s, target) if target else 0.0
    if left > 0:
        runstate._standby = True   # another worker should take it → stand down until its chance runs out
        runstate._failover_at = time.monotonic() + left + 0.5   # a clock arrival, not a poll
        return None
    runstate._standby, runstate._failover_at = False, 0.0   # I'm eligible to claim → run the active cadence

    # HOLD WHILE THE SPEAKER IS STILL TALKING. One utterance reaches the queue as several rows — mics endpoint
    # at different pauses — and acting on the first one to arrive is how half a sentence becomes a
    # command. Measured case: the Mac cut a 65-second dictation into two fragments and enqueued them
    # while the ESP was still recording; the fragments ran, and the ESP's complete 888-character
    # capture (the actual instruction) was then discarded as a duplicate of them.
    #
    # Waiting for every mic to go quiet costs nothing on a short command: by the time "turn the light
    # blue" is enqueued, no mic has heard anything for seconds, so it claims immediately.
    heard = _mics_hearing(db)
    held_s = (datetime.now(timezone.utc) - oldest_at).total_seconds() if oldest_at else 0.0
    if heard and (datetime.now(timezone.utc) - heard).total_seconds() < MIC_QUIET_S and held_s < MAX_HOLD_S:
        if not runstate._holding:
            log(f"holding {len(pending)} pending request(s) — a mic is still hearing him")
        runstate._holding = True
        return None
    runstate._holding = False

    # THE FULLEST CAPTURE WINS, not the first to arrive. Two mics produce two accounts of one
    # utterance; the longer one heard more of it. Deliberately never a merge of both: stitching two
    # transcripts risks saying a sentence twice inside a command, which is worse than losing a clause.
    #
    # (A real merge is the interesting version — the Mac hears the first half, the ESP the second, and
    # walking between rooms mid-sentence still yields one command. That needs the halves aligned by
    # their recording windows rather than concatenated, and is deliberately not attempted here.)
    group = _same_utterance(pending, pending[0])
    req = max(group, key=lambda r: len(r.get("transcript") or ""))
    if len(group) > 1:
        log(f"one utterance, {len(group)} captures — taking {req.get('source')}'s "
            f"({len(req.get('transcript') or '')} chars) over "
            f"{[(r.get('source'), len(r.get('transcript') or '')) for r in group if r is not req]}")
    rid = req["id"]

    # A stop signal can land anywhere in the reads above, and the handler only clears _running: without
    # this check the worker claims a command it has seconds to live for, and the next worker gets it only
    # after reclaim_stale declares this one dead.
    if not runstate._running:
        return None
    claimed = db.patch(
        "voice_requests",
        f"id=eq.{rid}&status=eq.pending",
        {
            "status": "claimed",
            "claimed_by": WORKER_ID,
            "claim_epoch": WORKER_EPOCH,
            "claimed_at": now_iso(),
            "assigned_to": WORKER_ID,
            "updated_at": now_iso(),
        },
        extra_headers={"Prefer": "return=representation"},
    )
    # Empty representation array => someone else won the race.
    if not claimed:
        return None
    # The other captures of the same utterance are retired HERE, before this one runs — not left
    # pending for the next iteration to pick up as a fresh command. Marked, never deleted: a bad
    # grouping should be visible in the queue rather than a command that silently vanished.
    for other in group:
        if other is req:
            continue
        db.patch("voice_requests", f"id=eq.{other['id']}&status=eq.pending",
                 {"status": "done", "result": SUPERSEDED_RESULT, "updated_at": now_iso()})
    return claimed[0]


# Results that mean the request executed NOTHING. A row like this must never be the surviving half of a
# two-mic pair: if one mic endpoints early it enqueues a mid-sentence fragment, the fragment is discarded
# as a false positive, and the OTHER mic's complete capture then arrives overlapping it — dedupe on
# arrival order alone drops the real command and the whole utterance disappears with nothing run.
# Skipping these is safe precisely because they did no work, so there is nothing to run twice.
SUPERSEDED_RESULT = "(superseded — a fuller capture of the same words ran)"


NO_WORK_RESULTS = ("(duplicate — dropped)", "(ignored: wake-word false positive)", SUPERSEDED_RESULT)


def _duplicate_of_recent(db: Supabase, req: dict) -> bool:
    """True if an earlier already-claimed/done request that ACTUALLY RAN covers the SAME RECORDING WINDOW
    as this one — i.e. a second always-on mic (ESP + Mac) heard the same utterance. Compares the actual
    [capture_start, capture_end] spans (overlap, each padded by DEDUP_TOL_S for clock skew + ESP stream
    latency), NOT enqueue time — so the pair always collapses regardless of transcription speed, while two
    genuinely distinct (sequential, non-overlapping) commands BOTH run even a second apart. Falls back to
    created_at proximity only for a pair where either side predates capture-window stamping."""
    rid = req.get("id")
    s, e = _parse_ts(req.get("capture_start") or ""), _parse_ts(req.get("capture_end") or "")
    ts = _parse_ts(req.get("created_at") or "")
    rows = db.get("voice_requests",
                  "select=id,created_at,status,result,capture_start,capture_end&order=created_at.desc&limit=12")
    tol = timedelta(seconds=DEDUP_TOL_S)
    for r in rows:
        if r.get("id") == rid or r.get("status") not in ("claimed", "done"):
            continue
        if (r.get("result") or "").strip() in NO_WORK_RESULTS:
            continue        # it ran nothing — it cannot stand in for this capture
        os_, oe = _parse_ts(r.get("capture_start") or ""), _parse_ts(r.get("capture_end") or "")
        if s and e and os_ and oe:
            if s <= oe + tol and os_ <= e + tol:   # padded window overlap → same utterance
                return True
        else:   # fallback: enqueue-time proximity for rows without a recording window
            ots = _parse_ts(r.get("created_at") or "")
            if ts and ots and 0 <= (ts - ots).total_seconds() <= DEDUP_WINDOW_S:
                return True
    return False
