# Voice-command worker daemon

Claims voice commands from the Supabase `voice_requests` queue and runs each one
on the **Claude subscription** — hub turns on a warm Agent SDK client
(Opus at its default effort, full toolset), full-agent turns through headless
`claude -p` (Opus) — with the `data` MCP attached, then writes the conversation
back to Supabase (`voice_messages`) for a live display. Every turn runs on the Claude subscription;
there is no API-billed path (see `../docs/ROUTING.md`).

The same `worker.py` runs on **Fly** as its own app, `jared-voice-worker` (`WORKER_ID=fly`, built from
`../fly-worker/`), the **Pi** (`WORKER_ID=pi`) and the **Mac** (`WORKER_ID=mac`); only the environment
differs. How a claimed turn is
routed once it gets here is `../docs/ROUTING.md`.

## What it does (per loop iteration)

1. **Heartbeat** — every `HEARTBEAT_INTERVAL_S` (30s), upsert `worker_presence`
   `{worker, last_seen, info:{}}`. A deferring worker drops to 60s, which stays
   inside `HEARTBEAT_FRESH_S` so it never looks dead to the workers below it.
   A worker whose own Supabase calls hit a network error or 5xx in the last
   `NET_DEGRADED_S` (180s) beats `info:{degraded:true}` instead. A flaky link lets
   enough beats through to look alive while every claim and reply takes minutes,
   so freshness alone cannot be the test.
2. **Claim (serialized, never concurrent)** — woken by a Supabase realtime INSERT
   on `voice_requests`; the poll below is only a backstop.
   - **Priority gate:** if any worker ranked *higher* in `WORKER_PRIORITY` has a
     `last_seen` within `HEARTBEAT_FRESH_S` (90s) and is not degraded, skip and
     let them take it.
   - **Affinity override (full-agent mode only):** while `agent_mode` is on, a live
     context window stays pinned to the worker that claimed the last request, even
     against priority, until the window resets, the owner goes silent past
     `OWNER_GRACE_S`, or the owner reports itself degraded — a full-agent CLI session
     lives on one machine. A hub turn follows priority alone: it is a fresh run handed
     the day's thread from the live state every worker holds, so pinning it bought
     nothing and held commands on a slower worker for up to `session_reset_s`.
   - **Degraded stands down:** a degraded worker does not claim while any other
     worker is healthy.
   - **Failover deadline (`failover_wait`):** a request still pending `FAILOVER_S`
     (8s) after it was created is taken by whoever is looking — a healthy worker
     claims in about a second, so one that has not is not going to. The one
     exception is the worker it is left to being mid-run itself: a follow-up queues
     behind the run it follows, but for at most `BUSY_WAIT_S` (120s) of that run, so
     a hung run cannot hold every command behind it. Another worker's run is no
     reason to wait. A standing-by worker sleeps to exactly its deadline.
   - **Desk hosts are not workers.** `desk` and `pi-desk` beat into the same
     `worker_presence` table; every "is another worker up" question counts only
     `WORKER_PRIORITY` members, and a presence with `info.stopping` or
     `info.degraded` counts as down.
   - **Atomic claim:** `PATCH ...&status=eq.pending` → `claimed`. If the returned
     representation is empty, someone else won the race — skip.
   - **Handed-off requests:** a request listing workers in `failed_on` is never
     claimed by them again, and skips affinity: it defers only to a higher-ranked
     worker that has not tried it (see *Handing a failed turn on*).
3. **Process** the claimed request:
   - Read the `voice_focus` singleton. `focus == 'hub'` runs here; any other value
     names a live Mac session, and the transcript is injected into that session's
     pty through the orchestrator (`ORCH_URL`) instead of running a fresh turn.
   - Write the user turn to `voice_messages`.
   - Manage the hub session (resume vs. fresh — see the reset rule below).
   - Run the turn, parse `stream-json`, capture the `session_id` and the final text.
   - Write the assistant turn to `voice_messages` and mark the request `done`.
     A failed turn that called no tool is handed to another worker; only when no
     healthy worker is left untried is it marked `error`.
