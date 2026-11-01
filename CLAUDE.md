# Working in voice-pipeline

## There is one route

Every hub turn, photos included, runs on **Opus at its default effort** (`HUB_MODEL` / `HUB_EFFORT`)
with the full toolset, on the warm SDK client. There is no fast first responder and no escalation —
Jared's call for quality over speed. Read `docs/ROUTING.md` before touching `worker.py`'s routing: it
covers what every turn is told, how sends are fenced per mic, and what to get right if a faster split
is ever rebuilt (gate by omission from an allow list, never `--disallowedTools`).

`voice_requests.tier` is a dead column: nothing writes or reads it (historical rows only). The two
wake words ("Hey Rhasspy" / "Hey Jarvis") are equivalent triggers — routing is the worker's one
route, and `app_settings.wakefree_route` is likewise an inert leftover row.

## Errors are reported, not just printed

The worker, the Fly voice server, `pi-desk` and the orchestrator send their failures and heartbeats to
the ops responder (health/responder/README.md), where a new incident starts an agent. The reporters
are copies of that repo's: `worker/ops_report.py`, `server/ops_report.py` and
`orchestrator/src/ops_report.cjs` (which `pi-desk` also loads). Edit
them there and copy them out. In the worker a failure is `log_error`, in the server `log.error`, in the
Node hosts `console.error`; `log` / `console.log` / `.catch(() => {})` are invisible to the responder and are only
for a retry or fallback that is already handling it. The Fly session host reports errors but sends no
heartbeat, because it sleeps whenever nothing runs.

## The worker bills the API in exactly one case: the subscription's usage limit

Every turn, on every worker (Pi, Mac, and Fly, which holds its own `CLAUDE_CODE_OAUTH_TOKEN`), runs on
the Claude subscription. A run that fails transiently (overloaded, 5xx, network) is retried on the
subscription (`run_subscription`, `SUBSCRIPTION_RETRY_S`) and then surfaced as an error, never moved
onto the key: that turns a passing "529 Overloaded" on an Opus turn into a Console-billed Opus run.

