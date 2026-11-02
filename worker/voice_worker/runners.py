"""Where a turn runs: the warm SDK client (fenced), the unrestricted `claude -p` agent, and Codex."""

import asyncio
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx

from .config import AGENT_TIMEOUT_S, ALL_TOOLS, JAREDBOT_TOOL, CLAUDE_BIN, CLAUDE_TIMEOUT_S, CREDITS_API_KEY, DATA_MCP_JS, GMAIL_MCP_PY, GMAIL_PY, GOVEE_MCP_PY, GOVEE_PY, HAS_MESSAGES, HTTP_TIMEOUT_S, HUB_EFFORT, HUB_MODEL, MESSAGES_HEADERS, MESSAGES_MCP_URL, PETLIBRO_MCP_PY, PETLIBRO_PY, SEND_TOOLS, SESSION_START_TOOLS, SESSION_TOOLS, STATE_DIR, SUPABASE_KEY, SUPABASE_URL, VOICE_MCP_JS, WORKER_ID
from .db import headers
from .logs import classify_failure, log, log_error
from .outbound import push_activity
from .presence import claude_unready
from .prompts import HUB_SYSTEM, SHARED_STATE_NOTE


# A subscription run that fails for a reason that passes on its own — Anthropic overloaded or erroring,
# a rate limit, the network — is tried again after each of these pauses before the turn is given up.
# There is no paid fallback behind it: waiting a little is free, and on 2026-09-21 a single "529
# Overloaded" sent an Opus turn to the Console key. An auth or billing failure is not retried; waiting
# cannot fix a revoked login.
SUBSCRIPTION_RETRY_S = (3, 8, 15)


def transient_failure(err: str) -> bool:
    e = (err or "").lower()
    if e.startswith("claude timed out"):
        return False   # our own watchdog: a hung run hangs again, and each retry holds the claim 300s more
    if classify_failure(err) == "rate-limit":
        return True
    return any(t in e for t in ("529", "500", "502", "503", "504", "internal server error", "timed out",
                                "timeout", "connection", "network", "temporarily"))


def run_subscription(run, label: str) -> dict:
    """`run()` (a subscription turn), retried through SUBSCRIPTION_RETRY_S while it fails transiently.
    Returns the last result; the caller surfaces it if it is still a failure."""
    result = run()
    for pause in SUBSCRIPTION_RETRY_S:
        if result.get("ok") or not transient_failure(result.get("raw_error") or ""):
            break
        log(f"{label} failed ({result.get('raw_error')}) — retrying on the subscription in {pause}s")
        push_activity(f"Retrying ({classify_failure(result.get('raw_error') or '')})")
        time.sleep(pause)
        result = run()
    if not result.get("ok"):
        # Reported by process_request, which knows whether the command was handed on or ended.
        log(f"{label} failed ({result.get('raw_error')}) — giving up, no paid fallback")
    return result


# The current command's audio-recording START time (voice_requests.capture_start), written here per
# command. The data MCP's now() reads it so a voice-logged SET lands at when the user actually spoke — the user's
# rest-clock reference — not ~30s later when the command finishes the pipeline.
#
# It is a FILE and not an env value, and that distinction is the whole point. The warm hub client
# (SdkRunner) starts its MCP servers ONCE per worker lifetime as warm stdio sessions, so anything placed in the
# subprocess environment is fixed for as long as the worker runs: passing the timestamp that way froze
# it at one moment and stamped 29 hours of writes with it (Aug 31, found 2026-09-01). The path is
# constant — safe to bake into a warm session — while the contents change per command.
CMD_TS_FILE = STATE_DIR / "cmd_ts"