4. Idle-poll every `IDLE_POLL_S` (60s), tightening to 2s only while realtime is
   down and 120s while deferring. An idle cycle costs ONE read: `pending` is read
   first, and the defer check and the orphan-claim sweep run on their own 60s
   clocks instead of every cycle, because neither answers anything while there is
   nothing to claim. Clean shutdown on SIGINT/SIGTERM.

## Environment variables

| Var | Default | Notes |
|-----|---------|-------|
| `SUPABASE_URL` | `https://felyggqjjhltwokdfhop.supabase.co` | |
| `SUPABASE_KEY` | *(required, no default)* | **Must be the service_role key** — it bypasses RLS and needs full read/write on `voice_requests`, `voice_messages`, `worker_presence`, `voice_focus`. The anon key will NOT work. |
| `WORKER_ID` | `mac` | Also `pi`, `fly`. Identifies this worker in the queue + presence. |
| `WORKER_PRIORITY` | `fly,pi,mac` | Ordered, highest first. A worker only claims if no higher-priority worker has a fresh heartbeat. The same value is `run.sh`'s default, `worker.py`'s, the Pi's `.env` and the Fly image's; change all four together. |
| `STATE_DIR` | `~/.voice-worker` | Stores `hub_session.json` and `agent_session.json` (session id + last activity). `/data/voice-worker` on Fly, on the volume. |
| `HUB_MODEL` / `HUB_EFFORT` | `opus` / *(unset)* | The hub turn's model and effort; unset is the model's default (`docs/ROUTING.md`). |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI. |
| `DATA_MCP_JS` | `~/repos/data-layer/dist/mcp.js` | The **built** data MCP entrypoint — a source change there does nothing until `npm run build`. |
| `VOICE_MCP_JS` | `~/repos/voice-pipeline/mcp-voice/src/index.js` | The voice MCP (`say`, `set_focus`, `list_sessions`). |
| `GOVEE_MCP_PY` / `GOVEE_PY` | `~/repos/govee-mcp/govee_mcp.py`, `python3.11` | The lights MCP and the interpreter that has its deps. |
| `GMAIL_MCP_PY` / `GMAIL_PY` | `~/repos/gmail-mcp/gmail_mcp.py`, its `.venv` python | Gmail read/draft/send, Opus only. Registered when the script exists and its credential does — `gmail-mcp/.env`, or (Fly) the `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` env vars, which are passed through to it. |
| `MESSAGES_MCP_URL` | `http://127.0.0.1:27187/mcp` on the Mac, empty elsewhere; Fly's `entrypoint.sh` sets the Mac's tailnet address once Tailscale is up | iMessage (Opus only), always over HTTP: only the Mac's `messages-http` job has the Full Disk Access `chat.db` needs, so a stdio copy spawned here would fail every read. The Pi sets the Mac's address. |
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
out of the environment at import and never uses it: the worker only runs on the
subscription, and popping it means no spawned subprocess — the warm SDK client,
`claude -p`, the MCP servers — can inherit it and silently bill the API.

### On the Pi: systemd

The Pi runs this as `voice-worker.service`. The unit is versioned here and installed by copying:

    sudo install -m 644 ~/voice-pipeline/worker/voice-worker.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl restart voice-worker

A root-only drop-in, `/etc/systemd/system/voice-worker.service.d/oauth.conf`, adds an
`EnvironmentFile` and is machine-local: it is not in git, and the copy above leaves it alone.

