#!/usr/bin/env python3
"""Headless voice-command worker daemon.

Claims voice commands from a Supabase queue (`voice_requests`) and runs each one
through a headless `claude -p` session on the Claude *subscription* (no API key),
with the `data` MCP attached, then writes the conversation back to Supabase
(`voice_messages`) for a live display.

The same code runs on the Mac (WORKER_ID=mac) and later the Pi (WORKER_ID=pi).
Only ordering of WORKER_PRIORITY differs; the highest-priority worker that has a
fresh heartbeat wins each request (the others defer).

Phase 1 only handles requests when `voice_focus.focus == 'hub'` (the shared "hub"
conversation). When focus points at a specific Mac session id, that's a Phase-2
feature — for now such requests are left untouched (released back to pending) and
skipped, so this worker never crashes on them. See README for the Phase-2 TODO.

Design notes:
- Serialized: exactly one request is processed at a time, never concurrent.
- Defensive: every Supabase call is wrapped so a transient HTTP error logs and
  is swallowed rather than killing the loop.
- The hub `claude` session is reused via `--resume` and reset on a 2h idle gap or
  a calendar-day rollover (see should_reset_session()).
"""

import asyncio
import difflib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone, date
from pathlib import Path

import httpx

# --------------------------------------------------------------------------- #
# Config (env, with sensible defaults)
# --------------------------------------------------------------------------- #

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co"
).rstrip("/")

# REQUIRED: the service_role key (bypasses RLS — needs full read/write on the
# voice_* tables and worker_presence). There is intentionally no hardcoded
# default; see README.
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

WORKER_ID = os.environ.get("WORKER_ID", "mac")
# Ordered highest-priority first. A worker only claims if no HIGHER-priority
# worker has a fresh heartbeat.
WORKER_PRIORITY = [
    w.strip() for w in os.environ.get("WORKER_PRIORITY", "mac,pi").split(",") if w.strip()
]

STATE_DIR = Path(os.path.expanduser(os.environ.get("STATE_DIR", "~/.voice-worker")))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Path to the built data MCP server (node entrypoint).
DATA_MCP_JS = os.environ.get(
    "DATA_MCP_JS", "/Users/jaredgantt/repos/data-layer/dist/mcp.js"
)

# Path to the voice-control MCP (set_focus / list_sessions) so the hub session can
# switch what the wall is focused on ("switch to the nutrition session").
VOICE_MCP_JS = os.environ.get(
    "VOICE_MCP_JS", "/Users/jaredgantt/repos/voice-pipeline/mcp-voice/src/index.js"
)

# Govee lights MCP so voice can control the lights ("turn the lights blue").
GOVEE_PY = os.environ.get("GOVEE_PY", "/opt/homebrew/bin/python3.11")
GOVEE_MCP_PY = os.environ.get("GOVEE_MCP_PY", "/Users/jaredgantt/repos/govee-mcp/govee_mcp.py")

# Low-latency activity push to the status-hub server: transient "what Jarvis is doing" events go
# straight to the SSE clients (bypassing Supabase realtime) so the on-screen activity bar tracks
# tool calls in real time. Authenticated by the hub's kiosk token in the URL.
HUB_PUSH_URL = os.environ.get(
    "HUB_PUSH_URL",
    "https://jared-status-hub.fly.dev/api/voice-activity?token=REDACTED",
)


