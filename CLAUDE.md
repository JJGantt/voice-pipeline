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

## Both mics must endpoint identically

The ESP and the Mac hear the same sentence, and the worker collapses the pair by *recording-window
overlap* (`_duplicate_of_recent`), keeping whichever arrived first. That only holds while the two agree
on where the sentence ended, so the endpointing constants in `voice-id/wakefree.py` and
`server/server.py` are one setting written twice — change both together.

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

## Sessions nobody dispatched are adopted, but never take the follow-up pointer

The Mac recognises `claude` processes it did not start — a terminal Jared opened himself — and
publishes them into `desk_sessions` with `origin='adopted'`, so liveness and the last reply work the
same for everything running. `discover.js` does the finding; adoption writes only `desk_sessions` and
**never `desk_focus`**. A dictated follow-up is aimed at work handed over, and the session he is
typing in is precisely what it must never land in.

Matching a process to its transcript is the hard part: there is no session id in the environment, and
Claude Code appends-and-closes the file so it is not held open either. Birth order is what works —
processes oldest-first, each taking the EARLIEST unclaimed transcript in its project directory born
within three minutes of its start. "Newest transcript since this process started" (which the LAUNCH
path can safely use, because it looks the instant it launches) is wrong here: measured on two live
sessions in one directory, both claimed the same file and each was reported saying the other's words.
No confident match means no row — a session reported with someone else's conversation is worse than
one not reported at all.

A process whose transcript already belongs to a row revives that row rather than making a new one,
which is what a manual `claude --resume` looks like from outside.

**A session outlives its transcript.** `/clear` starts a fresh file while the process keeps running,
so a row bound at adoption drifts onto a conversation that has ended — one tab reported yesterday's
subject and yesterday's last reply for hours after it had moved on. Each tick therefore looks for a
fresher transcript in the same directory (born after the process started, not bound to another live
session) and rebinds, resetting `mirror_offset` because the mirror is keyed by session id. An adopted
a session's NAME is refreshed the same way, in order of authority: a name Jared chose himself beats
the label the router wrote at dispatch, which beats Claude Code's own generated title.

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

`sweepRetention` in `pi-desk` is the ONE retention rule, on the always-on machine: closed sessions
(`ended`/`transferred`) go after 7 days, parked ones (`evicted`) after 14, the `desk_commands` log
after 30, and `voice_requests` — the raw captures — after 7. All tunable by env.

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

It reads only the last 512KB. These files reach tens of megabytes — one project directory here holds
31MB — and every tick would otherwise read all of it to learn the last few lines.

A closed session means the same thing on both machines: **`evicted` is parked and resumable** (a new
tab on the Mac, a new tmux session on the Pi), and `ended` means no transcript was ever bound, so
there is nothing to bring back.

The Mac's `deskSessions.js` tick is still smaller than the Pi's: no residency cap and no eviction of
its own. Those exist because a headless 4GB machine must decide what to close; on the Mac Jared can
see the tabs. What it does do each tick: adopt what it finds running, answer "is it still up" (a
`claude` process on the tab's tty — the tab outlives the process, so the tab is the wrong question),
publish the last completed turn, follow a `/clear` to the new transcript, and mirror the tail.

## Docs

`docs/ROUTING.md` is the routing contract. `ARCHITECTURE.md` covers the always-on machinery —
wake-free, speaker ID, feedback channels, settings. Each subdirectory has its own README.