def set_cmd_ts(value: str | None) -> None:
    """Publish (or clear) the in-flight command's capture_start for the data MCP."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CMD_TS_FILE.write_text(value or "")
    except Exception as e:  # noqa: BLE001
        log_error(f"cmd_ts write failed: {e}")   # a stale stamp is worse than none, so this is never fatal


def mcp_servers(codex_sessions: bool = False) -> dict:
    """Every MCP server a hub turn gets, keyed by name, in Claude Code's --mcp-config shape.

    `codex_sessions` is for a turn running because Claude could not take it (the Codex fallback, the
    credits runner at the usage limit): sessions it dispatches start on Codex (DATA_AGENT, read by
    data-layer start_session), since a Claude session would hit the same wall."""
    env = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
    # DATA_CALLER=hub marks sessions this worker dispatches, so their first reply is relayed back
    # into the hub thread (data-layer startSession → orchestrator/src/relay.js).
    data_env = {**env, "DATA_TS_FILE": str(CMD_TS_FILE),
                "DATA_CALLER": "hub", **({"DATA_AGENT": "codex"} if codex_sessions else {})}
    cfg = {
        "mcpServers": {
            "data": {"command": "node", "args": [DATA_MCP_JS], "env": data_env},
            "voice": {"command": "node", "args": [VOICE_MCP_JS], "env": env},
            "govee-lights": {"command": GOVEE_PY, "args": [GOVEE_MCP_PY], "env": env},
        }
    }
    # Only where the feeder server is installed: it is LAN-only, and a registered server whose files are
    # missing (Fly) fails to start on every turn.
    if os.path.exists(PETLIBRO_MCP_PY):
        cfg["mcpServers"]["petlibro-feeder"] = {"command": PETLIBRO_PY, "args": [PETLIBRO_MCP_PY], "env": env}
    # The Mac and the Pi keep the Gmail credential in gmail-mcp/.env; Fly has no such file and holds it
    # as secrets, passed through here — only what is set, since an empty value would shadow the .env.
    # Registered only where one of the two exists: without it every Gmail call fails at the token.
    g_env = {k: os.environ[k] for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
             if os.environ.get(k)}
    if os.path.exists(GMAIL_MCP_PY) and (len(g_env) == 3 or os.path.exists(pathlib.Path(GMAIL_MCP_PY).parent / ".env")):
        cfg["mcpServers"]["gmail"] = {"command": GMAIL_PY, "args": [GMAIL_MCP_PY], "env": g_env}
    if HAS_MESSAGES:
        cfg["mcpServers"]["messages"] = {"type": "http", "url": MESSAGES_MCP_URL, "headers": MESSAGES_HEADERS}
    return cfg["mcpServers"]


def write_mcp_config(codex_sessions: bool = False) -> str:
    cfg = {"mcpServers": mcp_servers(codex_sessions)}
    # alwaysLoad makes Claude Code connect a server BEFORE the first turn. Without it, `-p` connects
    # --mcp-config servers in the background and the model's first request carries only whichever ones
    # had finished starting: on Fly the voice server (~1.6s) made it and data (~2s) did not, so the
    # assistant answered "I don't have a nutrition tool" with the tool seconds away.
    for srv in cfg["mcpServers"].values():
        srv["alwaysLoad"] = True
    fd, path = tempfile.mkstemp(prefix="voice-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def _kill_group(proc) -> None:
    """Kill a run and everything it started. A grandchild holding stdout open would otherwise keep the
    stream reader from ever reaching EOF, and the claim with it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_claude(transcript: str, resume_session_id: str | None, *, manual: bool, on_event=None,
               system_append: str = "", model: str = "opus", cwd: str | None = None) -> dict:
    """Run the UNRESTRICTED Claude Code agent headless (`claude -p --dangerously-skip-permissions`) and
    STREAM its stream-json output, parsing events as they arrive. Full-agent mode and the watch's
    fallback path are its only callers; every hub turn runs on the fenced SdkRunner instead.

    Every native tool executes, alongside the MCP tools, in `cwd`, for up to AGENT_TIMEOUT_S. There is no
    fence on this path, which is why it refuses outright unless the caller says the command was started
    by a press (`manual`, from is_manual): a microphone must never reach it.

    on_event(kind, payload) fires live during the run so progress can be surfaced — on_event("tool",
    {"name", "input"}) for each tool call, on_event("text", ...) for narration a tool call followed.
    Returns {"session_id", "text", "result", "ok", "raw_error"}.
    """
    if manual is not True:
        log_error("run_claude refused a command that was not started by a press")
        return {"session_id": None, "text": "", "result": "", "ok": False,
                "raw_error": "the unrestricted agent only runs commands started by a press"}
    mcp_path = write_mcp_config()
    cmd = [CLAUDE_BIN, "-p", transcript]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += [
        "--model", model,
        "--mcp-config", mcp_path,
        "--strict-mcp-config",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",   # all native tools execute; MCP tools still present
        # Out of the schema: the browser-only tools, which no worker has the browser for.
        "--disallowedTools", *SESSION_TOOLS,
    ]
    if system_append:
        cmd += ["--append-system-prompt", system_append]
    timeout_s = AGENT_TIMEOUT_S
    if cwd and not os.path.isdir(cwd):   # a published path may not exist on the worker that runs it
        log(f"agent cwd {cwd!r} not present here; falling back to $HOME")
        cwd = os.path.expanduser("~")

    session_id = None
    assistant_chunks: list[str] = []
    result_text = ""
    raw_error = None
    proc = None
    timed_out = {"v": False}
    t_spawn = t_init = t_first = None   # timing seams: spawn → MCP-ready (system init) → first model token
    n_tools = 0

    # Subscription path: `claude -p` uses the logged-in Claude subscription. ANTHROPIC_API_KEY is popped
    # from the environment at import, so no subprocess can inherit it and silently bill the API.
    sub_env = os.environ.copy()
    # Load ALL MCP tool schemas inline every turn — no lazy tool-search/discovery round-trip. This is a
    # tool-centric agent (its whole job is calling these tools), and the trimmed toolset is well under the
    # context budget, so we want every tool callable directly. (Default 'auto' defers above 10% of context.)
    sub_env["ENABLE_TOOL_SEARCH"] = "false"

    try:
        t_spawn = time.monotonic()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=sub_env, cwd=cwd,  # line-buffered; cwd is where a full agent operates
            start_new_session=True,   # its own process group, so a kill reaches anything it spawned
        )
        # Watchdog: kill the run if it exceeds the timeout, which ends the streaming loop at EOF.
        done = threading.Event()

        def _watchdog():
            if not done.wait(timeout_s):
                timed_out["v"] = True
                _kill_group(proc)

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
        said = []   # text since the last tool call: progress once a tool follows it, else the reply
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            etype = ev.get("type")

            if etype == "system":  # init event = process up + MCP servers loaded (the cold-start boundary)
                if t_init is None:
                    t_init = time.monotonic()
                if ev.get("session_id"):
                    session_id = ev["session_id"]

            elif etype == "assistant":  # text lives in content[*].text; tool calls in content[*].tool_use
                if t_first is None:     # first model output → end of "thinking before it says anything"
                    t_first = time.monotonic()
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        assistant_chunks.append(block.get("text", ""))
                        said.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        n_tools += 1
                        tname = block.get("name", "")
                        if on_event:
                            try:
                                if "".join(said).strip():
                                    on_event("text", {"text": "\n".join(said)})
                                on_event("tool", {"name": tname, "input": block.get("input")})
                            except Exception:  # noqa: BLE001
                                pass
                        said = []
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
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:   # stdout closed but the process lingers: the reply is already in
            _kill_group(proc)
        stderr = "".join(err_buf)
        t_end = time.monotonic()
        if t_spawn:                       # latency breakdown of this run (where the seconds actually go)
            cold = f"{t_init - t_spawn:.1f}s" if t_init else "n/a"
            ttf  = f"{t_first - t_init:.1f}s" if (t_first and t_init) else "n/a"
            gen  = f"{t_end - t_first:.1f}s" if t_first else "n/a"
            log(f"timing: cold-start(spawn→mcp) {cold} | first-token {ttf} | "
                f"gen+{n_tools}tools {gen} | total {t_end - t_spawn:.1f}s | "
                f"model={model} resume={'y' if resume_session_id else 'n'} agent=y")

        if timed_out["v"]:
            return {"session_id": session_id, "text": "", "result": "",
                    "ok": False, "raw_error": f"claude timed out after {timeout_s}s"}
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
            _kill_group(proc)
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


