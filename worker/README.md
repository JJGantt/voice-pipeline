# Voice-command worker daemon

Claims voice commands from the Supabase `voice_requests` queue and runs each one
on the **Claude subscription** — ordinary turns on a warm Agent SDK client
(Sonnet, safe toolset), escalations and full-agent turns through headless
`claude -p` (Opus) — with the `data` MCP attached, then writes the conversation
back to Supabase (`voice_messages`) for a live display. The Console API key is
only a fallback (see `../docs/ROUTING.md`).

The same `worker.py` runs on the **Pi** (`WORKER_ID=pi`), the **Mac** (`WORKER_ID=mac`) and inside
the `jared-voice` Fly image (`WORKER_ID=fly`); only `WORKER_ID` differs. How a claimed turn is
routed once it gets here is `../docs/ROUTING.md`.

## What it does (per loop iteration)

1. **Heartbeat** — every `HEARTBEAT_INTERVAL_S` (30s), upsert `worker_presence`
   `{worker, last_seen, info:{}}`. A deferring worker drops to 60s, which stays
   inside `HEARTBEAT_FRESH_S` so it never looks dead to the workers below it.
2. **Claim (serialized, never concurrent)** — woken by a Supabase realtime INSERT
   on `voice_requests`; the poll below is only a backstop.
   - **Priority gate:** if any worker ranked *higher* in `WORKER_PRIORITY` has a
     `last_seen` within `HEARTBEAT_FRESH_S` (90s), skip and let them take it.
   - **Affinity override:** a live hub context window stays pinned to the worker
     that owns it, even against priority, until the window resets or the owner
     goes silent past `OWNER_GRACE_S` — a needless switch loses the conversation.
   - **Atomic claim:** `PATCH ...&status=eq.pending` → `claimed`. If the returned
     representation is empty, someone else won the race — skip.
3. **Process** the claimed request:
   - Read the `voice_focus` singleton. `focus == 'hub'` runs here; any other value
     names a live Mac session, and the transcript is injected into that session's
     pty through the orchestrator (`ORCH_URL`) instead of running a fresh turn.
   - Write the user turn to `voice_messages`.
   - Manage the hub session (resume vs. fresh — see the reset rule below).
   - Run the turn, parse `stream-json`, capture the `session_id` and the final text.
   - Write the assistant turn to `voice_messages` and mark the request `done`
     (or `error` on any failure).
4. Idle-poll every `IDLE_POLL_S` (15s), tightening to 2s only while realtime is
   down and 120s while deferring. Clean shutdown on SIGINT/SIGTERM.

## Environment variables

| Var | Default | Notes |
|-----|---------|-------|
| `SUPABASE_URL` | `https://felyggqjjhltwokdfhop.supabase.co` | |
| `SUPABASE_KEY` | *(required, no default)* | **Must be the service_role key** — it bypasses RLS and needs full read/write on `voice_requests`, `voice_messages`, `worker_presence`, `voice_focus`. The anon key will NOT work. |
| `WORKER_ID` | `mac` | Also `pi`, `fly`. Identifies this worker in the queue + presence. |
| `WORKER_PRIORITY` | `pi,mac,fly` (from `run.sh`) | Ordered, highest first. A worker only claims if no higher-priority worker has a fresh heartbeat. `worker.py`'s in-file default is `mac,pi`, which only applies if something starts it without `run.sh`. |
| `STATE_DIR` | `~/.voice-worker` | Stores `hub_session.json` and `agent_session.json` (session id + last activity). |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI. |
| `DATA_MCP_JS` | `~/repos/data-layer/dist/mcp.js` | The **built** data MCP entrypoint — a source change there does nothing until `npm run build`. |
| `VOICE_MCP_JS` | `~/repos/voice-pipeline/mcp-voice/src/index.js` | The voice MCP (`say`, `set_focus`, `list_sessions`). |
| `GOVEE_MCP_PY` / `GOVEE_PY` | `~/repos/govee-mcp/govee_mcp.py`, `python3.11` | The lights MCP and the interpreter that has its deps. |
| `GMAIL_MCP_PY` / `GMAIL_PY` | `~/repos/gmail-mcp/gmail_mcp.py`, its `.venv` python | Gmail read/draft/send. Skipped when the script is absent (Fly). |
| `MESSAGES_MCP_URL` | `http://127.0.0.1:27187/mcp` on the Mac, empty elsewhere | iMessage, always over HTTP: only the Mac's `messages-http` job has the Full Disk Access `chat.db` needs, so a stdio copy spawned here would fail every read. The Pi sets the Mac's address. |
| `MESSAGES_MCP_TOKEN` | *(none)* | That endpoint's bearer token, the same value as in `messages-mcp/.env`. Without it (or the URL) the iMessage tools are left out entirely. |
| `ORCH_URL` / `ORCH_TOKEN` | `http://localhost:7682` | The Mac orchestrator, for injecting into a focused session. |
| `CLAUDE_TIMEOUT_S` | `300` | Max seconds for a single hub run. |
| `AGENT_TIMEOUT_S` | `900` | Full-agent (coding) runs get a longer leash. |

