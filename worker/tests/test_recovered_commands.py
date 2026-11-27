"""Commands the hub recovered from a recording that broke partway (timing.cut_off), and the full clip of
one that arrives after its recovery (timing.supersedes).

Rule: such a command reaches the model framed as possibly cut off and possibly already repeated, so it
asks rather than acts on a fragment, and never does the same thing twice. Every other command carries no
such framing.
"""

from fakes import ago


def claimed(h, timing, source="watch-cmd", transcript="log 10 squats at"):
    row = dict(transcript=transcript, source=source, always_on=False, status="claimed", timing=timing,
               claimed_by=h.w.WORKER_ID, claim_epoch=h.w.WORKER_EPOCH, created_at=ago(3), updated_at=ago(3), failed_on=[])
    return dict(h.pg.insert("voice_requests", **row))


def prompt_for(load, timing, **kw):
    h = load(WORKER_ID="pi")
    sdk = h.install_runners()
    h.w.process_request(h.db, claimed(h, timing, **kw))
    assert len(sdk.calls) == 1
    return sdk.calls[0]["prompt"]


def test_a_recovered_watch_command_is_framed_as_possibly_cut_off_and_repeated(load):
    p = prompt_for(load, {"audio_via": "stream-recovered", "cut_off": {"verdict": "stalled", "held_s": 19.6, "view": "chat"}})
    assert "DID NOT ARRIVE THE NORMAL WAY" in p
    assert "His watch was recording it" in p and "19.6 seconds" in p
    assert "may already have said it again" in p and "ask whether there was more" in p
    assert "session screen" not in p


def test_a_recovered_follow_up_says_its_session_is_unknown(load):
    p = prompt_for(load, {"cut_off": {"verdict": "lost", "held_s": 4, "view": "session"}})
    assert "session screen" in p and "ask him which" in p


def test_a_recovered_phone_command_names_the_phone(load):
    p = prompt_for(load, {"cut_off": {"verdict": "silent", "held_s": 3.2}}, source="phone-cmd")
    assert "His phone was recording it" in p


def test_a_recording_the_phone_stopped_itself_is_framed_as_cut_off_not_late(load):
    p = prompt_for(load, {"cut_off": {"verdict": "stopped", "why": "stream", "held_s": 7}}, source="phone-cmd")
    assert "CUT OFF" in p and "after 7 seconds" in p and "the hub stopped acknowledging it" in p
    assert "ask him to finish it" in p
    assert "DID NOT ARRIVE THE NORMAL WAY" not in p and "may already have said it again" not in p


def test_the_full_clip_after_a_recovery_supersedes_it(load):
    p = prompt_for(load, {"audio_via": "upload", "supersedes": "abc-123"})
    assert "COMPLETE recording" in p and "abc-123" in p and "never anything twice" in p
    assert "DID NOT ARRIVE THE NORMAL WAY" not in p


def test_an_ordinary_command_carries_neither(load):
    p = prompt_for(load, {"audio_via": "live"})
    assert "DID NOT ARRIVE THE NORMAL WAY" not in p and "COMPLETE recording" not in p