def push_activity(text: str, state: str = "tool") -> None:
    """Fire a transient activity event to the hub (best-effort; never raises)."""
    if not HUB_PUSH_URL:
        return
    try:
        httpx.post(HUB_PUSH_URL, json={"text": text, "state": state, "focus": "hub"}, timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


# After an ESP-sourced ('fly') request finishes, tell the voice server so the ESP light goes from
# 'working' (blink) → off. The server holds the ESP socket; we just hit its /led-off HTTP route.
LED_TOKEN = os.environ.get("LED_TOKEN", "")
LED_PING_URL = os.environ.get("LED_PING_URL", "https://jared-voice.fly.dev/led-off")


def ping_esp_led_off() -> None:
    if not LED_PING_URL:
        return
    try:
        httpx.get(LED_PING_URL, params={"token": LED_TOKEN} if LED_TOKEN else None, timeout=3.0)
    except Exception:  # noqa: BLE001
        pass

# Orchestrator (Mac) — when focus is a specific Mac session, the transcript is
# injected into that session's wall terminal via the orchestrator instead of
# running a fresh `claude -p`.
ORCH_URL = os.environ.get("ORCH_URL", "http://localhost:7682")
ORCH_TOKEN = os.environ.get("ORCH_TOKEN", "")

# Timing
HEARTBEAT_INTERVAL_S = 10.0
IDLE_POLL_S = 1.0
HEARTBEAT_FRESH_S = 30.0      # a higher-priority worker counts as "present" within this
SESSION_IDLE_RESET_S = 2 * 60 * 60  # reset hub session after 2h of inactivity
HTTP_TIMEOUT_S = 30.0
CLAUDE_TIMEOUT_S = float(os.environ.get("CLAUDE_TIMEOUT_S", "300"))

HUB_SESSION_FILE = STATE_DIR / "hub_session.json"
# Sentinel the hub UI enqueues to force a fresh Claude session (manual "Reset context").
# Must match VOICE_RESET_SENTINEL in the status-hub server.
RESET_SENTINEL = "__RESET_CONTEXT__"

# Quick-add requests (source 'web-quick', from the hub's bottom-right mic FAB) show their reply in a
# tiny status chip that holds only a few words before it fades. Append this directive to the turn so
# Jarvis still does the work but confirms it in one short phrase. Only the directive-free transcript
# is shown as the user's bubble; this text is invisible to the conversation.
QUICK_REPLY_DIRECTIVE = (
    "\n\n[Reply format for THIS turn only: this came from the quick-add voice button, whose reply shows "
    "in a tiny status chip for a couple seconds. Do the task, then confirm in ONE phrase of 3–7 words "
    "naming what was done — e.g. \"Lunch bagel logged\", \"Weight logged: 178\", \"Lights set to blue\", "
    "\"Added milk to the list\". No greeting, no explanation, no follow-up question. If it failed, say so "
    "in under 7 words.]"
)

# Sentinel the model emits to silently drop a turn (wake-word false positive). Matched loosely below.
IGNORE_SENTINEL = "__IGNORE__"

# Always-on voice listeners (ESP, Mac) occasionally false-trigger on noise/silence, so the transcript
# can be a mishear, not a real request (a stray word, or a Whisper artifact like "thank you" from
# silence). Appended to transcribed turns so the model can drop obvious noise instead of earnestly
# answering it. Transcribed sources only — typed input is intentional and never gets this.
WAKE_NOISE_DIRECTIVE = (
    "\n\n[This text came from an always-on voice wake-word listener that frequently FALSE-TRIGGERS on "
    "background noise or silence, so it is often a mis-transcription rather than a real instruction. This "
    "system ONLY handles actual commands, requests, or questions you can act on. If the text is NOT a "
    "genuine actionable request — that includes bare pleasantries or acknowledgements ('thank you', "
    "'thanks', 'okay', 'you', 'bye', 'cool'), lone stray words ('speed'), filler, or anything nonsensical "
    "or blatantly unrelated — then it is almost certainly a false trigger: do NOT respond, do NOT thank "
    "them back, do NOT ask for clarification. Reply with EXACTLY "
    f"'{IGNORE_SENTINEL}' and nothing else. Only respond normally when there is a real task or question.]"
)

# Always-on voice (ESP/Mac) replies are spoken aloud, so they must be very short. Appended to fly/mac
# turns. (The quick-add FAB has its own terser directive; typed web turns get neither.)
VOICE_REPLY_DIRECTIVE = (
    "\n\n[This came in by voice and the reply is spoken aloud — be VERY succinct: confirm the result in "
    "ONE short phrase (e.g. 'Floor lamp blue at 10%', 'Logged the bagel', 'About 1,800 calories'). No "
    "preamble, no 'I'll do X' narration, no follow-up question — just the essential result.]"
)


# Govee lights the model can control. We read their display names from the govee MCP's env file and
# inject them into the system prompt every turn, so the model acts on "the floor lamp" immediately
# instead of burning a round-trip on a list/lookup tool first.
GOVEE_ENV = os.environ.get("GOVEE_ENV", "/Users/jaredgantt/repos/govee-mcp/.env-govee")


def _govee_light_names() -> list:
    names = []
    try:
        with open(GOVEE_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GOVEE_LIGHT_") and "=" in line:
                    parts = line.split("=", 1)[1].split(",", 2)
                    if len(parts) == 3:
                        names.append(parts[2].strip().strip('"').strip("'"))
    except Exception:  # noqa: BLE001
        pass
    return names


def govee_context() -> str:
    names = _govee_light_names()
    if not names:
        return ""
    return ("Govee smart lights you can control RIGHT NOW, by name, via the govee-lights tools — do NOT "
            "call any list/lookup tool first, these ARE the available lights: " + ", ".join(names) + ".")

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    """UTC now as an ISO-8601 string (what Supabase timestamptz expects)."""
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"{now_iso()} [{WORKER_ID}] {msg}", flush=True)


def headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Supabase REST wrappers — these NEVER raise; they log and return a safe default
# so a transient HTTP blip can't kill the loop.
# --------------------------------------------------------------------------- #


class Supabase:
    def __init__(self, client: httpx.Client):
        self.c = client

    def _url(self, table: str, query: str = "") -> str:
        u = f"{SUPABASE_URL}/rest/v1/{table}"
        return f"{u}?{query}" if query else u

    def get(self, table: str, query: str = "") -> list:
        try:
            r = self.c.get(self._url(table, query), headers=headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 — defensive on purpose
            log(f"GET {table}?{query} failed: {e}")
            return []

    def post(self, table: str, body, extra_headers: dict | None = None) -> list | None:
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.post(self._url(table), headers=h, json=body)
            r.raise_for_status()
            # return=representation gives JSON back; plain inserts give 201 + empty body
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            log(f"POST {table} failed: {e}")
            return None

    def patch(self, table: str, query: str, body, extra_headers: dict | None = None):
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.patch(self._url(table, query), headers=h, json=body)
            r.raise_for_status()
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            log(f"PATCH {table}?{query} failed: {e}")
            return None

    def upsert(self, table: str, body, on_conflict: str):
        try:
            r = self.c.post(
                self._url(table, f"on_conflict={on_conflict}"),
                headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                json=body,
            )
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            log(f"UPSERT {table} failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Heartbeat / priority
# --------------------------------------------------------------------------- #


def heartbeat(db: Supabase) -> None:
    db.upsert(
        "worker_presence",
        {"worker": WORKER_ID, "last_seen": now_iso(), "info": {}},
        on_conflict="worker",
    )


def _parse_ts(s: str):
    if not s:
        return None
    try:
        # Supabase returns e.g. 2026-06-20T00:11:00.124643+00:00
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None


def higher_priority_present(db: Supabase) -> bool:
    """True if any worker ranked ABOVE me in WORKER_PRIORITY has a fresh heartbeat."""
    try:
        my_rank = WORKER_PRIORITY.index(WORKER_ID)
    except ValueError:
        # I'm not in the priority list — assume lowest, defer to nobody specific.
        my_rank = len(WORKER_PRIORITY)
    higher = set(WORKER_PRIORITY[:my_rank])
    if not higher:
        return False
    rows = db.get("worker_presence", "select=worker,last_seen")
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.get("worker") in higher:
            ts = _parse_ts(row.get("last_seen", ""))
            if ts and (now - ts).total_seconds() <= HEARTBEAT_FRESH_S:
                return True
    return False


# --------------------------------------------------------------------------- #
# Hub session state
# --------------------------------------------------------------------------- #


def read_hub_session() -> dict | None:
    try:
        if HUB_SESSION_FILE.exists():
            return json.loads(HUB_SESSION_FILE.read_text())
    except Exception as e:  # noqa: BLE001
        log(f"read hub_session.json failed: {e}")
    return None


def write_hub_session(session_id: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HUB_SESSION_FILE.write_text(
            json.dumps({"session_id": session_id, "last_activity_iso": now_iso()})
        )
    except Exception as e:  # noqa: BLE001
        log(f"write hub_session.json failed: {e}")


def should_reset_session(state: dict | None) -> bool:
    """Reset (start fresh) if: no state, idle > 2h, or it's a new calendar day."""
    if not state or not state.get("session_id"):
        return True
    last = _parse_ts(state.get("last_activity_iso", ""))
    if not last:
        return True
    now = datetime.now(timezone.utc)
    if (now - last).total_seconds() > SESSION_IDLE_RESET_S:
        return True
    if last.astimezone().date() != now.astimezone().date():
        return True
    return False


# --------------------------------------------------------------------------- #
# claude headless invocation
# --------------------------------------------------------------------------- #


def write_mcp_config() -> str:
    env = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
    cfg = {
        "mcpServers": {
            "data": {"command": "node", "args": [DATA_MCP_JS], "env": env},
            "voice": {"command": "node", "args": [VOICE_MCP_JS], "env": env},
            "govee-lights": {"command": GOVEE_PY, "args": [GOVEE_MCP_PY]},
        }
    }
    fd, path = tempfile.mkstemp(prefix="voice-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def run_claude(transcript: str, resume_session_id: str | None, on_event=None, system_append: str = "") -> dict:
    """Run `claude -p` headless and STREAM the stream-json output, parsing events as they arrive.

    on_event(kind, payload) fires live during the run so progress can be surfaced — currently
    on_event("tool", {"name", "input"}) for each tool call. Returns {"session_id", "text",
    "result", "ok", "raw_error"}.

    Tool allowlist is restricted to the MCP servers: no Bash/Edit/Read. In headless `-p` mode a
    tool that is not in --allowedTools is DENIED (not prompted), so the run never hangs.
    """
    mcp_path = write_mcp_config()
    cmd = [CLAUDE_BIN, "-p", transcript]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += [
        "--mcp-config", mcp_path,
        "--strict-mcp-config",
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", "mcp__data", "mcp__voice", "mcp__govee-lights",
    ]
    if system_append:
        cmd += ["--append-system-prompt", system_append]

    session_id = None
    assistant_chunks: list[str] = []
    result_text = ""
    raw_error = None
    proc = None
    timed_out = {"v": False}

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=os.environ.copy(),  # line-buffered so events arrive promptly
        )
        # Watchdog: kill the run if it exceeds the timeout, which ends the streaming loop at EOF.
        done = threading.Event()

        def _watchdog():
            if not done.wait(CLAUDE_TIMEOUT_S):
                timed_out["v"] = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_watchdog, daemon=True).start()

        # Drain stderr concurrently so it can't fill its pipe and deadlock the stdout reader below.
        err_buf: list[str] = []

        def _drain_err():
            try:
                for el in proc.stderr:
                    err_buf.append(el)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_drain_err, daemon=True).start()

        # Stream events as claude emits them (one JSON object per line).
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            etype = ev.get("type")

            if etype == "system":  # init event carries the session id
                if ev.get("session_id"):
                    session_id = ev["session_id"]

            elif etype == "assistant":  # text lives in content[*].text; tool calls in content[*].tool_use
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        assistant_chunks.append(block.get("text", ""))
                    elif block.get("type") == "tool_use" and on_event:
                        try:
                            on_event("tool", {"name": block.get("name", ""), "input": block.get("input")})
                        except Exception:  # noqa: BLE001
                            pass
                if ev.get("session_id"):
                    session_id = ev["session_id"]

            elif etype == "result":  # final event
                if ev.get("session_id"):
                    session_id = ev["session_id"]
                if isinstance(ev.get("result"), str):
                    result_text = ev["result"]
                if ev.get("is_error"):
                    raw_error = ev.get("result") or "claude reported is_error"

        done.set()
        proc.wait(timeout=5)
        stderr = "".join(err_buf)

        if timed_out["v"]:
            return {"session_id": session_id, "text": "", "result": "",
                    "ok": False, "raw_error": f"claude timed out after {CLAUDE_TIMEOUT_S}s"}
        if proc.returncode not in (0, None) and not result_text and not assistant_chunks:
            raw_error = raw_error or f"claude exited {proc.returncode}: {stderr[:500]}"

        text = result_text or "\n".join(c for c in assistant_chunks if c).strip()
        return {
            "session_id": session_id,
            "text": text,
            "result": result_text,
            "ok": raw_error is None and bool(text),
            "raw_error": raw_error,
        }
    except Exception as e:  # noqa: BLE001
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return {
            "session_id": session_id,
            "text": "",
            "result": "",
            "ok": False,
            "raw_error": f"claude invocation failed: {e}",
        }
    finally:
        try:
            os.unlink(mcp_path)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Request handling
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Warm Agent SDK path (routing_mode='sdk', and always on Fly). A persistent ClaudeSDKClient lives in a
# dedicated background event loop so the MCP servers connect ONCE and stay hot across requests — no
# per-command CLI/MCP cold start. Uses the Anthropic API (ANTHROPIC_API_KEY) → billed to the SDK
# credit, unlike the subscription `claude -p` path above.
# --------------------------------------------------------------------------- #


class SdkRunner:
    def __init__(self):
        self._loop = None
        self._client = None
        self._last = 0.0
        self._day = None

    def _start_loop(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, name="sdk-loop", daemon=True).start()

    async def _disconnect(self):
        try:
            if self._client:
                await self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def reset(self):
        """Drop the warm client so the next request starts a fresh conversation."""
        if self._client is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop).result(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    async def _ensure_client(self, system_append):
        if self._client is not None:
            return
        from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions,
                                      PermissionResultAllow, PermissionResultDeny)
        mcp_env = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}

        async def _can_use_tool(tool_name, tool_input, context):  # allowlist: MCP tools only, no prompts
            if str(tool_name).startswith("mcp__"):
                return PermissionResultAllow()
            return PermissionResultDeny(message="only MCP tools are permitted")

        sysprompt = {"type": "preset", "preset": "claude_code"}
        if system_append:
            sysprompt["append"] = system_append
        # Only wire MCP servers whose files exist — the Fly fallback image has no Govee LAN MCP, and
        # the LAN lights aren't reachable from the cloud anyway, so it degrades gracefully.
        servers = {}
        if os.path.exists(DATA_MCP_JS):
            servers["data"] = {"type": "stdio", "command": "node", "args": [DATA_MCP_JS], "env": mcp_env}
        if os.path.exists(VOICE_MCP_JS):
            servers["voice"] = {"type": "stdio", "command": "node", "args": [VOICE_MCP_JS], "env": mcp_env}
        if os.path.exists(GOVEE_MCP_PY):
            servers["govee-lights"] = {"type": "stdio", "command": GOVEE_PY, "args": [GOVEE_MCP_PY]}
        opts = ClaudeAgentOptions(
            mcp_servers=servers,
            allowed_tools=["mcp__data", "mcp__voice", "mcp__govee-lights"],
            can_use_tool=_can_use_tool,
            system_prompt=sysprompt,
        )
        self._client = ClaudeSDKClient(opts)
        await self._client.connect()

    async def _run(self, transcript, system_append, on_event):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
        await self._ensure_client(system_append)
        await self._client.query(transcript)
        parts, raw_error = [], None
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        parts.append(b.text)
                    elif isinstance(b, ToolUseBlock) and on_event:
                        try:
                            on_event("tool", {"name": b.name, "input": b.input})
                        except Exception:  # noqa: BLE001
                            pass
            elif isinstance(msg, ResultMessage):
                if getattr(msg, "is_error", False):
                    raw_error = getattr(msg, "result", None) or "sdk reported error"
        text = "".join(parts).strip()
        return {"session_id": None, "text": text, "result": text,
                "ok": raw_error is None and bool(text), "raw_error": raw_error}

    def run(self, transcript, system_append="", on_event=None) -> dict:
        self._start_loop()
        now = time.monotonic()
        today = datetime.now().astimezone().date()
        if self._client is not None and (now - self._last > SESSION_IDLE_RESET_S or self._day != today):
            self.reset()   # fresh conversation on idle / day rollover, matching the subscription path
        self._last, self._day = now, today
        try:
            fut = asyncio.run_coroutine_threadsafe(self._run(transcript, system_append, on_event), self._loop)
            result = fut.result(timeout=CLAUDE_TIMEOUT_S + 30)
            if SDK_EPHEMERAL:
                self.reset()   # fallback (fly) runs rarely → don't hold the warm CLI/MCPs in RAM between uses
            return result
        except Exception as e:  # noqa: BLE001 — a dead warm client shouldn't wedge the worker
            self.reset()
            return {"session_id": None, "text": "", "result": "",
                    "ok": False, "raw_error": f"sdk run failed: {e}"}


