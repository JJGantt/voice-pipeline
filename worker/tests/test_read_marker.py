"""The read marker: what files an answer in the inbox, under which label, and whether it replaces the
last one (`[[read again]]`)."""
from voice_worker.prompts import read_again, take_read_marker


def test_plain_marker_flags_with_label():
    assert take_read_marker("It's in theaters. [[read: Is Resident Evil streaming?]]") == (
        "It's in theaters.", True, "Is Resident Evil streaming?")
    assert not read_again("x [[read: Is it?]]")


def test_again_marker_flags_and_replaces():
    t = "Not streaming yet. [[read again: Is the new Resident Evil streaming?]]"
    assert take_read_marker(t) == ("Not streaming yet.", True, "Is the new Resident Evil streaming?")
    assert read_again(t)


def test_bare_and_absent():
    assert take_read_marker("Done. [[read]]") == ("Done.", True, None)
    assert take_read_marker("Done.") == ("Done.", False, None)
    assert not read_again("Done.")
