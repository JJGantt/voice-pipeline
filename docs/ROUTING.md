# How a voice turn is routed

```
mic → transcript → voice_requests (Supabase)
                        ↓
              worker claims it (Pi primary, Mac fallback, Fly last)
                        ↓
          ┌─────────────┴──────────────┐
          │  Sonnet, the fast responder │   ← SAFE_TOOLS (allowed_tools on the warm SDK client)
          └─────────────┬──────────────┘
                        │
        ┌───────────────┼────────────────┐
        │               │                │
   __IGNORE__      answers it       __ESCALATE__
   (not for us)    itself           (needs a gated tool)
                                          ↓
                                 re-run the WHOLE turn
                                 on Opus + ALL_TOOLS
```

The bottom activity bar shows the prompt, tool calls, what the model says between them, and
escalations (an `OPUS` chip stays lit for the rest of an escalated command). All three runners report
in-between text the same way: held until the next tool call, dropped if none follows, since text after
the last tool call is the reply. Photo turns skip Sonnet entirely and are not escalations.

## `SAFE_TOOLS` must be an allow list — never `--disallowedTools`

The two are not equivalent, and the difference is the whole escalation mechanism:

| | in schema? | on call | Sonnet's behaviour |
|---|---|---|---|
| **omitted from `--allowedTools`** | **visible** | denied | sees it, knows it needs it, emits `__ESCALATE__` |
| listed in `--disallowedTools` | **removed** | n/a | cannot see it — replies "that tool isn't available" |

Escalation depends on Sonnet seeing a tool it may not call. A gated tool is gated by **omission**.
Converting this list to a deny list disables escalation silently — every gated write starts answering
"I can't do that" instead of handing the turn to Opus.

## What is gated

Not reads vs writes — **whether the model has to supply a value out of its own knowledge.**

| gated | what it would have to invent |
|---|---|
| `log_nutrition` `stage_meal` `edit_staged` `subtract_leftovers` | a food's macros, and the cooked weight of a dish |
| `set_nutrition_targets` | maintenance math, when the ask is "targets for a cut" rather than a stated number |
| `upsert_catalog_exercise` | a movement's categories and fractional muscle map |
| `save_recipe` | each ingredient's macros for the portion recorded — the same estimate as logging it |
| `publish_report` | — (gated by convention; it does not run on a voice turn often enough to matter) |

Everything else is on the fast path, including writes that are destructive: reads, actuators, lists
and their dated and note items, `create_list`, the whole session group (`start_session`, `follow_up`, `get_sessions`,
`resume_session`, `end_session`, `transfer_session`), `log_weight`/`set_height`,
`log_set`/`update_set`/`remove_set`, the rep watcher, and
`commit_staged`/`remove_staged`/`delete_nutrition_entry`, and the recipe reads plus `log_recipe`.
Every argument those take is either a number the user spoke or an id from a read — there is nothing in
them a bigger model knows better.

The web (`WebSearch`, `WebFetch`) and reading Gmail and iMessage are on the fast path too, by the same
rule. They are listed once, tool by tool, in `OPEN_TOOLS` and spread into both lists.

**Sending a text or an email (`SEND_TOOLS`) is decided per turn, by which mic heard it.** A pushed
command — typed, the phone's button, the watch's — may send; one from the always-on room mics (source
`fly`/`mac`) may not, because those hear the whole room and a sentence meant for someone else would
reach a real person. The sends are on no allow list: the warm client's `_gate` callback allows them
only when `SdkRunner.run(..., ambient=False)`, and every other runner gets `turn_tools(ambient)`
(full-agent mode, which has no allow list, gets them as `--disallowedTools`).

The fast path's list is enforced by a **PreToolUse hook** (`SdkRunner._fence`), not by `allowed_tools`
alone. The warm client is Claude Code, which also applies the host user's settings, and the Mac's
pre-approve every data, gmail and messages tool in auto mode — so `allowed_tools` let Sonnet on the Mac
call gated nutrition tools directly and let a room-mic turn send email. A hook's deny outranks any
allow rule. The `claude -p` runs get `--setting-sources project` for the same reason; the worker runs
from its own directory, which has none.

Room-mic turns also carry `ROOM_SEND_DIRECTIVE`, which tells the model not to reach a send another
way — by escalating, or by starting a session whose own tools could send it. That last route is
instruction-only: `start_session` stays on the room mics because dictating work to a session is
what they are mostly used for.

## Browser-only tools are removed, not gated

