"""Every test gets a freshly imported worker, configured by environment exactly as a real host is, with
Supabase answered by FakePostgrest and Claude by the stand-ins in fakes.py."""

import json
import os
import pathlib
import sys

import httpx
import pytest

HERE = pathlib.Path(__file__).resolve().parent
WORKER_DIR = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(WORKER_DIR))

from fakes import FakePostgrest, SUPABASE_HOST, scripted_runner_class  # noqa: E402

_RealClient = httpx.Client


def _purge_worker_modules():
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if f.startswith(str(WORKER_DIR)) and not f.startswith(str(HERE)):
            del sys.modules[name]


class Harness:
    def __init__(self, w, pg, claude_log):
        self.w, self.pg, self._claude_log = w, pg, claude_log
        self.db = w.Supabase(_RealClient(transport=pg.transport, timeout=5))

    def setall(self, name, value):
        """Rebind `name` in every worker module that has it (the worker may be one file or several)."""
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None) or ""
            if f.startswith(str(WORKER_DIR)) and not f.startswith(str(HERE)) and hasattr(mod, name):
                setattr(mod, name, value)

    def runner(self, tools=(), reply="Done.", ok=True, **kw):
        return scripted_runner_class(self.w.SdkRunner)(tools=tools, reply=reply, ok=ok, **kw)

    def install_runners(self, sdk=None, credits=None, fast=None):
        self.sdk = sdk or self.runner()
        self.credits = credits or self.runner(credits=True)
        self.fast = fast or self.runner(fast=True)
        self.setall("SDK", self.sdk)
        self.setall("SDK_CREDITS", self.credits)
        self.setall("SDK_FAST", self.fast)
        return self.sdk

    def claude_calls(self) -> list:
        if not self._claude_log.exists():
            return []
        return [json.loads(line) for line in self._claude_log.read_text().splitlines()]

    def beat(self, worker, age_s=1.0, **info):
        from fakes import ago
        rows = self.pg.rows("worker_presence")
        rows[:] = [r for r in rows if r["worker"] != worker]
        rows.append({"worker": worker, "last_seen": ago(age_s), "info": info})
        self.setall("_presence_cache", {"rows": None, "at": 0.0})


@pytest.fixture
def load(monkeypatch, tmp_path):
    """load(WORKER_ID="pi", ...) -> Harness around a worker imported fresh with that environment."""
    def _load(**env):
        pg = FakePostgrest()
        claude_log = tmp_path / "claude-argv.jsonl"
        base = {
            "SUPABASE_URL": f"http://{SUPABASE_HOST}", "SUPABASE_KEY": "service-key",
            "WORKER_ID": "fly", "WORKER_PRIORITY": "fly,pi,mac", "FALLBACK_WORKER": "pi",
            "STATE_DIR": str(tmp_path / "state"), "CLAUDE_BIN": str(HERE / "fake_claude.py"),
            "FAKE_CLAUDE_LOG": str(claude_log), "LED_BASE_URL": "", "HUB_TOKEN": "",
            "ORCH_URL": "http://orchestrator.test", "ORCH_TOKEN": "orch-token",
            "CODEX_BIN": "/nonexistent/codex", "CODEX_HOME": str(tmp_path / "codex"),
            "MESSAGES_MCP_URL": "", "REALTIME": "0", "OPS_TOKEN": "", "OPS_URLS": "http://127.0.0.1:9",
            "PETLIBRO_MCP_PY": "/nonexistent", "GMAIL_MCP_PY": "/nonexistent",
            "OPS_SPOOL": str(tmp_path / "ops-spool.jsonl"), "ANTHROPIC_API_KEY": "", "MIC_QUIET_S": "2.5", "MAX_HOLD_S": "25",
        }
        base.update({k: str(v) for k, v in env.items()})
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        client = _RealClient(transport=pg.transport, timeout=5)
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: _RealClient(*a, transport=pg.transport,
                                                                         **{x: y for x, y in k.items() if x != "transport"}))
        for verb in ("get", "post", "patch", "put", "delete"):
            monkeypatch.setattr(httpx, verb, getattr(client, verb))
        _purge_worker_modules()
        import worker  # noqa: F401
        h = Harness(sys.modules["worker"], pg, claude_log)
        pg.tables.setdefault("voice_requests", [])
        pg.tables.setdefault("worker_presence", [])
        pg.tables.setdefault("voice_presence", [])
        pg.tables.setdefault("app_settings", [])
        pg.tables.setdefault("voice_focus", [{"id": 1, "focus": "hub"}])
        pg.tables.setdefault("voice_messages", [])
        return h
    yield _load
    _purge_worker_modules()
