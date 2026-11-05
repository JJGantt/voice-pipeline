"""The read marker: what files an answer in the inbox, and whether it replaces the last one
(`[[read again]]`). The label is not the marker's: labels.py writes it."""
from voice_worker.prompts import read_again, take_read_marker


def test_marker_flags():
    assert take_read_marker("It's in theaters. [[read]]") == ("It's in theaters.", True)
    assert not read_again("x [[read]]")


def test_again_marker_flags_and_replaces():
    t = "Not streaming yet. [[read again]]"
    assert take_read_marker(t) == ("Not streaming yet.", True)
    assert read_again(t)


def test_label_in_marker_is_dropped():
    assert take_read_marker("Yes. [[read: Is it out?]]") == ("Yes.", True)
    assert read_again("Yes. [[read again: Is it out?]]")


def test_absent():
    assert take_read_marker("Done.") == ("Done.", False)
    assert not read_again("Done.")
