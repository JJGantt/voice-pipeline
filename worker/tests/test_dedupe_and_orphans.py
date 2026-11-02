"""Two behaviours that decide whether a spoken command runs exactly once.

Dedupe: an always-on capture whose recording window overlaps one that already RAN is the same utterance
heard by a second mic, and is dropped; two separate sentences a second apart both run; a row that ran
nothing can never stand in for one that would.

Orphans and hand-offs: a claim whose process is gone is released to another worker if it had not acted,
and closed as an error if it had (a half-done command is never replayed). A claim whose lease lapsed is
taken back the same way while its owner's heartbeat is fresh, and the worker that lost it neither acts
nor writes anything more for it.
"""

from fakes import ago


def row(h, **kw):
    base = dict(transcript="x", source="fly", status="pending", created_at=ago(5), updated_at=ago(5),
                failed_on=[], always_on=True)
    base.update(kw)
    return h.pg.insert("voice_requests", **base)


# ---- recording-window dedupe ----------------------------------------------------------------------------

def test_overlapping_window_that_ran_is_a_duplicate(load):
    h = load()
    row(h, status="done", result="Logged.", capture_start=ago(20), capture_end=ago(14))
    new = row(h, capture_start=ago(19), capture_end=ago(13.5))
    assert h.w._duplicate_of_recent(h.db, new) is True


def test_windows_within_clock_skew_tolerance_still_collapse(load):
    h = load()
    row(h, status="done", result="Logged.", capture_start=ago(20), capture_end=ago(14))
    new = row(h, capture_start=ago(13.5), capture_end=ago(10))   # starts 0.5s after the other ended
    assert h.w._duplicate_of_recent(h.db, new) is True


def test_sequential_sentences_both_run(load):
    h = load()
    row(h, status="done", result="Logged.", capture_start=ago(20), capture_end=ago(14))
    new = row(h, capture_start=ago(12), capture_end=ago(9))
    assert h.w._duplicate_of_recent(h.db, new) is False


def test_a_row_that_ran_nothing_never_stands_in(load):
    h = load()
    for result in ("(ignored: wake-word false positive)", "(duplicate — dropped)",
                   "(superseded — a fuller capture of the same words ran)"):
        h.pg.tables["voice_requests"] = []
        row(h, status="done", result=result, capture_start=ago(20), capture_end=ago(14))
        new = row(h, capture_start=ago(19), capture_end=ago(13))
        assert h.w._duplicate_of_recent(h.db, new) is False, result


def test_rows_without_windows_fall_back_to_arrival_proximity(load):
    h = load()
    row(h, status="done", result="ok", created_at=ago(8))
    assert h.w._duplicate_of_recent(h.db, row(h, created_at=ago(2))) is True
    h.pg.tables["voice_requests"] = []
    row(h, status="done", result="ok", created_at=ago(30))
    assert h.w._duplicate_of_recent(h.db, row(h, created_at=ago(2))) is False


def test_process_request_drops_an_always_on_duplicate_without_running_it(load):
    h = load()
    sdk = h.install_runners()
    row(h, status="done", result="Logged.", capture_start=ago(20), capture_end=ago(14))
    new = row(h, status="claimed", claimed_by="fly", claim_epoch=h.w.WORKER_EPOCH, capture_start=ago(19), capture_end=ago(13))
    h.w.process_request(h.db, dict(new))
    assert h.pg.get("voice_requests", new["id"])["result"] == "(duplicate — dropped)"
    assert sdk.calls == []


def test_a_pressed_command_is_never_deduped(load):
    h = load()
    sdk = h.install_runners()
    row(h, status="done", result="Logged.", capture_start=ago(20), capture_end=ago(14))
    new = row(h, status="claimed", claimed_by="fly", claim_epoch=h.w.WORKER_EPOCH, source="watch-cmd", always_on=False,
              capture_start=ago(19), capture_end=ago(13))
    h.w.process_request(h.db, dict(new))
    assert len(sdk.calls) == 1


# ---- orphans, stranded rows, hand-off ---------------------------------------------------------------

def test_orphan_that_had_not_acted_goes_back_to_pending_for_someone_else(load):
    h = load(WORKER_ID="pi")
    h.beat("fly", age_s=500)
    h.beat("pi")
    r = row(h, status="claimed", claimed_by="fly", claim_epoch="old", claimed_at=ago(200), created_at=ago(200))
    h.w.reclaim_stale(h.db)
    stored = h.pg.get("voice_requests", r["id"])
    assert stored["status"] == "pending" and stored["failed_on"] == ["fly"] and stored["claimed_by"] is None