The one exception is a hub turn from the watch, phone or web (`source` not in `ROOM_MIC_SOURCES`, the
ESP puck and the Mac listener) that the subscription refuses for its
usage limit (`limit_refused`: the turn's `rate_limit_event` said `rejected`, or the CLI's "You've hit
your 5-hour limit"). It re-runs on `SDK_CREDITS`, a second warm client billed to `ANTHROPIC_API_KEY`,
which the Max plan's monthly Console credits pay for. It is not handed to another worker, since every
worker shares the account. Room-mic captures are never re-run there, wake-word ones included (they
arrive with `always_on` false but carry most of the overheard TV): judging room noise on the key is what
drained the Console balance once. `SDK_CREDITS` connects on first use and closes on the next turn
the subscription answers.

The CLI and the Agent SDK silently prefer the key over the subscription login, so `worker.py` pops it
from the environment at import into `CREDITS_API_KEY`, and only `SDK_CREDITS` passes it on, explicitly
(with `CLAUDE_CODE_OAUTH_TOKEN` blanked). Nothing else it spawns can inherit it. `pi-desk/launch.sh`
strips it separately (it sources the worker's `.env` itself). Fly holds it as a secret.

Every SDK turn's `rate_limit_event` also goes to Supabase `claude_usage` (`report_usage`), which is how
the system knows how close the 5-hour and weekly windows are.

## `server/` and `fly-worker/` are build contexts, not source

Two Fly apps, each deployed from its own directory, and most of what they ship is copied in at deploy
time. Editing a copy is lost work; edit the source.

- **`server/` → `jared-voice`**: the ESP wake-word/transcription server and the Govee inventory sync.
  `assemble.sh` copies `sync_devices.py` in from the govee-mcp checkout. `server/server.py` is the one
  file there that is genuinely source. Deploy with:

      cd server && ./assemble.sh && flyctl deploy --now -a jared-voice

- **`fly-worker/` → `jared-voice-worker`**: the voice worker. `deploy.sh` copies `worker/worker.py`,
  `mcp-voice/`, the data-layer source and `govee_mcp.py` in (all gitignored), then deploys with the
  newest Claude Code baked in. Deploy with:

      fly-worker/deploy.sh

  The worker is in an app of its own because, sharing `jared-voice`, a deploy of either restarted
  both and the worker shared one CPU with transcription.

Both scripts take the checkouts from `~/repos/...` on the Mac and `~/data-layer` / `~/mcp-govee` on the
Pi. Over SSH the Mac's flyctl has no token (its login is not reachable from a non-interactive shell), so
a deploy started from the Pi runs on the Pi, with `FLY_API_TOKEN=$(cat ~/.fly/token)` and
`~/.fly/bin` on `PATH`.

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

The other half is upstream: `data-layer`'s dispatch path checks the target machine is beating AND
signed in before it writes a command at all, because the queue itself has no way to fail — an
unclaimed command simply expires, and a session launched on an expired `/login` answers "Login
expired" and does nothing, while the caller has already been told a session started. Each desk
heartbeat carries `info.login_expires_at` (`orchestrator/src/login.js`, re-read when the credential
store changes); `desk.ts` `machinesReady` reads both beats and a new session goes to the other machine
when the chosen one fails either test. When both fail it goes to the Fly session machine
(`fly-desk/README.md`), which is woken by an HTTP request because it is stopped almost always; only if
that does not answer is the prompt dead-lettered.

## Status `fallback` belongs to the Pi alone

A `voice_requests` row with status `fallback` came from the watch through Supabase's `voice-fallback`
edge function (data-layer), because the hub on Fly did not answer. Only `FALLBACK_WORKER` (the Pi)
claims it, in the same read as its `pending` rows, with no deferring or holding. Every other worker's
claim filter must stay `status=eq.pending`: a Fly worker can be up while the hub is down, and a widened
filter there would put the fallback back on Fly. The run is the unrestricted Opus agent with
`fallback_directive`; `_hub_unreachable` skips the pushes to the hub for its duration, since each would
only spend its timeout. Status-hub `docs/WATCH.md` § When the hub does not answer has the watch's half.

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

Claude Code's per-conversation scratch directories (`/tmp/claude-<uid>/<project>/<id>/`) go the moment
their conversation stops running (`pi-desk/scratch.js`): on the Pi `/tmp` is RAM, an idle tmpfs page
lands in zram swap, and swap over `MAX_SWAP_PCT` holds every queued launch. A parked session resumes
with an empty scratch directory.

Dead letters are deliberately exempt: an unretried one holds words Jared may still want, and a
retried or dismissed one is a decision worth keeping.

## Moving a session between machines

A session is an append-only transcript plus a working directory, so handing one over is copying the
file and resuming it. Both consumers mirror the tail of every transcript to the `transcripts` storage
bucket at the END OF A TURN (`mirror.js`), which is the only moment it is a complete thought. Only
the delta goes up — one transcript here is 16MB and a turn adds tens of KB — as
`<claude_session_id>/<offset>.part`, and `desk_sessions.mirror_offset` is how much is already there.

A session the Pi moves mid-turn is closed with C-c — for memory (`pi-desk` `onHeavy`), or by a
`transfer_session` to the Mac that arrives while it is working, which is every session that moves
ITSELF because its work needs the Mac (the call is the step in flight). Both send `[Moved from the
Pi]` as the prompt that restarts the turn, plus the moved note; without them the session resumes
idle and the task stops. Claude Code
records that as Jared rejecting the step it was on, with an instruction to stop and wait. On the way
down `mirrorDown` rewrites those trailing records (`unmarkMoveInterrupt`) when the command says
`moved` + `mid_turn`; without it the resumed session obeys the fake rejection and the work stops.

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

A session that asks to close ITSELF is never closed by a command: closing it mid-turn kills it before
its reply is written. data-layer stamps `close_after_turn` instead, and the host (`closeAfterTurn`,
both machines) parks it once that turn's reply has been reported and the transcript mirrored.
status-hub `docs/SESSIONS.md` has the details.

Closing a session on the Mac is `/exit` typed into the tab, then SIGTERM if it is ignored. A Terminal
TAB does not understand AppleScript's `close` (it answers -1708), which is why `closeTabForTty` used
to report failure and close nothing; the window is closed only when that tab is the last one in it.
A dispatched session's launch command ends in `exit 0`, so when the session ends by any route its
shell goes too: with the Terminal profile's "When the shell exits: Close if the shell exited cleanly"
(set on the Mac) the tab closes itself, and `closeHuskForTty` is the net when it does not.

**Terminal's AppleScript sees a macOS-native tab as a WINDOW with one tab.** With
`AppleWindowTabbingMode always`, nine tabs in one window answer as nine one-tab windows, so `count of
tabs` is always 1 and `close w` closes just that tab. Count tabs with System Events instead (`radio
buttons of tab group 1` of the window), or a merged window reads as a pile of separate ones.

Sessions open as WINDOWS, not tabs, until the orchestrator's `/usr/local/bin/node` holds the
Accessibility grant: a tab needs a Cmd-T keystroke, and the plain `do script` fallback opens a window
whatever `AppleWindowTabbingMode` says. The out log's `tab failed` lines are this.

## Typing into a running session: short text only, and "sent" means the transcript has it

Both machines put a follow-up into a live session by typing it — the Mac into a Terminal tab's tty
(`sendToTty`), the Pi into a tmux pane (`typeInto` in `pi-desk/sessions.js`). What they type and how
they confirm it is one contract, in `orchestrator/src/handoff.js`:

- **A message of any length is pasted as it is.** Past about 800 characters or 4 lines Claude Code wraps
  input as `<pasted_content>`, and a session still answers it as his request — every long dictation he
  pastes at the keyboard arrives that way. `transcripts.js` strips the tags so every surface shows only
  his words. A running session's tty is raw, so nothing is cut (20,000 characters arrived intact on the
  Mac), and tmux has no limit. Messages sent before this were stashed in `~/.claude-desk-prompts/` behind
  a read-this stub; `unstash`, `readsStash` and `dropEcho` remain only to render those turns, and go
  inert as the files are swept at thirty days.
- **Whatever is typed goes inside bracketed-paste markers, and Enter follows as a keypress.** Without a
  boundary Claude Code can read the return as part of the text and leave the message in the composer.
  The Mac wraps the text in `ESC[200~ … ESC[201~` itself; the Pi uses `tmux paste-buffer -p -r`. The Pi
  must not go back to `send-keys <text>`: it gives no boundary, and a message that is a key name
  ("Enter", "Up") is sent as that key.
- **"Sent" is reported only once the transcript has it** (`awaitSubmitted`). Claude Code writes a
  `user` record at once when idle, and a `queue-operation` enqueue carrying the text at once when a turn
  is running — a busy session queues a typed message just as it does one from Remote Control, so there is
  nothing to wait for. Unconfirmed after 8s, the Pi presses Enter once more (the Mac does not); an
  unconfirmed command fails and is dead-lettered with his words. Typing reports nothing on its own —
  text can sit unsent in the composer, or vanish, while the call that typed it succeeds.
- **A session waiting on a permission prompt is refused.** The keys would go to the dialog and the Enter
  would answer it. The state comes from the session's record (`waiting` in `discover.js`).
- **Pictures** in a command's `payload.attachments` are fetched from the hub on the machine that owns
  the session and named in the prompt (`withAttachments`), for `follow_up`, `resume_session` and
  `open_session` alike, on both machines.

Launching is different: the prompt goes in a file the SHELL reads (`"$(cat …)"` on the Mac, `launch.sh`
on the Pi) and reaches claude as an argument, which is neither typed nor treated as a paste.

Three approaches for long text that look right and are not: chunking (`do script` appends a return, so
every chunk submits as its own message), System Events `keystroke` (types without a return, but needs
Accessibility permission a launchd daemon does not have), and `claude --resume <id> "$(cat …)"` in a
fresh tab (resume on a LIVE session starts a second process replaying the same transcript, so the
conversation forks into two copies with one id).

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

The Pi resumes one of its own sessions **under the name it had**: same tmux name, same Remote Control
name, same row, flipped back to `resident`. The Remote Control name is the session's identity in the
Claude app, and a fresh one made a reaped-and-resumed conversation show up there as a different
session. The Mac still opens a resume as a new row with a new name.

The Mac's `deskSessions.js` tick is still smaller than the Pi's: no memory admission. What it does do
each tick: bind each running row to its process (`pid`) and that process's current conversation,
close a row whose process has exited, adopt any process no row owns, publish the last completed turn
and the last turn boundary (`active_at`), mirror the tail, and run the idle reaper.

**The idle reaper (`idle.js`) parks a session the Mac only HOSTED** — dispatched (from the watch, the
phone, the responder) and never typed into at the keyboard — once it has been idle 30 minutes
(`IDLE_PARK_MS`), with its mirror complete and no Bash tool still running under it. A session he typed
into at the Mac, or opened there himself (`origin = adopted`), is never parked; he closes it. The
transcript records a keyboard prompt and a pasted-in one identically, so the orchestrator
fingerprints every prompt it sends (`remote_inputs`, written BEFORE sending) and a human prompt
matching none of them stamps `touched_at`, which is never cleared and survives a resume. Remote
Control input from the Claude app is indistinguishable from the keyboard and counts as it, which
errs toward keeping a session. Ticks never overlap: parking takes seconds per session, and an
overlapping tick acting on rows read before the first wrote anything parked a session the first had
just found typed into.

## Docs

`docs/ROUTING.md` is the routing contract. `ARCHITECTURE.md` covers the always-on machinery —
wake-free, speaker ID, feedback channels, settings. Each subdirectory has its own README.