# The fly fallback shares the always-on voice machine and runs rarely, so it resets its SDK client
# after each request rather than holding the warm CLI/MCPs resident (keeps memory low on that box).
SDK_EPHEMERAL = WORKER_ID == "fly"
SDK = SdkRunner()


def get_routing_mode(db: Supabase) -> str:
    """'sdk' (warm Agent SDK, API/credit) or 'subscription' (claude -p). Fly is always sdk (API-only)."""
    if WORKER_ID == "fly":
        return "sdk"
    rows = db.get("app_settings", "key=eq.routing_mode&select=value")
    if rows and rows[0].get("value") in ("sdk", "subscription"):
        return rows[0]["value"]
    return "subscription"


def get_focus(db: Supabase) -> str:
    rows = db.get("voice_focus", "id=eq.1&select=focus")
    if rows and isinstance(rows, list) and rows[0].get("focus"):
        return rows[0]["focus"]
    return "hub"  # safe default


def inject_to_terminal(text: str) -> tuple[bool, str | None]:
    """Send a spoken line to the focused Mac session's wall terminal (the
    orchestrator writes it to the live pty and presses Enter). Best-effort."""
    try:
        r = httpx.post(
            f"{ORCH_URL}/inject",
            params={"token": ORCH_TOKEN},
            json={"text": text, "submit": True},
            timeout=10.0,
        )
        if r.status_code < 300:
            return True, None
        return False, f"orchestrator /inject HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"orchestrator unreachable: {e}"