`build_walmart_cart_url`, `record_walmart_cart` and `save_shopping_item` (`SESSION_TOOLS`) only mean
something next to the signed-in browser, which no worker runner has. They are taken out of every
runner's schema (`disallowed_tools` on the warm client, `--disallowedTools` on `claude -p`) — the one place `--disallowedTools` is right. Left merely gated, they would be
visible-but-denied, which is exactly what makes Sonnet escalate, and a cart request would reach an
Opus turn that can only call them with made-up values. Out of sight, a cart request is a
`start_session`, where a browser exists. The reads (`get_shopping_items`, `get_walmart_cart`) stay on
the fast path.

## Asking for Opus

Escalation also fires on request: "escalate to Opus", "use Opus" makes Sonnet reply `__ESCALATE__`
and the turn re-runs on Opus with his words unchanged. Lookups are answered in the turn itself; `start_session` is only for what needs his
signed-in browser (acting on a site, or reading something behind his logins). **Destructiveness is not what the gate is for**; a wrong delete is a tool-description problem,
and Sonnet already deletes list items and whole lists.

`create_list` is on the fast path for the same reason, despite making something new: it takes a name
Jared said and invents nothing. It is a separate tool from `add_to_list` so that ADDING cannot create
a list by accident (data-layer `00094`); gating it would only move that accident to a bigger model.

### A saved recipe is logged by a different tool so it can be ungated

`log_recipe` exists because of this table. Logging a recipe supplies nothing out of the model's
knowledge — a name the user said, a serving count the user said, and macros written down when the
recipe was saved — so by the rule it belongs to Sonnet. But it used to be a variant of
`log_nutrition`, and that tool is gated for good reason, so "log my overnight oats" bought a full
Opus escalation to copy numbers that already existed.

Splitting it made the gate mechanical rather than a matter of trust: `log_recipe` has no field on it
through which a macro could be invented. `save_recipe` stays gated, because writing the recipe in the
first place is exactly the estimating job `log_nutrition` does.

### Sets are gated per CALL, not per tool

`log_set` is on the fast path because nearly every set is a movement the catalog already has, where the
call is pass-through. Only a genuinely new movement needs a muscle map. The escalate directive tells
Sonnet to read the catalog first and escalate if the name is not in it.

`logSets()` backs that up: it throws on an unknown exercise unless a `muscles` map comes with the call,
so a Sonnet that skips the catalog read gets an error, not a corrupt row. What the server does NOT
enforce is "don't invent a map" — that guardrail is prompt-level, and it is the one thing here that is
weaker than a mechanical gate. `setWatcher()` is stricter still: it cannot create a catalog entry at
all, only error.

### Judge the instruction, not the conversation

`recent_context()` prepends the day's hub conversation (since 4 AM, timestamped) to the prompt, so the escalation decision is
made over a blob that often contains the last few nutrition turns. Sonnet used to read that and
escalate a to-do add because the previous minute had been about food. The directive now says to decide
from the current instruction alone; if escalations for lights and list adds reappear, that clause is
what regressed.

## What the split is for — and how it bills

**Everything runs on the subscription, and nothing else.** Sonnet runs on the warm `SdkRunner` (a
long-lived headless Claude Code process holding the MCP servers); escalation runs Opus through
`claude -p`. A failed run is retried on the subscription and then surfaced as an error — there is no
paid path. An API-billed path is attractive twice over and fails both times: as the fast path it drains
the Console balance at conversation volume, and as a fallback at the same tier it turns a passing
"overloaded" on an Opus turn into a Console-billed Opus run. Errors also never upgrade the model: an
error path that did turned every noise judgment into an Opus run (the dispatch comment in `worker.py`).

So the reasons the split exists, in the order they hold:

1. **What the fast path can REACH.** The allow list is a correctness gate — a tool that would have to
   invent a number is simply not in the schema Sonnet is handed, so it cannot quietly write one.
   Nothing to do with models or money.
2. **Capacity, on the common case.** Every ordinary turn draws from the shared subscription window,
   so the cheap fast model runs it and Opus only runs the rare gated turn.
3. **Speed — from the RUNNER, not the model.** The warm client is what avoids `claude -p` spawning
   the CLI and four stdio MCP servers per command: 3.6s on the Pi, ~42% of a simple turn, before the
   model sees a token.

The model choice contributes almost nothing to speed, which is worth knowing before anyone "optimises"
it. Measured on the Mac, same runner for both (`claude -p --output-format json`, three runs, API time
only):