_SDK_LOOP_LOCK = threading.Lock()


def report_usage(info: dict) -> None:
    """Record the subscription's usage windows from a turn's `rate_limit_event` in Supabase
    `claude_usage` (data-layer 00144), off the turn's thread. The table drops a reading identical to the
    newest, so a turn that moved nothing writes nothing. This is the worker's only way to see usage: its
    CLAUDE_CODE_OAUTH_TOKEN lacks the user:profile scope the usage endpoint needs."""
    w = (info or {}).get("unifiedWindows") or {}
    five, week = w.get("five_hour") or {}, w.get("seven_day") or {}
    if five.get("utilization") is None or week.get("utilization") is None:
        return

    def at(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None

    row = {"source": f"worker:{WORKER_ID}", "status": info.get("status"),
           "five_hour_pct": round(five["utilization"] * 100, 1), "five_hour_resets_at": at(five.get("resetsAt")),
           "seven_day_pct": round(week["utilization"] * 100, 1), "seven_day_resets_at": at(week.get("resetsAt"))}

    def _go():
        try:
            r = httpx.post(f"{SUPABASE_URL}/rest/v1/claude_usage", json=row, timeout=HTTP_TIMEOUT_S,
                           headers={**headers(), "Prefer": "return=minimal"})
            if r.status_code >= 300:
                log_error(f"claude_usage insert HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:  # noqa: BLE001
            log_error(f"claude_usage insert failed: {e}")
    threading.Thread(target=_go, name="usage-report", daemon=True).start()


GATED_REASON = "That tool is not available to this assistant."


class SdkRunner:
    """Warm Claude Agent SDK client — every hub turn runs here, billed to the subscription.

    One long-lived headless Claude Code process holds the four MCP servers, so the per-command spawn
    spawn cost is paid once, not per turn. Each hub turn runs in a FRESH conversation (`/clear` on the
    same warm process, see _turn): the caller replays the day's hub conversation into the prompt. Pinned to HUB_MODEL
    at HUB_EFFORT with the full toolset at connect; full-agent and fallback turns keep their own `claude -p`
    spawns. A failed turn is retried by `run_subscription` and then surfaced, never moved onto the paid API.
    """

    def __init__(self, model: str = HUB_MODEL, effort: "str | None" = HUB_EFFORT, credits: bool = False):
        """The full toolset plus Read, which the fence allows only inside the current turn's photo
        directory. `credits` bills the Console key instead of the subscription (see CREDITS_API_KEY)."""
        self.model = model
        self.effort = effort
        self.credits = credits
        self.limit_hit = False   # the last turn's rate_limit_event said the subscription refused it
        self.tools = ALL_TOOLS + ["Read"]
        self._read_dir = None  # this turn's attachment directory, the only place Read may look
        self._loop = None
        self._client = None
        self._manual = False   # the turn in flight was started by a press (is_manual); set by run()
        self._used = False     # the warm client has run a turn since it connected, so needs a /clear
        self._lock = None      # serialises connects; created on the SDK loop

    def _start_loop(self):
        with _SDK_LOOP_LOCK:   # warm() runs on its own thread, so two callers can race here
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="sdk-loop", daemon=True).start()

    async def _ensure(self):
        if self._client is not None:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()   # warm() and a first turn can both get here; connect once
        async with self._lock:
            if self._client is None:
                await self._connect()

    def warm(self) -> None:
        """Connect now rather than on the next turn. Measured cold, the first command after a restart
        took 19.7s on Fly and 9.9s on the Pi where a warm one took 2.8s and 4.5s; every deploy, restart
        and failed turn (`_recycle`) used to hand that cost to whatever the user said next."""
        if claude_unready():
            return
        self._start_loop()
        try:
            asyncio.run_coroutine_threadsafe(self._ensure(), self._loop).result(timeout=120)
        except Exception as e:  # noqa: BLE001
            log(f"sdk runner: warm-up failed ({e}); the next turn connects")

    def gate(self, name: str, tool_input: dict) -> tuple[str, "str | None"]:
        """The fence's decision for one tool call on the turn in flight: ("allow" | "deny", reason) or
        ("pass", None) to leave it to the allow list. Enforced as a PreToolUse hook BEFORE any permission
        rule: this client is Claude Code, which also applies the host user's own settings, and the Mac's
        pre-approve every data, gmail and messages tool in auto mode; with allowed_tools alone a room-mic
        turn sent an email in the first test of the send rule. A hook's deny outranks any allow rule."""
        exact = set(self.tools)
        servers = tuple(t + "__" for t in self.tools if t.startswith("mcp__") and t.count("__") == 1)
        # A session, or a message to another person, only from a turn the user pressed for or typed (is_manual).
        # Fails closed: the client is warm across turns, and the flag is False unless run() was told this
        # turn was pressed. Switching JaredBot OFF stays open: silencing an auto-replier sends nothing.
        sends = name in SEND_TOOLS or (name == JAREDBOT_TOOL and bool((tool_input or {}).get("on")))
        if (name in SESSION_START_TOOLS or sends) and not self._manual:
            return "deny", ("Starting or messaging a Claude session, and sending a text or an email, are off "
                            "for commands a microphone heard by itself. It has to come from the button on his "
                            "phone or watch, or be typed.")
        if name in SEND_TOOLS and name in exact:
            return "allow", None
        if name == "Read":   # photo turns only: the CLI views an image by reading its file
            path = os.path.realpath(str((tool_input or {}).get("file_path") or ""))
            if self._read_dir and path.startswith(os.path.realpath(self._read_dir) + os.sep):
                return "allow", None
            return "deny", "Read is only for the photos attached to this message."
        if name in exact or name.startswith(servers):
            return "pass", None
        return "deny", GATED_REASON

    async def _connect(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher, PermissionResultDeny

        gated = GATED_REASON

        def _decide(decision, reason=None):
            out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
            if reason:
                out["permissionDecisionReason"] = reason
            return {"hookSpecificOutput": out}

        async def _fence(input_data, tool_use_id, context):  # noqa: ARG001
            decision, reason = self.gate(input_data.get("tool_name") or "", input_data.get("tool_input") or {})
            return {} if decision == "pass" else _decide(decision, reason)

        async def _deny(tool_name, tool_input, context):  # noqa: ARG001
            # Reached only by a tool the fence let through without pre-approval; there is none today.
            # (Without a callback the SDK raises on a permission request, failing the whole turn.)
            return PermissionResultDeny(message=gated)

        opts = ClaudeAgentOptions(
            model=self.model,
            **({"effort": self.effort} if self.effort else {}),
            # The SDK ships its own copy of Claude Code, which only moves when the package is upgraded, and
            # the alias "sonnet" resolves to whatever model THAT copy knows. Running the installed CLI keeps
            # this path on the same models as every other turn instead of drifting a release behind.
            cli_path=shutil.which(CLAUDE_BIN),
            # The credits runner is the usage-limit fallback, so the sessions it starts run on Codex.
            mcp_servers=write_mcp_config(codex_sessions=self.credits),
            allowed_tools=list(self.tools),
            disallowed_tools=list(SESSION_TOOLS),   # browser-only: out of sight entirely
            # The stable hub framing lives here so the prefix caches across turns; per-turn context
            # (the replayed conversation) rides on the prompt instead.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": HUB_SYSTEM + "\n\n" + SHARED_STATE_NOTE,
                           "exclude_dynamic_sections": True},
            # All schemas inline, same as the -p path. The credits runner swaps the auth: an API key in the
            # environment outranks both the OAuth token and the machine's login.
            env={"ENABLE_TOOL_SEARCH": "false",
                 **({"ANTHROPIC_API_KEY": CREDITS_API_KEY, "CLAUDE_CODE_OAUTH_TOKEN": ""} if self.credits else {})},
            can_use_tool=_deny,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_fence])]},
            cwd=os.path.expanduser("~"),
            # The SDK reads the CLI's stdout one JSON line at a time and dies on any line over 1MB (its
            # default). A photo turn's Read result carries the image base64 in one line, so a phone photo
            # kills the whole turn ("JSON message exceeded maximum buffer size"). The buffer only grows
            # to the size of the line actually read, so a high ceiling costs nothing on ordinary turns.
            max_buffer_size=64 * 1024 * 1024,
        )
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        self._client = client
        self._used = False
        log(f"sdk runner: warm client up ({self.model}{' @' + self.effort if self.effort else ''})")

    async def _teardown(self):
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _turn(self, prompt, on_event):
        await self._ensure()
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage, RateLimitEvent
        # `/clear` is what makes this turn a fresh conversation; the `session_id` passed to query() is
        # only a label and starts nothing. Without it every turn appends to one conversation that also
        # re-receives the whole day as `ctx`, so it grows quadratically: ~977k tokens after 112 turns,
        # when auto-compaction stalled a turn for 90s and every command queued behind it. ~0.2s.
        if self._used:
            await self._client.query("/clear")
            async for _ in self._client.receive_response():
                pass
        self._used = True
        self.limit_hit = False
        await self._client.query(prompt, session_id=uuid.uuid4().hex)
        chunks, result_text, ok, raw_error = [], "", True, None
        used_tools = False
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        chunks.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        # The reply is what the model says after its LAST tool call. Text before one
                        # ("searching now") is narration; it goes to the activity stream as progress
                        # instead.
                        said = "\n".join(chunks).strip()
                        chunks = []
                        used_tools = True   # gates the retry in run(); tracked even with no on_event
                        if on_event:
                            try:
                                if said:
                                    on_event("text", {"text": said})
                                on_event("tool", {"name": b.name, "input": b.input})
                            except Exception:  # noqa: BLE001
                                pass
            elif isinstance(msg, RateLimitEvent):
                self.limit_hit = msg.rate_limit_info.status == "rejected"
                report_usage(msg.rate_limit_info.raw)
            elif isinstance(msg, ResultMessage):
                ok = not msg.is_error
                result_text = msg.result or ""
                if msg.is_error:
                    raw_error = result_text or msg.subtype
        return {"session_id": None, "text": "\n".join(chunks).strip(), "used_tools": used_tools,
                "result": result_text, "ok": ok, "raw_error": raw_error}

    def close(self):
        """Drop the warm client, if there is one, without starting another."""
        if self._client is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._teardown(), self._loop).result(timeout=15)
        except Exception as e:  # noqa: BLE001
            log_error(f"sdk close failed: {e}")

    def _recycle(self):
        """Drop the warm client and connect a fresh one in the background, before the next turn needs it."""
        try:
            asyncio.run_coroutine_threadsafe(self._teardown(), self._loop).result(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self.warm, name="sdk-warm", daemon=True).start()

    def _attempt(self, transcript, on_event) -> dict:
        """One turn on the warm client. `used_tools` reports whether it acted before failing; a crash
        hides that, so it reports True — the conservative answer, since it only suppresses a retry."""
        try:
            fut = asyncio.run_coroutine_threadsafe(self._turn(transcript, on_event), self._loop)
            return fut.result(timeout=CLAUDE_TIMEOUT_S + 30)
        except Exception as e:  # noqa: BLE001 — never wedge the worker
            return {"session_id": None, "text": "", "result": "", "ok": False, "used_tools": True,
                    "raw_error": f"sdk run failed: {e}"}

    def run(self, transcript, on_event=None, manual=False, read_dir=None) -> dict:
        """`manual` says the turn was started by a press (is_manual), which is what the fence reads for the
        session and send tools; `read_dir` is where this turn's photos are. `manual` defaults to the
        restrictive answer. Turns run one at a time, so a flag on the runner is exactly one turn's."""
        self._start_loop()
        self._manual, self._read_dir = manual, read_dir
        result = self._attempt(transcript, on_event)
        if result.get("ok"):
            return result
        # ANY failure leaves the warm process in an unknown state, so recycle it. Refusals are why the
        # recycle cannot be limited to timeouts and transport errors: the CLI answers "Sonnet 5 can't
        # help with this. Start a new session to continue." through the ORDINARY result path, and then
        # returns that same error for every later turn regardless of content — passing a fresh
        # session_id to an already-connected client does not clear it. On 2026-09-02 one false-triggered
        # ambient capture wedged the client for 85 minutes; every command in that window died behind it.
        self._recycle()
        if result.get("used_tools"):
            return result   # it acted, or may have: never replay side effects
        if limit_refused(result, self):
            return result   # refused for usage: a retry is refused too, and only delays the credits run
        # Nothing was called, so re-running is side-effect free and keeps the turn on the subscription
        # instead of spending the caller's fallback. A genuinely refusable turn just fails twice, fast.
        log(f"sdk turn failed ({result.get('raw_error')}) — recycled warm client, retrying once")
        retry = self._attempt(transcript, on_event)
        if not retry.get("ok"):
            self._recycle()   # the cold client failed too; don't hand the next turn a used one
        return retry