def claim_one(db: Supabase) -> dict | None:
    """Find one pending request and atomically claim it. Returns the row or None."""
    if higher_priority_present(db):
        return None

    pending = db.get(
        "voice_requests",
        "status=eq.pending&order=created_at.asc&limit=1&select=id,transcript,source",
    )
    if not pending:
        return None
    req = pending[0]
    rid = req["id"]

    claimed = db.patch(
        "voice_requests",
        f"id=eq.{rid}&status=eq.pending",
        {
            "status": "claimed",
            "claimed_by": WORKER_ID,
            "claimed_at": now_iso(),
            "assigned_to": WORKER_ID,
            "updated_at": now_iso(),
        },
        extra_headers={"Prefer": "return=representation"},
    )
    # Empty representation array => someone else won the race.
    if not claimed:
        return None
    return claimed[0]


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _duplicate_of_recent(db: Supabase, req: dict) -> bool:
    """True if another request within ~8s has a near-identical transcript and was already claimed/done —
    i.e. a second always-on listener (ESP + Mac) heard the same utterance. Keeps the EARLIER request."""
    rid = req.get("id")
    tx = _norm_text(req.get("transcript") or "")
    ts = _parse_ts(req.get("created_at") or "")
    if not tx or ts is None:
        return False
    rows = db.get("voice_requests", "select=id,transcript,created_at,status&order=created_at.desc&limit=12")
    for r in rows:
        if r.get("id") == rid or r.get("status") not in ("claimed", "done"):
            continue
        ots = _parse_ts(r.get("created_at") or "")
        if ots is None or abs((ts - ots).total_seconds()) > 8:
            continue
        if ots <= ts and difflib.SequenceMatcher(None, tx, _norm_text(r.get("transcript") or "")).ratio() >= 0.75:
            return True
    return False