`KillMode=mixed` is load-bearing (the unit's comment has the detail). Under the default, a restart
kills the Claude CLI mid-request and the worker pushes a false failure alert to the phone, and
`pull-repos` restarts this unit on every voice-pipeline push.

### Fly → Pi → Mac priority

`WORKER_PRIORITY` is `fly,pi,mac`, so **Fly claims whenever it is up**, the Pi is
the first fallback and the Mac the second. Fly is first on speed: measured on
identical commands it answers the common turn in 2.8s median against the
Pi's 4.4s, and Opus turns cost about the same on both, ~15s
(`../docs/WORKER-PLACEMENT.md`). It is also independent of the house. It cannot
reach iMessage, Gmail or the feeder; those are rare enough not to count against it.
The Mac is last because work started by voice can be long-running, and closing the
laptop must not kill it.

Each worker checks `worker_presence` before claiming and defers to any
higher-priority worker whose heartbeat is fresh. Failover needs no coordination
beyond that shared table, and is safe even if two claim at once, because the
PATCH is atomic (`status=eq.pending` guard + `return=representation` race check).

## Hub-session reset rule

Hub mode and full-agent mode keep **separate**, independently-persisted sessions
(`hub_session.json` / `agent_session.json` in `STATE_DIR`). Flipping the
`agent_mode` toggle changes which one a request resumes; it never clobbers the
other's context.

**The hub agent sees the whole day.** Every hub turn is a fresh run with the
conversation replayed into its prompt (`recent_context()`): everything said
since the day began at **4 AM Eastern** (`day_start`, `TZ_DISPLAY`, the hub's own
"today"), or since the last manual "Reset context" (`voice_reset_at`) if that is
later, oldest first, each line with its time, room chatter marked as ignored. An
idle gap does not cut it, and a host's own timezone does not move the
boundary. A heavy day is about 12k tokens.

**None of that is read per command.** The day's thread, the voice focus and the settings a turn consults
(`agent_mode`, `agent_cwd`, `session_reset_s`, `voice_reset_at`) are held in memory (`_live`): loaded
when the realtime socket subscribes, and kept current by that socket's `voice_messages`, `voice_focus`
and `app_settings` events. The socket is recycled every ~5 minutes and each subscribe reloads, which is
the backstop for a channel that goes deaf while still reporting SUBSCRIBED. Read per command, these
cost ~0.7s before a turn could start; from memory they are microseconds.

**The lights are not listed in the prompt.** A turn that needs them calls govee's `list_lights`.

**Only an always-on capture is checked for a duplicate.** Two room mics can hear one utterance; a
press, a watch command or a typed message comes from one device on purpose, so it skips
`_duplicate_of_recent` and the read of the last 12 requests it costs. `session_reset_s` still bounds which
worker owns the conversation in full-agent mode (affinity); it no longer bounds what the agent
remembers.

The agent session resets only on a manual reset.

## allowedTools (blast-radius note)

A hub turn runs with an **explicit list** of tools (`ALL_TOOLS`: the MCP servers, the web, mail
and messages) — never `Bash`, `Edit` or `Write`. `Read` is allowed only inside the current
turn's photo directory, which is how the model views an image file. The warm client enforces the
list with a PreToolUse hook (`_fence`), because Claude Code also applies the host's own permission
settings; `../docs/ROUTING.md` has why.

Full-agent mode is the opposite shape: `--dangerously-skip-permissions` in
`agent_cwd`, every native tool live. It exists for dictated coding work and is
gated behind the `agent_mode` setting.

## Handing a failed turn on

A failure on one machine is that machine's until every machine has tried the command. When a turn
fails after its retries and **called no tool**, `hand_off` puts the request back to `pending`, adds
this worker to `voice_requests.failed_on`, and the realtime wake brings the next healthy worker straight
to it — no waiting out `FAILOVER_S`. Only when no healthy worker in `WORKER_PRIORITY` is left untried is
the request closed as `error`. Nothing is written to the thread before the handoff, so the worker that
succeeds writes the whole turn.

A turn that called any tool is never handed on: it may already have logged a meal or sent a message,
and running it again would do that twice. Those still end as `error` where they failed.

`claude_unready()` checks that `CLAUDE_BIN` resolves to something executable (about 13µs on the Pi).
It runs BEFORE a claim: a host that fails it marks everything pending as tried with one write per row
and claims nothing, and that write is the realtime wake for the next worker. Claiming first and handing
off afterwards cost 1.17s on the Pi, all of it reads the turn does before reaching the check. It runs
again at dispatch, which covers an orphan taken over by `reclaim_stale`. A Claude Code
self-update through npm can die half way and leave the link dangling, or `claude.exe` as a 500-byte
non-executable placeholder.
Repairing it means clearing npm's leftover `.claude-code-*` temp directory under
`~/.npm-global/lib/node_modules/@anthropic-ai/` (npm refuses with `ENOTEMPTY` while it is there) and
reinstalling `@anthropic-ai/claude-code`.

## The last transcriber is the Mac

Every capture path tries Groq, then OpenAI's Whisper (`OPENAI_API_KEY`). When both fail on a watch or
phone command, the hub keeps the clip and queues the row `untranscribed` — only while the Mac worker is
heartbeating, since it is the only host with a local Whisper. The Mac's worker takes it on the realtime
event that attaches `audio` (and on each subscribe, for one it missed), runs `whisper-cli` with
`ggml-small.en`, and turns it `pending`. Any worker closes an `untranscribed` row older than
`UNTRANSCRIBED_MAX_S` (75s, under the 90s stuck-`transcribing` sweep) as an error and investigates.

## Orphans: a worker that dies mid-run

A claim carries `claim_epoch`, a random id per worker PROCESS that its heartbeat also publishes. A worker
restarted mid-run — a deploy, `pull-repos`, a SIGKILL at the unit's stop timeout — comes back under the
same name with a fresh heartbeat, so only the epoch shows its old claim is dead. The epoch counts only
when the owner's heartbeat is newer than the claim: a just-restarted worker claims before its first beat
lands, and sweepers cache presence for 30s, so a row older than the claim still carries the previous
process's epoch and would orphan a live claim the moment it is made. `reclaim_stale` (every
`RECLAIM_SWEEP_S` and at startup, whatever is pending) releases an orphan: back to `pending` with the
owner in `failed_on` when it had not acted, closed as an error and investigated when it had (it may be
half-done) or when it is older than the conversation window.

