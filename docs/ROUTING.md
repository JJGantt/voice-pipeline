# How a voice turn is routed

```
mic → transcript → voice_requests (Supabase)
                        ↓
              worker claims it (Mac primary, Pi fallback, Fly last)
                        ↓
          ┌─────────────┴──────────────┐
          │  Sonnet, the fast responder │   ← SAFE_TOOLS in --allowedTools
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

The bottom activity bar shows the prompt, tool calls, and escalations (an `OPUS` chip stays lit for
the rest of an escalated command). Photo turns skip Sonnet entirely and are not escalations.

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
| `publish_report` | — (gated by convention; it does not run on a voice turn often enough to matter) |

Everything else is on the fast path, including writes that are destructive: reads, actuators, lists,
notes, dated items, the whole session group (`start_session`, `follow_up`, `get_sessions`,
`resume_session`, `end_session`, `transfer_session`), `log_weight`/`set_height`,
`log_set`/`update_set`/`remove_set`, the rep watcher, and
`commit_staged`/`remove_staged`/`delete_nutrition_entry`. Every argument those take is
either a number the user spoke or an id from a read — there is nothing in them a bigger model knows
better. **Destructiveness is not what the gate is for**; a wrong delete is a tool-description problem,
and Sonnet already deletes notes and whole lists.

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

`recent_context()` prepends the recent hub conversation to the prompt, so the escalation decision is
made over a blob that often contains the last few nutrition turns. Sonnet used to read that and
escalate a to-do add because the previous minute had been about food. The directive now says to decide
from the current instruction alone; if escalations for lights and list adds reappear, that clause is
what regressed.

## What the split is for — and which half costs money

**The fast path is the METERED one.** Sonnet runs on the warm `ApiRunner` (~1c/turn) on every machine;
the escalation runs Opus through `claude -p` on the **subscription**, where tokens are free. That is
the opposite way round from the intuition that escalating to the bigger model costs more — escalation
moves a turn OFF the meter.

So the reasons the split exists, in the order they hold:

1. **What the fast path can REACH.** The allow list is a correctness gate — a tool that would have to
   invent a number is simply not in the schema Sonnet is handed, so it cannot quietly write one.
   Nothing to do with models or money.
2. **Money, on the common case.** Every ordinary turn is billed, so the cheap model runs it. The
   expensive model only ever runs where tokens are free.
3. **Speed — from the RUNNER, not the model.** The warm runner is what avoids `claude -p` spawning
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

What escalation does cost is **time**: the whole turn is re-run, so a gated command pays a full API
Sonnet turn, then a CLI cold start, then a full Opus turn. It does not cost double money — the second
half is the free half. That is why anything not needing the gate belongs on the fast path, and why the
fix for latency, if one is ever wanted, is to continue an escalated turn rather than restart it.

## Keeping the list honest

An allow list of exact tool-name strings has no link to the tools that exist, so it drifts in both
directions: it keeps naming tools that were deleted, and silently gates out tools that were added.

`validate_safe_tools()` logs both at startup — listed names that no longer exist, and data tools
absent from the list. It does not repair anything. **After adding a tool to data-layer, read that log
line**; a new tool is invisible to the fast path until it appears here.

## The addressing directive

Transcribed turns (never typed ones) get `ADDRESSING_DIRECTIVE` appended, because the mic is always on
and hears the whole room.

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

**Keep it that short.** Anything that helps the model spot noise, or argues about what a mistake
would cost, misfires on dictated development work — which rambles, names a machine, and is the most
expensive thing to lose. State facts it cannot observe; leave it the judgements it can make itself.