def process_request(db: Supabase, req: dict) -> None:
    rid = req["id"]
    transcript = req.get("transcript") or ""
    log(f"claimed request {rid}: {transcript[:80]!r}")

    # Manual context reset from the hub UI: drop the resumed hub session so the next command starts
    # fresh. The status-hub server already cleared the visible thread and pushed a live reset to
    # every surface — here we just forget the Claude session and ack (no Claude run, no turns).
    if transcript.strip() == RESET_SENTINEL:
        try:
            HUB_SESSION_FILE.unlink(missing_ok=True)
        except Exception as e:
            log(f"reset: unlink hub_session failed: {e}")
        SDK.reset()   # also drop the warm SDK conversation, if any
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(context reset)", "updated_at": now_iso()})
        log("hub context reset — session dropped")
        return

    focus = get_focus(db)

    # Focus is a specific Mac session → inject the transcript into that session's
    # wall terminal (the orchestrator owns the live `claude --resume` pty) instead
    # of running a fresh hub `claude -p`. The conversation shows in the xterm panel.
    if focus != "hub":
        log(f"focus={focus!r}: injecting into wall terminal via orchestrator")
        ok, err = inject_to_terminal(transcript)
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            ({"status": "done", "result": "(injected to terminal)", "updated_at": now_iso()}
             if ok else
             {"status": "error", "error": err or "inject failed", "updated_at": now_iso()}),
        )
        return

    # Dedup: multiple always-on listeners (ESP + Mac) hear one utterance, so it lands as several requests
    # a few seconds apart. If a near-simultaneous, near-identical transcript was already handled, drop this
    # one so the command never runs twice. v1 keeps the earlier request; quality-ranked pick comes later.
    if _duplicate_of_recent(db, req):
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(duplicate — dropped)", "updated_at": now_iso()})
        log(f"dropped duplicate request {rid}: {transcript[:60]!r}")
        return

    # (The user turn is written AFTER the model runs — deferred so a wake-word false positive that the
    # model drops never appears in the conversation. See below.)

    # Hub session management.
    state = read_hub_session()
    if should_reset_session(state):
        resume_id = None
        log("hub session: starting FRESH (reset rule fired)")
    else:
        resume_id = state["session_id"]
        log(f"hub session: resuming {resume_id}")

    # Surface tool calls live by pushing them straight to the hub's activity bar (transient — not
    # stored in voice_messages, so they never appear in the conversation thread).
    def _emit(kind, payload):
        if kind != "tool":
            return
        name = str(payload.get("name") or "")
        short = name.rsplit("__", 1)[-1] if "__" in name else name
        try:
            args = payload.get("input")
            argstr = json.dumps(args, ensure_ascii=False, separators=(",", ":")) if args else ""
        except Exception:  # noqa: BLE001
            argstr = str(payload.get("input") or "")
        if len(argstr) > 200:
            argstr = argstr[:199] + "…"
        text = (short + (" " + argstr if argstr else "")).strip()
        push_activity(text, "tool")  # direct push → instant activity bar (transient, not stored)

    # Per-source prompt directives (ride only on the prompt handed to Claude, never shown in the thread):
    #  · transcribed sources (ESP/Mac/quick-FAB) get false-positive awareness so noise mis-hears drop,
    #  · the quick-add FAB additionally gets the terse fits-in-the-chip reply format.
    source = (req.get("source") or "").strip()
    quick = source == "web-quick"
    audio = source in ("fly", "mac", "web-quick")   # transcribed (mishear-prone); typed text is excluded
    prompt = transcript
    if audio:
        prompt += WAKE_NOISE_DIRECTIVE
    if quick:
        prompt += QUICK_REPLY_DIRECTIVE
    if source in ("fly", "mac"):
        prompt += VOICE_REPLY_DIRECTIVE   # spoken aloud → keep it short

    sys_ctx = govee_context()
    if get_routing_mode(db) == "sdk":
        result = SDK.run(prompt, system_append=sys_ctx, on_event=_emit)      # warm Agent SDK (API/credit)
    else:
        result = run_claude(prompt, resume_id, on_event=_emit, system_append=sys_ctx)  # claude -p (subscription)

    if result.get("session_id"):
        write_hub_session(result["session_id"])
        log(f"captured session_id {result['session_id']}")

    text = (result.get("text") or "").strip()

    # Wake-word false positive: the model flagged the transcript as noise/mishear → drop it silently.
    # No user OR assistant turn is written, so the conversation stays clean; just clear the activity bar.
    if audio and re.sub(r"[^a-z]", "", text.lower()) == "ignore":
        push_activity("(ignored — likely noise)", "done")
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(ignored: wake-word false positive)", "updated_at": now_iso()})
        log(f"ignored likely false-positive: {transcript[:60]!r}")
        return

    # Real turn → now write the user bubble (deferred past the noise check), then close the activity bar.
    db.post("voice_messages",
            {"request_id": rid, "focus": "hub", "role": "user", "text": transcript, "done": True})
    push_activity(text[:120], "done")  # close out the activity bar w/ the result

    if result["ok"]:
        db.post(
            "voice_messages",
            {"request_id": rid, "focus": "hub", "role": "assistant",
             "text": text, "done": True},
        )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "done", "result": text, "updated_at": now_iso()},
        )
        log(f"done request {rid}: {text[:120]!r}")
    else:
        err = result.get("raw_error") or "unknown error"
        # Still surface whatever assistant text we did get, if any.
        if result.get("text"):
            db.post(
                "voice_messages",
                {"request_id": rid, "focus": "hub", "role": "assistant",
                 "text": result["text"], "done": True},
            )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "error", "error": err, "updated_at": now_iso()},
        )
        log(f"ERROR request {rid}: {err}")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

