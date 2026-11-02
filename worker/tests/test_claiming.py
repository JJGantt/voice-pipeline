"""Claiming: who takes a pending command, when, and that only one worker ever does.

The priority chain is fly > pi > mac. A lower worker leaves a request to a healthy higher one for
FAILOVER_S, longer only while that worker is busy (up to BUSY_WAIT_S), and never to one that is stale,
degraded or stopping. The claim itself is a compare-and-swap on status=pending.
"""

from fakes import ago


def pending(h, transcript="log a banana", age=20.0, **kw):
    row = dict(transcript=transcript, source="watch-cmd", status="pending", created_at=ago(age),
               updated_at=ago(age), failed_on=[], always_on=False)
    row.update(kw)
    return h.pg.insert("voice_requests", **row)


def status(h, row):
    return h.pg.get("voice_requests", row["id"])["status"]


# ---- the claim itself ---------------------------------------------------------------------------------

def test_primary_claims_and_stamps_its_process(load):
    h = load(WORKER_ID="fly")
    row = pending(h)
    got = h.w.claim_one(h.db)
    assert got["id"] == row["id"]
    stored = h.pg.get("voice_requests", row["id"])
    assert stored["status"] == "claimed" and stored["claimed_by"] == "fly"
    assert stored["claim_epoch"] == h.w.WORKER_EPOCH


def test_claim_is_compare_and_swap(load):
    """Another worker claiming between this one's read and its write wins; this one gets nothing."""
    h = load(WORKER_ID="fly")
    row = pending(h)

    def steal(method, table, query):
        if method == "PATCH" and table == "voice_requests" and "status=eq.pending" in query:
            h.pg.get("voice_requests", row["id"]).update(status="claimed", claimed_by="pi")
    h.pg.before.append(steal)
    assert h.w.claim_one(h.db) is None
    assert h.pg.get("voice_requests", row["id"])["claimed_by"] == "pi"


def test_a_worker_never_reclaims_what_failed_on_it(load):
    h = load(WORKER_ID="fly")
    row = pending(h, failed_on=["fly"])
    assert h.w.claim_one(h.db) is None
    assert status(h, row) == "pending"


# ---- status `fallback` belongs to the Pi alone --------------------------------------------------------

def test_fallback_rows_are_claimed_only_by_the_fallback_worker(load):
    h = load(WORKER_ID="fly")
    row = pending(h, status="fallback", source="watch-fallback")
    assert h.w.claim_one(h.db) is None
    assert status(h, row) == "fallback"

    h = load(WORKER_ID="pi")
    row = pending(h, status="fallback", source="watch-fallback", age=0.1)
    h.beat("fly")   # a healthy primary does not hold a fallback back: nobody else will take it
    got = h.w.claim_one(h.db)
    assert got and got["id"] == row["id"] and status(h, row) == "claimed"


# ---- failover priority ----------------------------------------------------------------------------------

def test_lower_worker_defers_to_a_healthy_primary_at_first(load):
    h = load(WORKER_ID="pi")
    h.beat("fly")
    row = pending(h, age=1.0)
    assert h.w.claim_one(h.db) is None
    assert status(h, row) == "pending"
    assert h.w._standby is True and h.w._failover_at > 0   # sleeps to exactly the failover moment


def test_lower_worker_takes_it_once_an_idle_primary_has_had_its_chance(load):
    h = load(WORKER_ID="pi")
    h.beat("fly")
    row = pending(h, age=h.w.FAILOVER_S + 2)
    assert h.w.claim_one(h.db)["id"] == row["id"]


def test_a_busy_primary_keeps_its_follow_ups_for_a_bounded_time(load):
    h = load(WORKER_ID="pi")
    h.beat("fly")
    pending(h, transcript="earlier", status="claimed", claimed_by="fly", claimed_at=ago(30))
    row = pending(h, age=h.w.FAILOVER_S + 2)
    assert h.w.claim_one(h.db) is None          # queued behind fly's run
    h.pg.rows("voice_requests")[0]["claimed_at"] = ago(h.w.BUSY_WAIT_S + 5)
    assert h.w.claim_one(h.db)["id"] == row["id"]   # a run that long is a hang: take it


