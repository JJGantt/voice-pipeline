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

**One path, one model.** Every hub turn — photos included — runs on the warm `SdkRunner`: `HUB_MODEL`
(`opus`) at `HUB_EFFORT` (unset = the model's default, which is adaptive: it thinks as hard as the turn
needs), with `ALL_TOOLS` plus `Read`. There is no fast first responder and no escalation. Jared chose
quality over speed on 2026-10-29: he does not need anything instantly, and a low-effort first responder
judges worse exactly where it matters — macro estimates, and what to make of web results. If latency
becomes a problem he decides the optimisation; see "Splitting it again" below before building one.
Full-agent mode is separate: the unrestricted coding agent on its own `--resume` session.

The bottom activity bar shows the prompt, tool calls, and what the model says between them. Every
runner reports in-between text the same way: held until the next tool call, dropped if none follows,
since text after the last tool call is the reply.

## Effort

Measured 2026-10-16 on four meals (a Chipotle burrito, cereal with milk and a banana, two thirds of a
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
  generic foods, saved recipes or repeats of logged meals. Measured 2026-10-29, Opus at default effort,
  two runs each with and without web: famous chains (Big Mac, Chick-fil-A sandwich, grande latte,
  Chipotle burrito, Crunchwrap) came out at the published figures from memory, and searching only added
  5–25s. Less-common items were off 20–40% from memory and searching fixed most of them: Jinya Spicy
  Creamy Vegan Ramen 960 → 790 (published ~790), La Colombe Vanilla Draft Latte 130–150 → 110 (label
  ~110). A search can land on a stale third-party listing (a Panera melt came back 990 against ~890),
  which is why the directive points at the brand's own page.
- `TOOLS_DIRECTIVE` — picks between tools that look interchangeable: `log_recipe` only for a recipe he
  names, never for "same as yesterday" (that copies the logged entry); read the workout catalog and pass
  the exercise id, with `upsert_catalog_exercise` only for a genuinely new movement; look things up with
  WebSearch/WebFetch in the turn, and `start_session` only for what needs his signed-in browser.
- `DELIBERATE_DIRECTIVE` on a pushed or typed command, or `ADDRESSING_DIRECTIVE` + `ROOM_SEND_DIRECTIVE`
  on a room-mic turn (below).

`logSets()` backs the catalog rule mechanically: it throws on an unknown exercise unless a `muscles` map
comes with the call. `log_recipe` has no field through which a macro could be invented — it takes a
saved recipe's name and a serving count.

## Sends are decided per turn, by which mic heard it

A pushed command — typed, the phone's button, the watch's — may send a text or an email (`SEND_TOOLS`);
one from an always-on mic (`voice_requests.always_on`) may not, because those hear the whole room and a
sentence meant for someone else would reach a real person. The warm client's `_fence` PreToolUse hook
allows the sends only when `SdkRunner.run(..., ambient=False)`. The unrestricted agent (`run_claude`)
has no fence and keeps them, which is safe only because it runs nothing but manual commands (below).

The fence, not `allowed_tools`, is what holds the list. The warm client is Claude Code, which also
applies the host user's settings, and the Mac's pre-approve every data, gmail and messages tool in auto
mode — with `allowed_tools` alone a room-mic turn sent email. A hook's deny outranks any allow rule.
The fence also allows `Read` only inside the current turn's photo directory.

Room-mic turns carry `ROOM_SEND_DIRECTIVE`, which tells the model a send has to come from a press. The
other way round a send, starting a session whose own tools could send it, is closed by the next rule.

## Sessions and the unrestricted agent need a press

Anything that reaches the worker without a manual press cannot start a session. A command may reach a
Claude session (`start_session`, `follow_up`, `resume_session`, `transfer_session`, or the pty inject
into a focused Mac session), the unrestricted agent (`run_claude`, `--dangerously-skip-permissions`:
full-agent mode and the watch's fallback path), Codex (`run_codex`, every MCP tool pre-approved) or the
credits runner only when `is_manual(req)`: its `source` is in `MANUAL_SOURCES` AND it is not stamped
`always_on`. The labels there are the ones only a press or a keyboard writes: `watch-cmd` (the watch's
Hub control), `watch-fallback` (the same press through Supabase when the hub is down), `web` and
`web-quick` (the hub's mic buttons and its typed box) and `share` (the iPhone share sheet).

It **fails closed**. A wake-word capture (`fly`/`mac` with `always_on` false) is not manual, because the
TV can say the wake word; nor is any listener (`watch`, `phone`, `mac`, `fly` wake-free), any label a
client invents, or an unstamped row. The hub lets an uploading client name its own `source`, so a new
device is not manual until its label is added here, after checking that every writer of it is a button.

Enforced at each point, not once: `process_request` routes a non-manual command to the fenced warm client
even with agent mode on or a session focused; the fence refuses `SESSION_START_TOOLS` unless
`SdkRunner.run(..., manual=True)`; `run_claude` and `run_codex` refuse outright unless called with
`manual=True`. A non-manual turn also carries `NO_SESSION_DIRECTIVE`, so the model says it needs the
button rather than trying. Sends stay on the `always_on` rule above, so a wake-word turn can still send.

## Browser-only tools are removed from the schema

`build_walmart_cart_url`, `record_walmart_cart` and `save_shopping_item` (`SESSION_TOOLS`) only mean
something next to the signed-in browser, which no worker runner has. They are taken out of every
runner's schema (`disallowed_tools` on the warm client, `--disallowedTools` on `run_claude`), so a cart
request becomes a `start_session`, where a browser exists. The reads (`get_shopping_items`,
`get_walmart_cart`) stay.

## A command can arrive with two transcripts

Both transcribers that feed the queue — the hub for the watch, phone and web (status-hub
`/api/voice-command`) and `server/server.py` for the ESP (`transcribe_alt`) — run both Groq Whisper
models on every command in parallel and store the second reading as `voice_requests.alt_transcript` when
its words differ. The Mac's own listeners (`wakefree.py`, `mac-listener`) do not; they are disabled. The worker appends it to the
prompt (`ALT_TRANSCRIPT_DIRECTIVE`), telling the model to act on whichever reading makes sense word by
word and to ask when neither does. Neither model is the better transcriber: large-v3 hears "haircut" as
"pair putt" where turbo gets it, and turbo hears "a to-do" as "to view" where large gets it. The thread,
the activity bar and dedupe all use `transcript` alone.

## Billing: the subscription, and nothing else

A failed run is retried on the subscription and then surfaced as an error — there is no paid path. An
API-billed fallback is attractive and wrong: at conversation volume it drains the Console balance, and
it turns a passing "overloaded" into a Console-billed Opus run.

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
assistant — and the commands that die behind it are unrelated. On 2026-09-02 one false-triggered
ambient capture wedged the client for 85 minutes and roughly fifteen commands failed.

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
listener and a button, and a second list of labels here is how the phone's listener ended up counted
as a press. An unstamped row is a press for this, so a new capture path that forgets the flag gets
answered, never silently dropped. What a command may DO is the stricter `is_manual` above, which reads
`source` too and fails the other way: a wake-word capture is deliberate here but not manual there.

Only a room-mic turn can be ignored: the `__IGNORE__` sentinel is taught in `ADDRESSING_DIRECTIVE`
alone. That alone does not keep it from a deliberate turn, because every turn replays the day's thread
and a model copies a reply it sees there — so a bare sentinel reply is never replayed (`is_ignore` in
`_line`'s filter), and a deliberate turn that answers `__IGNORE__` anyway is re-run clean, with no
context. Deliberate turns get `DELIBERATE_DIRECTIVE` instead: work out the implied question when he
describes something without asking one, and if he truly cannot be understood say so in the model's own
words. There is no canned "didn't understand" line; offered one, the model used it on clear speech.

`DELIBERATE_DIRECTIVE` also makes the model his **orchestrator**: work he describes that needs a real
machine (code, debugging, deploys, a project, his signed-in browser) becomes a `start_session` whether
or not he says "session", and a reply from him that plainly answers a session's message in the thread
goes back to that session through `follow_up`. When it is unclear which of the two he means, the
model starts a new session rather than asking: he gives a command and looks away, so a clarifying
question usually goes unseen. For that, `_line` names a session's reply in the
replayed thread as `Session "<label>" (<id>)` (from `voice_messages.session_id` / `session_label`)
rather than `Assistant`, so the model can tell which session said what and pass its id. None of this
applies on a turn that is not manual (a wake-word capture is deliberate but not manual): the session
tools are refused there, and `NO_SESSION_DIRECTIVE` tells the model to say it needs the button.

Every turn also gets `READ_DIRECTIVE`: a reply to a QUESTION ends with the `[[read]]` marker, and a
reply to a request to DO something does not. The directive is worded around what he asked for, not
what the reply contains, because a confirmation routinely carries information — the video picked
for the TV and what it is about, the past entry a staged meal was copied from — and judged by its
content the model flags those, and the room-noise "that wasn't meant for me" replies with them. A
request that FAILED, or only partly worked, is flagged regardless: he gives a command and looks away
assuming it worked, so a failure is exactly the reply he must not miss.

A failure caused by a BROKEN or MISSING tool also starts a session to fix it (`DELIBERATE_DIRECTIVE`),
with his request, the tool and its exact error as the prompt; a failure that is the state of the world
(the TV is off) starts nothing, since there is no code to change. `take_read_marker` strips it
before the reply is stored or spoken and sets `voice_messages.needs_read`, which the watch uses to
keep the answer reachable until it has been on screen (status-hub `docs/WATCH.md`, "Unread answers").
The replayed day puts the marker back on flagged replies (`_line`), for the same reason the ignore
sentinel is kept out of it: the model copies what the history shows, and a history of untagged
answers would teach it to stop tagging.

It carries only what the model cannot work out from the text itself:

1. the audio is room-wide, so most of what arrives was not said to the assistant;
2. an ignore must be the bare `__IGNORE__` sentinel and nothing else, since the exact sentinel is what drops the turn;
3. a machine named in a dispatch ("the Mac agent", "the Pi") is a **destination**, not a third party.
4. development talk naming no destination is him speaking to a session already open elsewhere —
   which has heard him already, through its own channel.

Point 4 is STRUCTURAL — it is not about what the words mean. Any sentence can be a dispatch or not,
depending only on whether Jared handed it over. Listing example sentences that are "not for you" is the
wrong shape of instruction: it teaches that certain CONTENT is never a dispatch, when the same words
forwarded deliberately are exactly that. A room-mic turn cannot dispatch at all now (it is not manual),
so points 3 and 4 decide only whether the model answers or ignores; point 3 is what makes it answer
dictated work with "that needs the button" instead of silence.

**Keep it that short.** Anything that helps the model spot noise, or argues about what a mistake
would cost, misfires on dictated development work — which rambles, names a machine, and is the most
expensive thing to lose. State facts it cannot observe; leave it the judgements it can make itself.