| | short voice reply | ~490-token answer |
|---|---|---|
| Sonnet | 6.7 / 7.9 / 10.4s | 7.5s |
| Opus | 6.4 / 8.0 / 8.6s | 10.5s |

On a short reply — which is every voice turn — they are indistinguishable: the run-to-run spread is
wider than the gap, and Opus won one of the three runs. Sonnet only pulls ahead once an answer is long
(~63 tok/s vs ~47), and voice answers never are.

What escalation does cost is **time**: the whole turn is re-run, so a gated command pays a full
Sonnet turn, then a CLI cold start, then a full Opus turn. That is why anything not needing the gate
belongs on the fast path, and why the fix for latency, if one is ever wanted, is to continue an
escalated turn rather than restart it.

## A refused turn wedges the warm client

The `SdkRunner` holds ONE long-lived headless Claude Code process. When the model refuses a turn, the
CLI answers `API Error: Sonnet 5 can't help with this. Start a new session to continue.` and then
returns that same error for **every later turn regardless of content**, until the process is
reconnected. Passing a fresh `session_id` to `query()` does not clear it, even though each turn
already does exactly that.

The mic is always on, so the input that trips a refusal is usually room audio nobody addressed to the
assistant — and the commands that die behind it are unrelated. On 2026-09-02 one false-triggered
ambient capture wedged the client for 85 minutes and roughly fifteen commands failed.

So `SdkRunner.run()` recycles the client after **any** failed turn, not just a timeout or transport
error — a refusal arrives through the ordinary `ResultMessage` path with `is_error` set, so an
`except` block never sees it. It then re-runs the turn once on the cold client, but only when the
failed attempt called no tool (`used_tools`): a refusal fails before any tool call, so that re-run has
no side effects, while a turn that already acted is never replayed. A genuinely refusable turn just
fails twice, quickly, and surfaces as an error.

## Keeping the list honest

An allow list of exact tool-name strings has no link to the tools that exist, so it drifts in both
directions: it keeps naming tools that were deleted, and silently gates out tools that were added.

`validate_safe_tools()` logs both at startup — listed names that no longer exist, and data tools
absent from the list. It does not repair anything. **After adding a tool to data-layer, read that log
line**; a new tool is invisible to the fast path until it appears here.

## The addressing directive

Room-mic turns (sources `fly` and `mac`) get `ADDRESSING_DIRECTIVE` appended, because those mics are
always on and hear the whole room. The phone's push-to-talk button (`web-quick`) and typed text never
do: he pressed the button or typed it, so it was meant.

Only a room-mic turn can be ignored: the `__IGNORE__` sentinel is taught in `ADDRESSING_DIRECTIVE`
alone, so a deliberate turn never learns it exists. Deliberate turns get `DELIBERATE_DIRECTIVE`
instead: work out the implied question when he describes something without asking one, and if he
truly cannot be understood say so in the model's own words. There is no canned "didn't understand"
line; offered one, the model used it on clear speech.

It carries only what the model cannot work out from the text itself:

1. the audio is room-wide, so most of what arrives was not said to the assistant;
2. the reply is read aloud, so an ignore must be the bare `__IGNORE__` sentinel and nothing else;
3. a machine named in a dispatch ("the Mac agent", "the Pi") is a **destination**, not a third party.
4. development talk naming no destination is him speaking to a session already open elsewhere —
   which has heard him already, through its own channel.

Point 4 is the one that cost something, and the rule is STRUCTURAL — it is not about what the words
mean. Any sentence can be a dispatch or not, depending only on whether Jared handed it over, so
`follow_up` requires an explicit handoff phrase ("send a follow-up", "tell it to") and judges nothing
by subject matter. Listing example sentences that are "not for you" is the wrong shape of instruction
and was removed: it teaches that certain CONTENT is never a dispatch, when the same words forwarded
deliberately are exactly that.

5. focusing a session NAMES the destination — everything after "focus the X session", in that same
   turn, goes to that session as a follow-up, with no handoff phrase needed. Point 4 says the
   opposite for talk that names no destination, and without this clause it wins: the model reads the
   remainder as belonging to a session that already heard him, forwards nothing, and then reports
   having sent a follow-up it never sent.

**Keep it that short.** Anything that helps the model spot noise, or argues about what a mistake
would cost, misfires on dictated development work — which rambles, names a machine, and is the most
expensive thing to lose. State facts it cannot observe; leave it the judgements it can make itself.
