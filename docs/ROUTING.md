# How a voice turn is routed

```
mic → transcript → voice_requests (Supabase)
                        ↓
              worker claims it (Fly primary, Pi then Mac fallback)
                        ↓
        Opus at its default effort, full toolset (the warm SDK client)
                        ↓
           __IGNORE__ (room mic, not for us)  or  the reply
```

**One path, one model**, apart from the fast route below, which a request has to ask for. Every turn (photos, and the watch's fallback when the hub is down, included) runs on the warm `SdkRunner`: `HUB_MODEL`
(`opus`) at `HUB_EFFORT` (unset = the model's default, which is adaptive: it thinks as hard as the turn
needs), with `ALL_TOOLS` plus `Read`. There is no fast first responder and no escalation. Decision:
quality over speed. A low-effort first responder judges worse exactly where it matters (macro
estimates, and what to make of web results). A latency optimisation is the owner's decision; see
"Splitting it again" below before building one.
Nothing on the worker has shell or file-editing tools: work that needs them is a `start_session`.

The bottom activity bar shows the prompt, tool calls, and what the model says between them. Every
runner reports in-between text the same way: held until the next tool call, dropped if none follows,
since text after the last tool call is the reply.

## Effort

Measured on four meals (a Chipotle burrito, cereal with milk and a banana, two thirds of a
restaurant pad thai, tofu with oil and cornstarch), asking Opus for calories and macros at each level:

| effort | seconds per answer | macros |
|---|---|---|
| low | 7–10 | the pad thai came out 730 cal against ~800 at the higher levels |
| default | 7–11 | — |
| max | 17–29 | the same as default (burrito identical; within 10 cal on cereal and pad thai; tofu 330 against 370, both plausible) |

So `max` costs two to three times the time for the same numbers, and low is measurably worse. Opus over
Sonnet for judgment on misheard words: Sonnet saves "pair putt" as a to-do and then reports no haircut
item while naming "pair putt" at the same hour, where Opus links the two.

## What every turn is told

Three blocks ride on every hub prompt beside the replayed conversation and the time stamp:

- `NUTRITION_DIRECTIVE` — decompose an unspecified dish into ingredients with realistic portions, use
  the per-ingredient breakdown, and **web-search a specific restaurant item or branded product whose
  published nutrition it can't quote with certainty**, preferring the brand's own page; never search
  generic foods, saved recipes or repeats of logged meals. Measured with Opus at default effort, with
  and without web: famous chain items come out at the published figures from memory, and searching only
  adds 5–25s; less-common items are off 20–40% from memory and searching fixes most of them. A search
  can land on a stale third-party listing, which is why the directive points at the brand's own page.
- `TOOLS_DIRECTIVE` — picks between tools that look interchangeable: `log_recipe` only for a recipe the
  user names, never for "same as yesterday" (that copies the logged entry); read the workout catalog and pass
  the exercise id, with `upsert_catalog_exercise` only for a genuinely new movement; look things up with
  WebSearch/WebFetch in the turn, and `start_session` only for what needs the signed-in browser — which
  includes a source he NAMED (Reddit, a particular site) that blocks the turn's fetch: a session reads it
  in the Pi's browser. A blocked page he did not ask for is skipped in favour of readable sources.
- `DELIBERATE_DIRECTIVE` on a pushed or typed command, or `ADDRESSING_DIRECTIVE`
  on a room-mic turn (below).

`logSets()` backs the catalog rule mechanically: it throws on an unknown exercise unless a `muscles` map
comes with the call. `log_recipe` has no field through which a macro could be invented — it takes a
saved recipe's name and a serving count.

## Sends need a press

A text or an email reaches a real person, so it may be sent only on a command that was pressed for or typed
(`is_manual`, next section). Anything a microphone heard by itself, a wake-word capture included, gets
no send on any path: the warm client's fence (`SdkRunner.gate`, run as a PreToolUse hook) refuses
`SEND_TOOLS` and switching JaredBot on (`JAREDBOT_TOOL` with `on: true`, an auto-replier that texts on the
owner's behalf) unless `SdkRunner.run(..., manual=True)`; `manual` defaults to False, so a caller that forgets it
fails closed. Switching JaredBot off stays open to any mic. Codex has no fence and keeps every MCP tool,
which is safe only because it refuses any command that is not manual (below), and the credits runner is
reached only by manual commands.

The fence, not `allowed_tools`, is what holds the list. The warm client is Claude Code, which also
applies the host user's settings, and the Mac's pre-approve every data, gmail and messages tool in auto
mode — with `allowed_tools` alone a room-mic turn can send email. A hook's deny outranks any allow rule.
The fence also allows `Read` only inside the current turn's photo directory.

## Sessions need a press

Anything that reaches the worker without a manual press cannot start a session. A command may reach a
Claude session (`start_session`, `follow_up`, `resume_session`, `transfer_session`, or the pty inject
into a focused Mac session), Codex (`run_codex`, every MCP tool pre-approved) or the credits runner only when `is_manual(req)`: its `source` is in `MANUAL_SOURCES` AND it is not stamped
`always_on`. The labels there are the ones only a press or a keyboard writes: `watch-cmd` (the watch's
Hub control), `watch-fallback` (the same press through Supabase when the hub is down), `web` and
`web-quick` (the hub's mic buttons and its typed box) and `share` (the iPhone share sheet).

It **fails closed**. A wake-word capture (`fly`/`mac` with `always_on` false) is not manual, because the
TV can say the wake word; nor is any listener (`watch`, `phone`, `mac`, `fly` wake-free), any label a
client invents, or an unstamped row. The hub lets an uploading client name its own `source`, so a new
device is not manual until its label is added here, after checking that every writer of it is a button.

Enforced at each point, not once: `process_request` answers a non-manual command on the fenced warm client
even with a session focused; the fence refuses `SESSION_START_TOOLS` and the sends unless
`SdkRunner.run(..., manual=True)`; `run_codex` refuses outright unless called with `manual=True`. A non-manual turn also carries `NO_PRESS_DIRECTIVE`, so the model says it needs the
button rather than trying, for a session or a send alike.

## Session-only tools are removed from the schema

`SESSION_TOOLS` are taken out of every runner's schema (`disallowed_tools` on the warm client and the
credits client), so the request becomes a `start_session` instead:

- `build_walmart_cart_url`, `record_walmart_cart` and `save_shopping_item` only mean something next to
  the signed-in browser, which no worker runner has. The reads (`get_shopping_items`,
  `get_walmart_cart`) stay.
- `publish_report` writes up work a session has finished; a voice turn has none.

The voice assistant answers and dispatches. Anything that operates a machine (restarting, reloading,
fixing) is a session's job, never a tool here; the Pi already heals the wall itself (`kiosk-health.py`).

## A command can arrive with two transcripts

Both transcribers that feed the queue — the hub for the watch, phone and web (status-hub
`/api/voice-command`) and `server/voice_server/transcribe.py` for the ESP (`transcribe_alt`) — run both Groq Whisper
models on every command in parallel and store the second reading as `voice_requests.alt_transcript` when
its words differ. The Mac's own listeners (`wakefree.py`, `mac-listener`) do not; they are disabled. The worker appends it to the
prompt (`ALT_TRANSCRIPT_DIRECTIVE`), telling the model to act on whichever reading makes sense word by
word and to ask when neither does. Neither model is the better transcriber: large-v3 hears "haircut" as
"pair putt" where turbo gets it, and turbo hears "a to-do" as "to view" where large gets it. The thread,
the activity bar and dedupe all use `transcript` alone.

## Billing: the subscription, except where a turn is paid for on purpose

A failed run is retried on the subscription and then surfaced as an error. An API-billed fallback for
failures is attractive and wrong: at conversation volume it drains the Console balance, and it turns a
passing "overloaded" into a Console-billed Opus run. The Console key (`CREDITS_API_KEY`, which the Max
plan's monthly API credits pay for) is used in exactly two cases: a pressed turn the subscription
refused for its usage limit (`SDK_CREDITS`, see CLAUDE.md), and the fast route.

## The fast route

A request with `voice_requests.route = 'fast'` runs on `SDK_FAST`: the same warm client, model, tools,
fence and prompt as every other turn, with Claude Code in fast mode (`--settings {"fastMode": true}`)
and billed to the Console key. Fast mode is the same model writing up to 2.5x faster at twice the
per-token price; it does not shorten the wait for the first output. On a subscription login it would
be paid from usage credits instead, so it only ever runs on the key, which the plan's monthly API
credits cover.

- **Only the watch's Hub Fast complication asks for it** (status-hub `statushub://record?route=fast`,
  sent to `/api/voice-command` as `route=fast`). Every other route is unchanged.
- **Only a pressed command runs on it.** Anything a microphone heard by itself goes to the standard
  route, logged as an error, as does a request on a worker with no `ANTHROPIC_API_KEY`.
- **A fast run that fails before calling a tool is re-run on the standard route**; one that called a
  tool is surfaced, never repeated. The credits and Codex fallbacks do not apply to a fast turn.
- **The fast client is connected on its first turn** (a cold connect, several seconds) and dropped when
  the next standard turn answers, so a machine that never sees a fast request never holds a third
  Claude process. Its turns carry `"route": "fast"` in `voice_requests.timing`.

## Splitting it again

A cheap first responder that hands hard turns to a stronger model is the obvious speed-up. What it
costs, measured when it ran: the handed-off turn is re-run from scratch, so it pays a full first-responder
turn, then a CLI cold start, then the full strong turn. If it is rebuilt:

- **Gate by omission from an allow list, never by `--disallowedTools`.** A tool left off `allowed_tools`
  stays in the schema and is refused on call, so the first responder sees it needs it and hands off. A
  disallowed tool is removed from the schema: the first responder cannot see it and truthfully answers
  "that isn't available", and hand-offs silently stop.
- **The speed comes from the warm runner, not the model.** `claude -p` spawning the CLI and four stdio
  MCP servers costs 3.6s on the Pi before the model sees a token; the warm client pays it once. At low
  effort on a one-line todo answer Sonnet took about 2.0s of API time and Opus about 3.4s; on tool-heavy
  turns both ran 4–8s.
- **Judge the current instruction, not the replayed conversation.** A first responder reading the day's
  context escalated list adds because the previous minute had been about food.

## Every turn starts with `/clear`

The warm process holds ONE conversation for its whole life unless it is cleared, and the SDK has no
"new conversation" call. `_turn` sends `/clear` (about 0.2s) before every turn after the first, so each
turn sees only the day's conversation the worker replays into its prompt (`recent_context`, everything
since 4am or the last manual reset). Without it, each turn would ALSO carry every earlier turn and that
turn's copy of the replay, which grows quadratically and ends in an auto-compaction that blocks one
command for about 90s, and every command queued behind it.

## A refused turn wedges the warm client

The `SdkRunner` holds ONE long-lived headless Claude Code process. When the model refuses a turn, the
CLI answers `API Error: Sonnet 5 can't help with this. Start a new session to continue.` and then
returns that same error for **every later turn regardless of content**, until the process is
reconnected. Passing a fresh `session_id` to `query()` does not clear it: that argument is a label
and starts no new conversation.

The mic is always on, so the input that trips a refusal is usually room audio nobody addressed to the
assistant — and the commands that die behind it are unrelated.

So `SdkRunner.run()` recycles the client after **any** failed turn, not just a timeout or transport
error — a refusal arrives through the ordinary `ResultMessage` path with `is_error` set, so an
`except` block never sees it. It then re-runs the turn once on the cold client, but only when the
failed attempt called no tool (`used_tools`): a refusal fails before any tool call, so that re-run has
no side effects, while a turn that already acted is never replayed. A genuinely refusable turn just
fails twice, quickly, and surfaces as an error.

## The addressing directive

Room-mic turns get `ADDRESSING_DIRECTIVE` appended, because those mics are always on and hear the
whole room. A press (the phone's button, the watch's), typed text, and a command the ESP heard after
its wake word never do: it was meant.

Which is which is `voice_requests.always_on`, stamped by whoever captured the clip — the ESP server
(only for a wake-free capture, started on speech energy; a wake-word capture is a press), the Mac's
listener and wake-free capture, and the hub for an uploaded clip from a listener label
(`GATED_SOURCES` in status-hub). The worker never decides it from `source`: one device can be both a
listener and a button, and a second list of labels here drifts from the capturer's (a listener ends up
counted as a press). An unstamped row is a press for this, so a new capture path that forgets the flag gets
answered, never silently dropped. What a command may DO is the stricter `is_manual` above, which reads
`source` too and fails the other way: a wake-word capture is deliberate here but not manual there.

