"""The settings backstop: a poll that sees drift counts it as a realtime miss only if the drift is still
there after the grace period, because the poll can read a write before its realtime event arrives."""

import asyncio
import os
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "shared"))
os.environ.update({"GROQ_API_KEY": "x", "LED_TOKEN": "led-secret", "VERIFY_TOKEN": "verify-secret",
                   "VOICE_DEVICE_TOKEN": "device-secret", "SUPABASE_SERVICE_KEY": "k",
                   "SUPABASE_URL": "http://supabase.test", "OPS_TOKEN": "", "OPS_URLS": "http://127.0.0.1:9"})
from voice_server import modes, settings_sync  # noqa: E402


def run(monkeypatch, caplog, first, second, during_grace=None):
    modes.state.suppressed = False
    monkeypatch.setattr(settings_sync, "RECONCILE_GRACE_S", 0)
    monkeypatch.setattr(settings_sync.db, "get_settings", lambda keys: second)

    async def go():
        task = asyncio.create_task(settings_sync._reconcile(first))
        await asyncio.sleep(0)
        if during_grace:
            during_grace()
        await task
    with caplog.at_level("WARNING", logger="voice"):
        asyncio.run(go())
    return [r for r in caplog.records if "realtime missed" in r.getMessage()]


def test_no_drift_is_quiet(monkeypatch, caplog):
    assert run(monkeypatch, caplog, {"voice_muted": "0"}, {"voice_muted": "0"}) == []


def test_realtime_delivering_during_grace_is_not_a_miss(monkeypatch, caplog):
    deliver = lambda: modes.apply_setting("voice_muted", "1")   # noqa: E731
    assert run(monkeypatch, caplog, {"voice_muted": "1"}, {"voice_muted": "1"}, deliver) == []
    assert modes.state.suppressed is True


def test_value_flipped_back_during_grace_is_not_a_miss(monkeypatch, caplog):
    assert run(monkeypatch, caplog, {"voice_muted": "1"}, {"voice_muted": "0"}) == []
    assert modes.state.suppressed is False


def test_drift_that_persists_is_a_miss_and_applied(monkeypatch, caplog):
    assert len(run(monkeypatch, caplog, {"voice_muted": "1"}, {"voice_muted": "1"})) == 1
    assert modes.state.suppressed is True
