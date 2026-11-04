# fly-desk: the last-resort session machine

Fly app `jared-desk-fly`. It runs dispatched Claude sessions when neither the Pi nor the Mac can take
one: data-layer `desk.ts` `startSession` tries the machine asked for, then the other, and only then
wakes this. The host is `pi-desk/index.js` itself with `DESK_MACHINE=fly`, so launching, watching,
parking, follow-ups, resume and transfer all behave as on the Pi.

## How it sleeps and wakes

- The machine is **stopped almost always**. The dispatcher (and the hub, for a follow-up to one of its
  sessions) requests `https://jared-desk-fly.fly.dev/wake`; Fly's proxy starts the machine and holds the
  request until the host is listening. Then the command is written and drained as usual.
- It stops itself: idle sessions are parked after `PI_DESK_IDLE_MAX_S` (30 min), and once nothing has
  run for `FLY_IDLE_EXIT_S` (10 min) the host exits 0. The restart policy is `on-failure`, so a clean
  exit leaves it stopped.
- `auto_stop_machines` must stay `off`. A session at work makes no HTTP requests, and the proxy would
  read that as idle and stop the machine mid-turn.

## Incident runs from the ops responder

When Supabase is down nothing can be dispatched (every route in reads Supabase), so the ops responder
(health/responder/README.md) wakes this machine and posts the incident to `POST /incident` (Bearer
`OPS_TOKEN`). It runs as `claude -p` in a `health` checkout, started through that repo's
`responder/bin/incident-claude` (refused if the launcher is missing), logs to `$HOME/incident-<run>.log`,
and reports the run done to the responder when it exits. It counts as work for the idle exit, so the
machine stays up until it finishes. It does not appear in the session list, which lives in Supabase.

## What differs from the Pi

- **Credential:** the year-long `CLAUDE_CODE_OAUTH_TOKEN` (there is no interactive login), so sessions
  have no Remote Control and no Claude in Chrome. Its heartbeat carries no `login_expires_at`.
- **Permissions:** an allow-list in `claude-config/fly/settings.json`, as on the Pi, not
  `--dangerously-skip-permissions`. An MCP server missing from that list stops a session on a prompt
  nobody can answer, so a server added in `entrypoint.sh` needs its line there too.
- **No cgroup budgets, no hand-offs to the Mac, no retention sweeps** (the Pi does those for everyone).
- **MCP:** data, gmail, govee-lights (for Claude in `~/.claude.json`, for Codex in `$CODEX_HOME/config.toml`). No iMessage (no tailnet here), no browser.
- `$HOME` is `/data/home` on the `desk_data` volume, so checkouts, transcripts and `~/.claude.json`
  survive a stop. The host's log is `/data/desk.log`.

## Codex

Codex sessions (`agent = 'codex'`, status-hub `docs/SESSIONS.md` § Codex sessions) run here too, once
Codex is signed in. The CLI is baked into the image (`deploy.sh` passes the newest `@openai/codex`);
`CODEX_HOME` is `/data/home/.codex` on the volume, and `entrypoint.sh` rewrites its `config.toml` every boot
with the same MCP servers as `~/.claude.json`, each with its tools approved. The login (`auth.json`) is
made once, by hand, with the machine awake: wake it (`curl https://jared-desk-fly.fly.dev/wake`), then
`fly ssh console -a jared-desk-fly -C 'codex login --device-auth'` and approve the code it prints. Until
then its heartbeat-less dispatch refuses a Codex session with "Codex is not signed in on the Fly machine".

## Secrets

`CLAUDE_CODE_OAUTH_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, `GH_TOKEN`, `GIT_USER_NAME` / `GIT_USER_EMAIL`
(the identity sessions here commit as), `FLY_API_TOKEN` (an org token), `SUPABASE_ACCESS_TOKEN`,
`GOVEE_API_KEY`, `GOOGLE_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN`, `OPS_TOKEN` (the ops responder's, for
`/incident`), `HUB_TOKEN` (the hub's kiosk token, which sessions here use through status-hub's `bin/hub`). A Google password change revokes the
Claude token here as everywhere; re-set it with `fly secrets set -a jared-desk-fly`.

## Deploy

`./deploy.sh` from a machine with checkouts of voice-pipeline, data-layer, govee-mcp and gmail-mcp. It
copies the sources into the build context (gitignored) and bakes in the newest Claude Code. A change to
`pi-desk/` or `orchestrator/src/` reaches this machine only through a deploy.

## Cost

shared-cpu-2x with 4GB, billed only while running, plus a 10GB volume (about $1.50/month).