_running = True


def _handle_signal(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    log(f"received signal {signum}; shutting down after current iteration")


def one_iteration(db: Supabase) -> bool:
    """Run a single claim+process cycle. Returns True if a request was processed."""
    req = claim_one(db)
    if not req:
        return False
    try:
        process_request(db, req)
    except Exception as e:  # noqa: BLE001 — a bug in processing must not kill the loop
        log(f"process_request crashed: {e}")
        try:
            db.patch(
                "voice_requests",
                f"id=eq.{req['id']}",
                {"status": "error", "error": str(e), "updated_at": now_iso()},
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        if (req.get("source") or "").strip() == "fly":
            ping_esp_led_off()   # model done (or dropped/errored) → clear the ESP working-light
    return True


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY is required (service_role key). See README.",
              file=sys.stderr)
        return 2

    once = "--once" in sys.argv

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"starting (priority={WORKER_PRIORITY}, state_dir={STATE_DIR}, once={once})")

    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        db = Supabase(client)

        if once:
            heartbeat(db)
            processed = one_iteration(db)
            log(f"--once complete (processed={processed})")
            return 0

        last_heartbeat = 0.0
        while _running:
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                heartbeat(db)
                last_heartbeat = now

            processed = one_iteration(db)
            # Loop quickly if we just worked (drain the queue); else idle-poll.
            if not processed:
                time.sleep(IDLE_POLL_S)

    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