**Acted** means the turn called a tool that changes something: anything but `get_*`, `list_*`,
`search_*`, `find_*`, web search/fetch and the feeder's status reads (`replay_safe`). The first such call
stamps `acted_at`. A failure after reads only is still handed on; a crash in `process_request` is handed
on the same way.

A pending row whose `failed_on` already holds every healthy worker — the one it was handed to died
before claiming — is **stranded**; the same sweep clears its `failed_on` so everyone may try again.

On SIGTERM the worker heartbeats `info.stopping` at once and wakes its main loop, so others stop
deferring to it and an idle worker exits in a moment rather than at the stop timeout. On Fly,
`entrypoint.sh` restarts the worker whenever it exits.

## Failure alerts

When a command fails on a worker (handed on, passed on unclaimed, or ended as an error), the worker
reports `<stage> · <class> · <error>` to the ops responder (`alert_failure`; health/responder/README.md),
with the request and what became of it as the context the investigating agent is given. Class is one
of `auth`/`billing`/`rate-limit`/`error`. The responder groups repeats into one incident, starts one
agent for it and tells Jared; nothing in the worker throttles or dispatches. Overloaded, 5xx,
rate-limit and network errors are retried a few times before any of this; auth and billing are not.

Every other failure the worker logs goes the same way: `log_error` prints the line and reports it.
`log` is for lines that only announce a retry or a fallback. The reporter is `ops_report.py`, a copy of
health/responder's `report/report.py` (edit it there and copy it out); it reads `OPS_TOKEN` from the
environment or `~/.config/ops/env`.

The worker's heartbeat to the responder rides the heartbeat thread but is sent only while the main loop
has come round within `LOOP_STALL_S` (default 30 minutes, longer than any turn), so a wedged loop goes
quiet the way a dead worker does.

What the classes mean:

- `auth` — the setup token is revoked or expired. Re-mint: `claude setup-token` → this `.env` on
  every host + `fly secrets set CLAUDE_CODE_OAUTH_TOKEN=… -a jared-voice-worker` → restart. A Google
  password change revokes it (the claude.ai account signs in with Google).
- `billing` — the subscription reports a billing problem.
- `rate-limit` — transient and already retried; nothing to do unless it persists.