# ── Codex: the hub's last resort when Claude itself is down ────────────────────────────────────────
# Billed to the owner's ChatGPT plan through `codex exec`, which is the only programmatic way in: the plan has
# no API access. Used for a watch/phone/web hub turn that Claude could not answer at all (the subscription
# failed for a reason other than its usage limit, or the credits run failed too) and that called no tool
# first, so nothing is done twice. It gets the same MCP servers, start_session included, and a session it
# dispatches runs on Codex too (DATA_AGENT, `mcp_servers(codex_sessions=True)`). Each machine signs in to
# Codex separately (`codex login --device-auth`): a ChatGPT login copied between machines breaks when
# one of them rotates the refresh token.
CODEX_BIN = shutil.which(os.environ.get("CODEX_BIN", "codex"))


CODEX_TIMEOUT_S = float(os.environ.get("CODEX_TIMEOUT_S", "240"))


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        # Bare keys: `-c` takes quoted ones without complaint and then drops the table. Env var names and
        # header names are all bare-key safe.
        return "{" + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + "}"
    return json.dumps(str(v))


def codex_ready() -> bool:
    home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex")
    return bool(CODEX_BIN) and (home / "auth.json").exists()


def run_codex(prompt: str, *, manual: bool) -> dict:
    """One hub turn on Codex. Same result shape as an SDK turn; `used_tools` is True whenever it ran,
    since nothing here reports what it called. Codex has no fence (every MCP tool is pre-approved, sends
    and start_session included), so it refuses a command that was not started by a press."""
    if manual is not True:
        log_error("run_codex refused a command that was not started by a press")
        return {"session_id": None, "text": "", "result": "", "ok": False, "used_tools": False,
                "raw_error": "Codex only runs commands started by a press"}
    over = ["-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="medium"']
    for name, srv in mcp_servers(codex_sessions=True).items():
        key = f"mcp_servers.{name}"   # bare: `-c` splits the path on dots and does not unquote a segment
        if srv.get("type") == "http":
            over += ["-c", f"{key}.url={_toml(srv['url'])}"]
            if srv.get("headers"):
                over += ["-c", f"{key}.http_headers={_toml(srv['headers'])}"]
        else:
            over += ["-c", f"{key}.command={_toml(srv['command'])}", "-c", f"{key}.args={_toml(srv.get('args', []))}"]
            if srv.get("env"):
                over += ["-c", f"{key}.env={_toml(srv['env'])}"]
        over += ["-c", f'{key}.default_tools_approval_mode="approve"']
    fd, out = tempfile.mkstemp(prefix="voice-codex-", suffix=".txt")
    os.close(fd)
    full = HUB_SYSTEM + "\n\n" + SHARED_STATE_NOTE + "\n\n" + prompt
    try:
        proc = subprocess.run([CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                               "-s", "read-only", *over, "-o", out, "-"],
                              input=full,   # stdin: a day of replayed context outgrows Linux's 128KB per-argument cap
                              cwd=os.path.expanduser("~"), capture_output=True, text=True, timeout=CODEX_TIMEOUT_S)
        text = pathlib.Path(out).read_text().strip()
        ok = proc.returncode == 0 and bool(text)
        return {"session_id": None, "text": text, "result": text, "ok": ok, "used_tools": True,
                "raw_error": None if ok else f"codex exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}"}
    except Exception as e:  # noqa: BLE001
        return {"session_id": None, "text": "", "result": "", "ok": False, "used_tools": True,
                "raw_error": f"codex run failed: {e}"}
    finally:
        pathlib.Path(out).unlink(missing_ok=True)


SDK = SdkRunner()


# Connected only when a turn first needs it, and dropped once the subscription answers again, so a machine
# that never hits the limit never holds a second Claude process.
SDK_CREDITS = SdkRunner(credits=True)


def limit_refused(result: dict, runner: "SdkRunner | None" = None) -> bool:
    """The subscription refused this turn for its usage limit: the stream said so, or the CLI's message
    did ("You've hit your 5-hour limit · resets 2:30am")."""
    e = (result.get("raw_error") or result.get("result") or "").lower()
    return (runner or SDK).limit_hit or bool(re.search(r"hit your .{0,30}limit", e))