def test_orphan_that_had_acted_is_closed_never_replayed(load):
    h = load(WORKER_ID="pi")
    h.beat("fly", age_s=500)
    r = row(h, status="claimed", claimed_by="fly", claimed_at=ago(200), created_at=ago(200), acted_at=ago(190))
    h.w.reclaim_stale(h.db)
    assert h.pg.get("voice_requests", r["id"])["status"] == "error"


def test_a_restarted_process_recognises_its_predecessors_claim_as_orphaned(load):
    h = load(WORKER_ID="fly")
    r = row(h, status="claimed", claimed_by="fly", claim_epoch="previous-process", claimed_at=ago(5), created_at=ago(6))
    h.w.reclaim_stale(h.db)
    assert h.pg.get("voice_requests", r["id"])["status"] == "pending"


def test_a_live_claim_is_left_alone(load):
    h = load(WORKER_ID="pi")
    h.beat("fly", epoch="e1")
    r = row(h, status="claimed", claimed_by="fly", claim_epoch="e1", claimed_at=ago(20), created_at=ago(21))
    h.w.reclaim_stale(h.db)
    assert h.pg.get("voice_requests", r["id"])["status"] == "claimed"


def test_a_brand_new_claim_with_a_late_heartbeat_gets_its_chance(load):
    h = load(WORKER_ID="pi")
    h.beat("fly", age_s=120)
    r = row(h, status="claimed", claimed_by="fly", claimed_at=ago(10), created_at=ago(11))
    h.w.reclaim_stale(h.db)
    assert h.pg.get("voice_requests", r["id"])["status"] == "claimed"


def test_stranded_row_is_cleared_for_a_retry(load):
    h = load(WORKER_ID="pi")
    h.beat("fly")
    r = row(h, failed_on=["fly", "pi"], created_at=ago(30))
    h.w.reclaim_stale(h.db)
    assert h.pg.get("voice_requests", r["id"])["failed_on"] == []


def test_hand_off_goes_only_to_a_healthy_untried_worker(load):
    h = load(WORKER_ID="fly")
    r = row(h, status="claimed", claimed_by="fly", claim_epoch=h.w.WORKER_EPOCH)
    assert h.w.hand_off(h.db, dict(r), "boom") is False          # nobody else is up
    h.beat("pi")
    assert h.w.hand_off(h.db, dict(r), "boom") is True
    stored = h.pg.get("voice_requests", r["id"])
    assert stored["status"] == "pending" and stored["failed_on"] == ["fly"]


# ---- the lease --------------------------------------------------------------------------------------------

def test_a_lapsed_lease_is_taken_back_while_the_owners_heartbeat_is_fresh(load):
    """A process hung mid-turn: its heartbeat thread beats on, its turn renews nothing."""
    h = load(WORKER_ID="pi")
    h.beat("fly", epoch="e1")
    h.beat("pi")
    r = row(h, status="claimed", claimed_by="fly", claim_epoch="e1", claimed_at=ago(150), created_at=ago(150),
            lease_until=ago(10))
    h.w.reclaim_stale(h.db)
    stored = h.pg.get("voice_requests", r["id"])
    assert stored["status"] == "pending" and stored["failed_on"] == ["fly"] and stored["lease_until"] is None


def test_a_live_lease_keeps_the_claim(load):
    h = load(WORKER_ID="fly")
    r = row(h, source="watch-cmd", always_on=False)
    got = h.w.claim_one(h.db)
    assert got["id"] == r["id"] and h.pg.get("voice_requests", r["id"])["lease_until"]
    h2 = load(WORKER_ID="pi")
    h2.pg.tables = h.pg.tables
    h2.beat("fly", epoch=h.w.WORKER_EPOCH)
    h2.w.reclaim_stale(h2.db)
    assert h.pg.get("voice_requests", r["id"])["status"] == "claimed"


def test_a_worker_that_lost_its_claim_mid_turn_acts_on_nothing_and_writes_nothing(load):
    h = load(WORKER_ID="fly")
    sdk = h.install_runners(sdk=h.runner(tools=[("mcp__data__log_nutrition", {"items": []})], reply="Logged."))
    r = row(h, source="watch-cmd", always_on=False)
    req = h.w.claim_one(h.db)

    renewals = []

    def taken_back(method, table, query):   # the sweep releases the row after the attempt starts, before the tool
        if method == "PATCH" and table == "voice_requests" and "claim_epoch=eq." in query:
            renewals.append(query)
            if len(renewals) == 2:
                h.pg.get("voice_requests", r["id"]).update(status="pending", claimed_by=None, claim_epoch=None)
    h.pg.before.append(taken_back)
    h.w.process_request(h.db, req)
    assert sdk.calls[-1]["decisions"]["mcp__data__log_nutrition"] == "deny"
    assert h.pg.rows("voice_messages") == []
    stored = h.pg.get("voice_requests", r["id"])
    assert stored["status"] == "pending" and not stored.get("acted_at")

