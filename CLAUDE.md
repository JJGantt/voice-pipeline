# Working in voice-pipeline

## There is one route, and it is dynamic

Every hub turn starts on **Sonnet** with `SAFE_TOOLS` in `--allowedTools`, and escalates to a fresh
**Opus** run with `ALL_TOOLS` when the turn emits `__ESCALATE__` or is denied a gated tool. Read
`docs/ROUTING.md` before touching `worker.py`'s routing — it covers what is gated and why, and the
two things that break silently:

- **`SAFE_TOOLS` must stay an allow list.** Converting it to `--disallowedTools` removes the gated
  tools from the schema instead of denying them on call, so Sonnet can no longer see that it needs
  one — escalation stops happening and every gated write starts answering "I can't do that".
- **A new data-layer tool is invisible to voice until it is named there.** `validate_safe_tools()`
  logs the drift in both directions at startup; read that line after adding a tool.

`voice_requests.tier` is still written by every capture path and read by nothing — the wake word that
sets it (`hey_jarvis` vs `hey_rhasspy`) no longer changes how a turn runs.

## `claude -p` must not see `ANTHROPIC_API_KEY`

The CLI silently prefers it over the subscription login and bills the API. `run_claude()` and
`pi-desk/launch.sh` each strip it from the subprocess environment; the service env keeps it because
`ApiRunner` (the Fly path) needs it. Anywhere new that spawns Claude has to strip it too.

## `server/` is a build context, not source

`assemble.sh` does `rm -rf` on `server/data-layer`, `server/mcp-voice`, `server/govee-mcp` and
`server/worker.py`, then recopies them from `~/repos/data-layer`, `mcp-voice/`, `~/repos/govee-mcp`
and `worker/`. Editing a copy is lost work. Deploy with:

    cd server && ./assemble.sh && flyctl deploy --now -a jared-voice

`server/server.py` is the one file there that is genuinely source.

## `fly-worker/` is dead

No `fly.toml`, no `Dockerfile`, nothing deploys it. The real Fly worker is `server/worker.py`,
assembled from `worker/worker.py`. Do not read it for current behaviour and do not edit it.

## Never commit a voiceprint

`voice-id/exemplars.npy` and `reference.npy` are biometric data and are gitignored. The live copy
lives in `app_settings.voiceprint`, uploaded by `enroll.py`; regenerate rather than copy one around.

## The slow polls are deliberate

Realtime is the pickup path everywhere here; every poll cadence is a backstop and tightens only when
`isHealthy()` says realtime is down. At 1s the three workers cost ~280k empty reads a day against the
free-plan quota. If pickup feels slow, fix the subscription — do not shorten the poll.

Supabase realtime channels do not self-heal: supabase-js leaves a dead channel in `CHANNEL_ERROR`
forever. Subscribe through `orchestrator/src/realtime.js`'s `watchTable` (the worker's
`RealtimeWaker` is the Python twin) rather than calling `.channel()` directly, and pass an explicit
`ws` transport — the Pi runs Node 20, which has no native WebSocket, and supabase-js there reports
"realtime unavailable" and silently polls forever.

## On the Pi, `gh auth setup-git` is a prerequisite

`pi-desk` clones with `gh` (which carries its own auth) but *pulls* with plain git over HTTPS. With
no credential helper the pull prompts for a username, fails instantly in a daemon, and every session
after the first runs against a stale tree. The pull failure is non-fatal by design, so it shows up as
wrong results rather than an error.

## Docs

`docs/ROUTING.md` is the routing contract. `ARCHITECTURE.md` covers the always-on machinery —
wake-free, speaker ID, feedback channels, settings. Each subdirectory has its own README.
