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

`voice_requests.tier` is a dead column: nothing writes or reads it (historical rows only). The two
wake words ("Hey Rhasspy" / "Hey Jarvis") are equivalent triggers — routing is the worker's dynamic
dispatch, and `app_settings.wakefree_route` is likewise an inert leftover row.

## The worker never bills the API — `ANTHROPIC_API_KEY` is discarded

Every turn, on every worker (Pi, Mac, and Fly, which holds its own `CLAUDE_CODE_OAUTH_TOKEN`), runs on
the Claude subscription. A run that fails transiently (overloaded, 5xx, rate limit, network) is retried
on the subscription (`run_subscription`, `SUBSCRIPTION_RETRY_S`) and then surfaced as an error; there
is no paid fallback: one at the same model tier turns a passing "529 Overloaded" on an Opus turn into
a Console-billed Opus run with thinking and the full toolset.

The CLI and the Agent SDK silently prefer the key over the subscription login and bill the API, so
`worker.py` pops it from the environment at import and never reads it; nothing it spawns can inherit
it. `pi-desk/launch.sh` strips it separately (it sources the worker's `.env` itself).

## `server/` is a build context, not source

`assemble.sh` does `rm -rf` on `server/data-layer`, `server/mcp-voice`, `server/govee-mcp` and
`server/worker.py`, then recopies them from `~/repos/data-layer`, `mcp-voice/`, `~/repos/govee-mcp`
and `worker/`. Editing a copy is lost work. Deploy with:

    cd server && ./assemble.sh && flyctl deploy --now -a jared-voice

`server/server.py` is the one file there that is genuinely source.

## `fly-worker/` is dead

No `fly.toml`, no `Dockerfile`, nothing deploys it. The real Fly worker is `server/worker.py`,
assembled from `worker/worker.py`. Do not read it for current behaviour and do not edit it.

## Both mics must endpoint identically

The ESP and the Mac hear the same sentence, and the worker collapses the pair by *recording-window
overlap* (`_duplicate_of_recent`), keeping whichever arrived first. That only holds while the two agree
on where the sentence ended, so the endpointing constants in `voice-id/wakefree.py` and
`server/server.py` are one setting written twice — change both together. The same goes for where a
sentence STARTED: `PREROLL_S`, `HISTORY_S` and the `BACKFILL_*` constants (how far an accepted capture
reaches back for the words spoken before its trigger) live in both files and must match, as do the
whole-clip accept defaults (`ACCEPT_THR_DEFAULT` / `ACCEPT_THRESHOLD`, `ACCEPT_MAX_S`).

When they disagree the failure is silent and total. The mic that closes early enqueues a mid-sentence
fragment; the fragment is discarded as a false positive; the other mic's *complete* capture then
overlaps it and is dropped as the duplicate. Nothing runs, and nothing anywhere reports an error.
Dedupe therefore skips rows whose `result` is in `NO_WORK_RESULTS` — a request that executed nothing
can never stand in for one that would have.

## Never commit a voiceprint

`voice-id/exemplars.npy` and `reference.npy` are biometric data and are gitignored. The live copy
lives in `app_settings.voiceprint`, uploaded by `enroll.py`; regenerate rather than copy one around.

## The slow polls are deliberate

Realtime is the pickup path everywhere here; every poll cadence is a backstop and tightens only when
`isHealthy()` says realtime is down. At 1s the three workers cost ~280k empty reads a day against the
free-plan quota. If pickup feels slow, fix the subscription — do not shorten the poll.

**An idle cycle asks ONE question: is anything pending?** `IDLE_POLL_S` is 60s. The two other reads it
used to make — "does a live context window belong to someone else?" (`should_defer` → `context_owner`)
and "are there orphaned claims?" (`reclaim_stale`) — answer nothing while there is nothing to claim, so
`claim_one` reads `pending` FIRST and those two run on their own 60s clocks. Deferring is still decided
fresh, before any claim, the instant something IS pending. Putting either back on the cycle costs
~14k reads a day to learn nothing.

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

## A process's conversation is read from Claude Code's own record — never matched or guessed

The Mac recognises `claude` processes it did not start — a terminal Jared opened himself — and
publishes them into `desk_sessions` with `origin='adopted'`, so liveness and the last reply work the
same for everything running. Adoption writes only `desk_sessions`, never `desk_focus`: unpinned, focus
is computed from activity (`active_at`), not stored.

Which conversation a process is in comes from `~/.claude/sessions/<pid>.json`, which Claude Code keeps
for every live process: the conversation id (the transcript's filename) and the working directory. It
rewrites the id on `/clear`, deletes the file when the process exits, and says whether a turn is in flight
and since when — the source of `working` and `active_at`. `orchestrator/src/discover.js`
reads it on both machines; on the Pi the tmux pane's pid is claude's because `launch.sh` `exec`s it.
Do not go back to matching transcripts by birth time or recency: every session in the home directory
shares one project directory, so each new session there looked like a `/clear` of every older one —
336 rebinds against 42 adoptions, and rows reported saying each other's words.

A row belongs to a PROCESS (`pid`), not a Terminal tab: a later `claude` in the same tab is a new row,
and a row whose process has exited is closed. A process whose conversation already belongs to a row
revives that row rather than making a new one, which is what a manual `claude --resume` looks like.

`ps` must list `comm` LAST. macOS truncates it to 16 characters when another column follows, and a
`claude` launched by full path then reads as `/Users/jaredgant` and is not seen at all.

**A session outlives its transcript.** `/clear` starts a fresh file while the process keeps running.
Each tick reads the process's record again and rebinds the row to whatever conversation it names,
resetting `mirror_offset` because the mirror is keyed by session id — and clearing the ABANDONED
conversation's mirror (`mirrorReset`) as it goes. Nothing will ever point at those parts again: the row
names a different conversation from that moment, so retention cannot reach them either. Without that
they stay in the bucket for ever; 65 conversations and 104MB had accumulated by 2026-09-16. A session's NAME is refreshed
every tick too, in order of authority: a name Jared chose himself beats the label the router wrote
at dispatch, which beats Claude Code's own generated title.

He can choose it two ways, and they are the same act: `/rename <name>` typed into the session, or
ctrl-R in the session picker. Both make Claude Code write a `custom-title` record into the transcript,
which is what gets read back here.

**Renaming from the hub or by voice sends `/rename` into the live session** rather than writing that
record ourselves. Tested both: appending the record directly works for everything that READS the
transcript, but the running session never re-reads its own file, so its terminal tab keeps the old
title. `/rename` renames the session itself — tab included — and it writes the record for us.

That order is the point. A generated title describes the content, so **two different sessions about
the same subject get the same name** — 'Pull day workout log' is the title of nineteen transcripts
here. The only reliable way to tell two apart is to name one.

**This is Mac-only on purpose.** A transcript only rolls when someone types `/clear`; COMPACTION does
not roll it — it writes `compactMetadata` and `isCompactSummary` records into the same file (verified
across the transcripts on disk). Pi sessions are headless in tmux with nobody typing in them, so
there is nothing there to follow. Adding the same rebinding to `pi-desk` would be code for a case
that cannot happen unless Jared attaches to the tmux session himself and clears it.

## Nothing in here is kept for ever

`sweepRetention` in `pi-desk` is the retention rule for this system, on the always-on machine: closed
sessions (`ended`/`transferred`) go after 7 days, parked ones (`evicted`) after 14, the `desk_commands`
log after 30, and `voice_requests` — the raw captures — after 7, together with the phone recordings
behind them in the `voice-clips` bucket. All tunable by env. Photo attachments are the one exception:
the hub sweeps its own `voice-attachments` bucket after 7 days.

Deleting a voice request does **not** delete the conversation: `voice_messages` has no cascade from
it (verified by deleting one and counting), so the readable thread survives and only the capture row
goes.

Deleting a row does not destroy a conversation — the transcript stays on disk on the machine that ran
it, and `claude --resume` still works. What goes is the row that OFFERS it in the pane, plus the
mirrored copy in storage, cleared in the same step so the bucket cannot fill with transcripts of
sessions nothing can name any more.

Dead letters are deliberately exempt: an unretried one holds words Jared may still want, and a
retried or dismissed one is a decision worth keeping.

## Moving a session between machines

A session is an append-only transcript plus a working directory, so handing one over is copying the
file and resuming it. Both consumers mirror the tail of every transcript to the `transcripts` storage
bucket at the END OF A TURN (`mirror.js`), which is the only moment it is a complete thought. Only
the delta goes up — one transcript here is 16MB and a turn adds tens of KB — as
`<claude_session_id>/<offset>.part`, and `desk_sessions.mirror_offset` is how much is already there.

The one rewrite that makes a transcript portable is the home directory: `/Users/jaredgantt` on the
Mac, `/home/jaredgantt` on the Pi. `localize()` does it on the way down. Without it the resumed
session points at a directory that does not exist.

**ONE WRITER, ALWAYS.** `claude --resume` on a transcript that is live elsewhere does not reattach —
it starts a second process replaying the same file, and the two diverge with one id between them
(measured: 11 records became 27 on the Pi while the Mac's copy stayed at 11). So a transfer is
claimed by the machine GIVING THE SESSION UP: it closes the session, flushes the last of the mirror,
and only then enqueues `resume_mirrored` for the destination.

When the source machine is unreachable there is nothing to close and nothing left to flush, so the
destination is asked directly and the old row is marked `transferred`. That is the case the whole
mechanism exists for — the laptop is shut and the work should carry on — and it costs whatever the
session said after its last completed turn, which is nothing, because that is when mirroring runs.

Closing a session on the Mac is `/exit` typed into the tab, then SIGTERM if it is ignored. A Terminal
TAB does not understand AppleScript's `close` (it answers -1708), which is why `closeTabForTty` used
to report failure and close nothing; the window is closed only when that tab is the last one in it.

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
- **Whatever is typed goes inside bracketed-paste markers.** `do script` writes the text and its
  return as one chunk, and Claude Code reads a long chunk as a paste, so the return lands inside it
  and the message sits in the composer unsent while the command reports `done`. Measured on an idle
  session: 8 characters submitted, 105 did not. With `ESC[200~ … ESC[201~` around the text the return
  is a keypress at any length, and a slash command (`/rename`) still runs. The Pi hits the same thing
  through tmux and sends Enter as a separate call.

Three approaches that look right and are not: chunking (`do script` appends a return, so every chunk
submits as its own message), System Events `keystroke` (types without a return, but needs
Accessibility permission a launchd daemon does not have), and `claude --resume <id> "$(cat …)"` in a
fresh tab (resume on a LIVE session starts a second process replaying the same transcript, so the
conversation forks into two copies with one id).

The Pi needs none of this: tmux writes into the pane with its own flow control — measured, 3000
characters in, 3000 out.

`orchestrator/src/transcripts.js` is shared by both machines. `readTail` reports the FINAL message of
a turn, identified by the transcript record whose `stop_reason` is `end_turn` — not "the last
assistant text", which mid-turn is a half-written thought. And only the text after the turn's last tool
call: text before one is narration, which `sessionWatch.js` streams as progress, and joined onto the
reply it opened every answer and reply notification with "Checking X first...". `working` is what separates them: a reply
is final only when `working` is false.

It reads only the last 512KB. These files reach tens of megabytes — one project directory here holds
31MB — and every tick would otherwise read all of it to learn the last few lines.

A closed session means the same thing on both machines: **`evicted` is parked and resumable** (a new
tab on the Mac, a new tmux session on the Pi), and `ended` means no transcript was ever bound, so
there is nothing to bring back.

The Mac's `deskSessions.js` tick is still smaller than the Pi's: no residency cap and no eviction of
its own. Those exist because a headless 4GB machine must decide what to close; on the Mac Jared can
see the tabs. What it does do each tick: bind each running row to its process (`pid`) and that
process's current conversation, close a row whose process has exited, adopt any process no row owns,
publish the last completed turn and the last turn boundary (`active_at`), and mirror the tail.

## Docs

`docs/ROUTING.md` is the routing contract. `ARCHITECTURE.md` covers the always-on machinery —
wake-free, speaker ID, feedback channels, settings. Each subdirectory has its own README.
