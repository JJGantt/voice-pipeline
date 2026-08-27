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

## A command that cannot run is kept, never dropped

Both desk consumers write `desk_dead_letters` when a command expires unrun, errors, or was claimed by
a machine that then died — the whole payload, prompt included. A dispatched prompt is usually minutes
of dictated speech nobody can repeat, so losing one costs far more than the launch it was meant to
start.

It is **not** a retry queue, and adding automatic retry would be the wrong fix: work that could not
run when it was asked for has usually stopped being wanted by the time the machine comes back, and a
prompt that fires hours late is worse than one that never fired. Retry is a decision made afterwards,
from the saved list.

The other half is upstream: `data-layer`'s dispatch path checks the target machine is beating before
it writes a command at all, because the queue itself has no way to fail — an unclaimed command simply
expires while the caller has already been told a session started.

## Reading a session: two handles, two columns

`desk_sessions.tty` reaches a Mac session (its Terminal tab); `transcript_path` reads what any session
said. They were one column once, and a Mac row's "transcript path" was really a tty — which is why
nothing could read a Mac session's replies.

## A tty takes 1024 bytes, so nothing long is ever typed at the Mac

`do script` writes into a Terminal tab's tty, and a tty accepts 1024 bytes of input and silently
discards the rest — a 2500-character dispatch arrived as exactly 1024 characters, mid-word, leaving
the shell on an unterminated quote and no session at all. Both directions now go by reference:

- **Launching** puts the prompt in a file and lets the SHELL read it (`"$(cat …)"`), so the typed
  line is ~214 characters whatever was said. Same idea as the file `pi-desk` hands tmux.
- **A follow-up** cannot do that — it goes to Claude Code's composer, not a shell — so over ~900
  characters (or with any newline, which an AppleScript string literal cannot hold) the text is
  stashed in `~/.claude-desk-prompts/` and the session is told to read it, **echo it back verbatim
  first**, then act. The echo is what keeps the words visible in the session; without it the terminal
  shows a stub and the request lives only in a file.

Three approaches that look right and are not: chunking (`do script` appends a return, so every chunk
submits as its own message), System Events `keystroke` (types without a return, but needs
Accessibility permission a launchd daemon does not have), and `claude --resume <id> "$(cat …)"` in a
fresh tab (resume on a LIVE session starts a second process replaying the same transcript, so the
conversation forks into two copies with one id).

The Pi needs none of this: tmux writes into the pane with its own flow control — measured, 3000
characters in, 3000 out.

`orchestrator/src/transcripts.js` is shared by both machines. `readTail` reports the FINAL message of
a turn, identified by the transcript record whose `stop_reason` is `end_turn` — not "the last
assistant text", which mid-turn is a half-written thought. `working` is what separates them: a reply
is final only when `working` is false.

The Mac's `deskSessions.js` tick is deliberately much smaller than the Pi's: no residency cap, no
eviction, no resume. Those exist because a headless 4GB machine must decide what to close; on the Mac
Jared can see the tabs. It answers only "is it still up" (a `claude` process on the tab's tty — the
tab outlives the process, so the tab is the wrong question) and "what did it last say".

## Docs

`docs/ROUTING.md` is the routing contract. `ARCHITECTURE.md` covers the always-on machinery —
wake-free, speaker ID, feedback channels, settings. Each subdirectory has its own README.