Only a room-mic turn can be ignored: the `__IGNORE__` sentinel is taught in `ADDRESSING_DIRECTIVE`
alone. That alone does not keep it from a deliberate turn, because every turn replays the day's thread
and a model copies a reply it sees there — so a bare sentinel reply is never replayed (`is_ignore` in
`_line`'s filter), and a deliberate turn that answers `__IGNORE__` anyway is re-run clean, with no
context. Deliberate turns get `DELIBERATE_DIRECTIVE` instead: work out the implied question when the
user describes something without asking one, and if the user truly cannot be understood say so in the model's own
words. There is no canned "didn't understand" line; offered one, the model used it on clear speech.

`DELIBERATE_DIRECTIVE` also makes the model the user's **orchestrator**: described work that needs a
real machine (code, debugging, deploys, a project, the signed-in browser) becomes a `start_session`
whether or not the word "session" is said, and a reply that plainly answers a session's message in the
thread goes back to that session through `follow_up`. When it is unclear which of the two is meant, the
model starts a new session rather than asking: the user gives a command and looks away, so a
clarifying question usually goes unseen. For that, `_line` names a session's reply in the
replayed thread as `Session "<label>" (<id>)` (from `voice_messages.session_id` / `session_label`)
rather than `Assistant`, so the model can tell which session said what and pass its id. None of this
applies on a turn that is not manual (a wake-word capture is deliberate but not manual): the session
tools and the sends are refused there, and `NO_PRESS_DIRECTIVE` tells the model to say it needs the button.

A request to put on, play or watch something that names no device goes to the **bedroom TV**; the Mac
gets it only when that same request names the Mac. The directive says so outright because the model,
left to infer it, carries the device over from earlier in the replayed day — after a "sorry, I meant on
my Mac", the next unqualified "put it on YouTube" goes to a Mac session.

Every turn also gets `READ_DIRECTIVE`: a reply to a QUESTION ends with the `[[read]]` marker, and a
reply to a request to DO something does not. The directive is worded around what was asked for, not
what the reply contains, because a confirmation routinely carries information — the video picked
for the TV and what it is about, the past entry a staged meal was copied from — and judged by its
content the model flags those, and the room-noise "that wasn't meant for me" replies with them. A
request that FAILED, or only partly worked, is flagged regardless: the user gives a command and looks
away assuming it worked, so a failure is exactly the reply that must not be missed.

A failure caused by a BROKEN tool (it errors or misbehaves) also starts a session to fix it
(`DELIBERATE_DIRECTIVE`), with the user's request, the tool and its exact error as the prompt; a failure that
is the state of the world (the TV is off) starts nothing, since there is no code to change. A request no tool
covers is not a failure: it is an ordinary handoff, and the session gets the request alone. Do not extend
this rule to MISSING tools. Every request that belongs in a session has no tool by definition, so the rule
turns each handoff into "answer it and build the orchestrator a tool for it", which is the orchestrator
growing into a session. `take_read_marker` strips it
before the reply is stored or spoken and sets `voice_messages.needs_read`, which files the answer in
the inbox (status-hub `docs/WATCH.md`, "The inbox").

**The inbox label is written by a separate Sonnet call, not by the answering model** (`labels.py`):
once the reply row is written, a background `claude -p --model sonnet` with no tools labels what he
said — his words and his two hub messages before them, never the answer — in a few words, ending in
`?` only when it was a question, and patches it onto the row as `label`. Until then, or if that fails
(reported as an error), the inbox shows the question's own words. Given the answer, the label drifts to
what the answer covered rather than what was asked; the earlier messages are what resolve "it" and
"the one that just came out". The answering model is not asked for the label because, asked to write
it inside its marker, it drifts into leaving it off, and a bare marker in the replayed day is copied by
every later turn. A label written inside a marker anyway is dropped. The replayed day puts `[[read]]`
back on every flagged hub reply (`_line`).

**One question is one inbox row.** When a reply answers the same question as the last flagged one —
he narrowed, corrected or clarified it ("I mean the new one") — the model writes
`[[read again]]` instead, and the worker clears `needs_read` on that earlier answer once the
new turn is written (`last_flagged` / `unflag` in process.py), so the clarified answer takes its row.
The earlier reply stays in the thread. A command spoken from an answer open in the inbox (`about_id`)
is flagged like any other: its words and reply go in line in the thread, not under the answer.

A session started with `start_session`'s `needs_read` has its relayed first reply filed in the inbox
too (`relay.js`, from the desk_commands payload), labelled the same way from the dispatched prompt
(`orchestrator/src/label.js`; the job label stands in until it lands). The orchestrator decides it at
dispatch, because most dispatched jobs need no answer read. A command spoken with an inbox answer open carries `about_id`, and
`about_directive` puts that question and answer on its prompt, since the answer may be days out of the
replayed context.
The replayed day puts the marker back on flagged replies (`_line`), for the same reason the ignore
sentinel is kept out of it: the model copies what the history shows, and a history of untagged
answers would teach it to stop tagging.

It carries only what the model cannot work out from the text itself:

1. the audio is room-wide, so most of what arrives was not said to the assistant;
2. an ignore must be the bare `__IGNORE__` sentinel and nothing else, since the exact sentinel is what drops the turn;
3. a machine named in a dispatch ("the Mac agent", "the Pi") is a **destination**, not a third party.
4. development talk naming no destination is the user speaking to a session already open elsewhere —
   which has heard it already, through its own channel.

Point 4 is STRUCTURAL — it is not about what the words mean. Any sentence can be a dispatch or not,
depending only on whether the user handed it over. Listing example sentences that are "not for you" is the
wrong shape of instruction: it teaches that certain CONTENT is never a dispatch, when the same words
forwarded deliberately are exactly that. A room-mic turn cannot dispatch at all now (it is not manual),
so points 3 and 4 decide only whether the model answers or ignores; point 3 is what makes it answer
dictated work with "that needs the button" instead of silence.

**Keep it that short.** Anything that helps the model spot noise, or argues about what a mistake
would cost, misfires on dictated development work — which rambles, names a machine, and is the most
expensive thing to lose. State facts it cannot observe; leave it the judgements it can make itself.