def test_primary_that_is_stale_degraded_or_stopping_is_not_waited_for(load):
    for info, age in (({}, 200.0), ({"degraded": True}, 1.0), ({"stopping": True}, 1.0)):
        h = load(WORKER_ID="pi")
        h.beat("fly", age_s=age, **info)
        row = pending(h, age=0.5)
        assert h.w.claim_one(h.db)["id"] == row["id"], info


def test_mac_defers_to_the_pi_when_fly_is_gone(load):
    h = load(WORKER_ID="mac")
    h.beat("pi")
    pending(h, age=0.5)
    assert h.w.claim_one(h.db) is None
    h = load(WORKER_ID="mac")
    pending(h, age=0.5)                          # nobody above is beating
    assert h.w.claim_one(h.db) is not None


def test_a_request_another_worker_failed_skips_straight_to_an_untried_one(load):
    h = load(WORKER_ID="pi")
    h.beat("fly")
    row = pending(h, age=0.5, updated_at=ago(20), failed_on=["fly"])
    assert h.w.claim_one(h.db)["id"] == row["id"]


def test_a_degraded_worker_stands_down_while_a_healthy_one_exists(load):
    h = load(WORKER_ID="fly")
    h.beat("pi")
    h.setall("_net_fail_at", __import__("time").monotonic())
    pending(h)
    assert h.w.claim_one(h.db) is None and h.w._standby is True


def test_a_host_that_cannot_run_claude_passes_requests_on_unclaimed(load):
    h = load(WORKER_ID="fly", CLAUDE_BIN="/nonexistent/claude")
    h.beat("pi")
    row = pending(h)
    assert h.w.claim_one(h.db) is None
    stored = h.pg.get("voice_requests", row["id"])
    assert stored["status"] == "pending" and stored["failed_on"] == ["fly"]
    # With nobody left to try it, it is claimed here and ends as an error the normal way.
    h = load(WORKER_ID="fly", CLAUDE_BIN="/nonexistent/claude")
    row = pending(h)
    assert h.w.claim_one(h.db)["id"] == row["id"]


# ---- spoken order, holding, and one utterance heard by several mics -----------------------------------

def test_requests_run_in_spoken_order_not_arrival_order(load):
    h = load(WORKER_ID="fly")
    later = pending(h, transcript="I said 11", age=20, capture_start=ago(30), capture_end=ago(28))
    first = pending(h, transcript="set a timer for 10", age=19, capture_start=ago(60), capture_end=ago(57))
    assert h.w.claim_one(h.db)["id"] == first["id"]
    assert status(h, later) == "pending"


def test_holds_while_a_mic_still_hears_him_but_never_for_ever(load):
    h = load(WORKER_ID="fly")
    h.pg.insert("voice_presence", source="esp", heard_jared_at=ago(0.5))
    row = pending(h, age=3)
    assert h.w.claim_one(h.db) is None and h.w._holding is True
    h.pg.get("voice_requests", row["id"])["created_at"] = ago(h.w.MAX_HOLD_S + 1)
    assert h.w.claim_one(h.db)["id"] == row["id"]


def test_fullest_capture_of_one_utterance_wins_and_the_rest_are_retired(load):
    h = load(WORKER_ID="fly")
    frag = pending(h, transcript="add the calf", source="mac", always_on=True,
                   capture_start=ago(20), capture_end=ago(15))
    full = pending(h, transcript="add the calf raises at 14 reps to today", source="fly", always_on=True,
                   capture_start=ago(20.4), capture_end=ago(12))
    other = pending(h, transcript="turn the light blue", capture_start=ago(8), capture_end=ago(6))
    assert h.w.claim_one(h.db)["id"] == full["id"]
    f = h.pg.get("voice_requests", frag["id"])
    assert f["status"] == "done" and f["result"] == h.w.SUPERSEDED_RESULT
    assert status(h, other) == "pending"          # a separate sentence is not swallowed


def test_same_utterance_grows_through_a_window_that_contains_both_fragments(load):
    h = load()
    a = {"capture_start": ago(60), "capture_end": ago(40), "transcript": "first half"}
    b = {"capture_start": ago(30), "capture_end": ago(10), "transcript": "second half"}
    whole = {"capture_start": ago(62), "capture_end": ago(9), "transcript": "the whole dictation"}
    typed = {"transcript": "typed"}
    assert len(h.w._same_utterance([a, b, whole, typed], a)) == 3
    assert h.w._same_utterance([a, typed], typed) == [typed]