### Where to get the service key

It's in the `data` MCP server's `env` block in `~/.claude.json`
(`mcpServers.data.env.SUPABASE_KEY`), and hardcoded in
`/Users/jaredgantt/repos/data-layer/src/client.ts`. The worker takes it **from
the `SUPABASE_KEY` env var only** — it does not read those files at runtime.
`run.sh` requires you to export it.

## Running

```bash
# Mac
SUPABASE_KEY=<service-role-key> ./run.sh

# Pi (same code; just change the worker id)
WORKER_ID=pi SUPABASE_KEY=<service-role-key> ./run.sh

# Single iteration, for testing (claims+processes at most one request, then exits)
SUPABASE_KEY=<service-role-key> ./run.sh --once
```

`run.sh` auto-selects a Python that has `httpx`: the dedicated worker venv
(`../.venv-worker`, which is what the Pi uses), then the server venv
(`server/.venv`), then Homebrew `python3.11`, then plain `python3`. The system
`python3` does **not** have `httpx` installed.

`run.sh` loads a gitignored `.env` beside it. `worker.py` pops `ANTHROPIC_API_KEY`
out of the environment at import: the Console key is the FALLBACK billing path
(read only by the `ApiRunner`), and popping it means no spawned subprocess — the
warm SDK client, `claude -p`, the MCP servers — can inherit it and silently bill
the API instead of the subscription.

### On the Pi: systemd

The Pi runs this as `voice-worker.service`. The unit is versioned here and installed by copying:

    sudo install -m 644 ~/voice-pipeline/worker/voice-worker.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl restart voice-worker

A root-only drop-in, `/etc/systemd/system/voice-worker.service.d/oauth.conf`, adds an
`EnvironmentFile` and is machine-local: it is not in git, and the copy above leaves it alone.

`KillMode=mixed` is load-bearing (the unit's comment has the detail). Under the default, a restart
kills the Claude CLI mid-request and the worker pushes a false failure alert to the phone, and
`pull-repos` restarts this unit on every voice-pipeline push.

### Pi → Mac priority

`run.sh` sets `WORKER_PRIORITY=pi,mac,fly`, so the **Pi claims whenever it is up**
and the Mac is the fallback. That inversion is the point: work started by voice
can be long-running, and closing the laptop must not kill it.

Each worker checks `worker_presence` before claiming and defers to any
higher-priority worker whose heartbeat is fresh. Failover needs no coordination
beyond that shared table, and is safe even if two claim at once, because the
PATCH is atomic (`status=eq.pending` guard + `return=representation` race check).

## Hub-session reset rule

Hub mode and full-agent mode keep **separate**, independently-persisted sessions
(`hub_session.json` / `agent_session.json` in `STATE_DIR`). Flipping the
`agent_mode` toggle changes which one a request resumes; it never clobbers the
other's context.

The hub window is **reset** (started fresh) when any of these hold:

- no stored session id yet, **or**
- last activity was longer ago than `app_settings.session_reset_s` (default
  **300s**), **or**
- last activity was on a **different calendar day** than now, **or**
- the hub enqueued the `__RESET_CONTEXT__` sentinel ("Reset context").

The agent session resets only on that manual reset.

## allowedTools (blast-radius note)

A hub turn runs `claude -p` with an **explicit list** of MCP tool names in
`--allowedTools`, plus `--strict-mcp-config --mcp-config <tmpfile>` — never
`Bash`, `Edit` or `Write`. `Read` is added only for photo turns, which is how the
CLI views an image file. In headless `-p` mode a tool not in the list is
**denied, not prompted**, so a run never hangs on a permission dialog — and that
denial is what escalation is built on. Which names go in which list, and why it
must never become `--disallowedTools`, is `../docs/ROUTING.md`.

Full-agent mode is the opposite shape: `--dangerously-skip-permissions` in
`agent_cwd`, every native tool live. It exists for dictated coding work and is
gated behind the `agent_mode` setting.

## Failure alerts

The worker pushes to Jared's phone (hub `/api/notify`, title `Voice worker`) when the subscription
path fails — even if the API fallback then completes the request — and when a request fails on both
paths. Body: `<host> · <stage> · <class> · <error>`, stage ∈ `subscription`/`request`, class ∈
`auth`/`billing`/`rate-limit`/`error`. One push per host per (stage, class) per `ALERT_COOLDOWN_S`
(default 3600). Not gated by the hub's push toggle; that toggle is for command results.

What the classes mean on the phone:

- `auth` — the setup token is revoked or expired. Re-mint: `claude setup-token` → this `.env` on
  every host + `fly secrets set CLAUDE_CODE_OAUTH_TOKEN=… -a jared-voice` → restart. A Google
  password change revokes it (the claude.ai account signs in with Google).
- `billing` — the Console balance is empty; the API fallback cannot complete anything.
- `rate-limit` — transient; nothing to do unless it persists.
