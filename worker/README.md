# Voice-command worker daemon

Claims voice commands from the Supabase `voice_requests` queue and runs each one
through a headless `claude -p` session on the **Claude subscription** (no API
key), with the `data` MCP attached, then writes the conversation back to Supabase
(`voice_messages`) for a live display.

The same `worker.py` runs on the **Mac** (`WORKER_ID=mac`) and later the **Pi**
(`WORKER_ID=pi`). Only the priority ordering differs.

## What it does (per loop iteration)

1. **Heartbeat** — every 10s, upsert `worker_presence` `{worker, last_seen, info:{}}`.
2. **Claim (serialized, never concurrent)** — poll the oldest `pending` request.
   - **Priority gate:** if any worker ranked *higher* than me in `WORKER_PRIORITY`
     has a `last_seen` within 30s, skip and let them take it.
   - **Atomic claim:** `PATCH ...&status=eq.pending` → `claimed`. If the returned
     representation is empty, someone else won the race — skip.
3. **Process** the claimed request:
   - Read the `voice_focus` singleton. **Phase 1 only handles `focus == 'hub'`.**
     If focus is anything else (a specific Mac session id), the request is
     released back to `pending` and skipped — never crashes. See Phase-2 TODO.
   - Write the user turn to `voice_messages`.
   - Manage the shared **hub session** (resume vs. fresh — see reset rule below).
   - Run `claude -p` headless with the `data` MCP, parse `stream-json`, capture
     the `session_id` and the final assistant text.
   - Write the assistant turn to `voice_messages` and mark the request `done`
     (or `error` on any failure).
4. Idle-poll every ~1s; drains the queue fast when busy. Clean shutdown on
   SIGINT/SIGTERM (finishes the current iteration, then exits).

## Environment variables

| Var | Default | Notes |
|-----|---------|-------|
| `SUPABASE_URL` | `https://felyggqjjhltwokdfhop.supabase.co` | |
| `SUPABASE_KEY` | *(required, no default)* | **Must be the service_role key** — it bypasses RLS and needs full read/write on `voice_requests`, `voice_messages`, `worker_presence`, `voice_focus`. The anon key will NOT work. |
| `WORKER_ID` | `mac` | Also `pi`. Identifies this worker in the queue + presence. |
| `WORKER_PRIORITY` | `mac,pi` | Ordered, highest first. A worker only claims if no higher-priority worker has a fresh heartbeat. |
| `STATE_DIR` | `~/.voice-worker` | Stores `hub_session.json` (session id + last activity). |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI. |
| `DATA_MCP_JS` | `/Users/jaredgantt/repos/data-layer/dist/mcp.js` | The built data MCP server entrypoint. |
| `CLAUDE_TIMEOUT_S` | `300` | Max seconds for a single headless `claude` run. |

### Where to get the service key

It's in the `data` MCP server's `env` block in `~/.claude.json`
(`mcpServers.data.env.SUPABASE_KEY`), and hardcoded in
`/Users/jaredgantt/repos/data-layer/src/client.ts`. The worker takes it **from
the `SUPABASE_KEY` env var only** — it does not read those files at runtime.
`run.sh` requires you to export it.

## Running

```bash
# Mac (default priority puts mac first)
SUPABASE_KEY=<service-role-key> ./run.sh

# Pi (same code; just change the worker id)
WORKER_ID=pi SUPABASE_KEY=<service-role-key> ./run.sh

# Single iteration, for testing (claims+processes at most one request, then exits)
SUPABASE_KEY=<service-role-key> ./run.sh --once
```

`run.sh` auto-selects a Python that has `httpx`: it prefers the voice-pipeline
server venv (`server/.venv`), then Homebrew `python3.11`, then plain `python3`.
The system `python3` (3.14) does **not** have `httpx` installed.

Make sure `ANTHROPIC_API_KEY` is **unset** in the worker's environment so
`claude` uses the subscription rather than billing the API.

### Mac → Pi priority

With `WORKER_PRIORITY=mac,pi` (the default on both machines):
- The **Mac** never defers (nothing is ranked above it).
- The **Pi** checks `worker_presence` before claiming; if the Mac's heartbeat is
  within 30s, the Pi defers. If the Mac is asleep/offline (no fresh heartbeat),
  the Pi takes over automatically. When the Mac comes back, it resumes claiming
  and the Pi defers again.

This gives seamless failover with no coordination beyond the shared presence
table — and it's safe even if both run, because the claim PATCH is atomic
(`status=eq.pending` guard + `return=representation` race check).

## Hub-session reset rule

The shared "hub" conversation is one continuous `claude` session, reused via
`--resume <session_id>` stored in `STATE_DIR/hub_session.json`. It is **reset**
(started fresh, no `--resume`) when any of these hold:

- no stored session id yet, **or**
- last activity was more than **2 hours** ago, **or**
- last activity was on a **different calendar day** than now.

Otherwise the existing session is resumed so context carries across commands.
After every successful run the captured `session_id` + `last_activity` are
re-written.

## allowedTools / permission finding (blast-radius note)

The voice path runs `claude -p` with:

```
--allowedTools "mcp__data" --strict-mcp-config --mcp-config <tmpfile>
```

`--allowedTools "mcp__data"` prefix-matches **all** `mcp__data__*` tools and
nothing else — no `Bash`, `Edit`, `Read`, or `Write`. This bounds the blast
radius of an arbitrary voice command to data operations only.

**Verified empirically (2026-06-19):** in headless `-p` mode, a tool not in
`--allowedTools` is **denied, not prompted**, so the run never hangs on a
permission dialog. A data-reading prompt loaded the MCP
(`mcp_servers: [{name: data}]`), called `mcp__data__list_reminders`
successfully (`is_error: None`), and returned a real answer — with no prompt and
no need for `--permission-mode acceptEdits`. So `--allowedTools "mcp__data"`
alone is sufficient. (If a future change ever surfaces a prompt for MCP tools,
the fallback to try is adding `--permission-mode acceptEdits`.)

Note: on accounts with many MCP tools the model may reach a data tool via the
built-in `ToolSearch` deferred-tool mechanism first; that still resolves to the
allowed `mcp__data__*` tool and runs fine.

## Phase-2 TODO: `focus != 'hub'` routing

`voice_focus.focus` can point at a specific **Mac session id** instead of
`'hub'`, meaning "send this command into that live session" rather than the
shared hub conversation. Phase 1 does **not** implement that — when focus is not
`'hub'`, `process_request()` releases the request back to `pending` and returns
(so it isn't lost and the worker doesn't crash).

Phase 2 will need to: resolve the focused session id, deliver the transcript to
that running session (e.g. via the session's own resume/IPC channel rather than
the hub session), and stream its output back into `voice_messages` under that
focus. Until then, set `voice_focus.focus = 'hub'` for the worker to act.

## Streaming (nice-to-have, not in v1)

v1 writes the assistant turn once, after the run completes. Incrementally
updating the `voice_messages` row as `stream-json` arrives (for a token-by-token
live display) is a future enhancement — the parsing loop in `run_claude()`
already iterates the stream events, so it's the natural hook point.
