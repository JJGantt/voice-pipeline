"""Two behaviours that decide whether a spoken command runs exactly once.

Dedupe: an always-on capture whose recording window overlaps one that already RAN is the same utterance
heard by a second mic, and is dropped; two separate sentences a second apart both run; a row that ran
nothing can never stand in for one that would.

Orphans and hand-offs: a claim whose process is gone is released to another worker if it had not acted,
and closed as an error if it had (a half-done command is never replayed).
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
    new = row(h, status="claimed", claimed_by="fly", capture_start=ago(19), capture_end=ago(13))
    h.w.process_request(h.db, dict(new))
    assert h.pg.get("voice_requests", new["id"])["result"] == "(duplicate — dropped)"
    assert sdk.calls == []


def test_a_pressed_command_is_never_deduped(load):
    h = load()
    sdk = h.install_runners()
    row(h, status="done", result="Logged.", capture_start=ago(20), capture_end=ago(14))
    new = row(h, status="claimed", claimed_by="fly", source="watch-cmd", always_on=False,
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
    r = row(h, status="claimed", claimed_by="fly")
    assert h.w.hand_off(h.db, dict(r), "boom") is False          # nobody else is up
    h.beat("pi")
    assert h.w.hand_off(h.db, dict(r), "boom") is True
    stored = h.pg.get("voice_requests", r["id"])
    assert stored["status"] == "pending" and stored["failed_on"] == ["fly"]
