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

Data **writes** needing domain knowledge — nutrition values, muscle maps, maintenance math:
`log_nutrition`, `stage_meal`, `commit_staged`, `subtract_leftovers`, `log_weight`, `set_height`,
`log_set`, `update_set`, `remove_set`, `upsert_catalog_exercise`, the rep-watcher pair, and the
nutrition edit/delete siblings.

Everything else is on the fast path: reads, actuators (lights, feeder, TV, voice settings), the
low-stakes writes needing no domain knowledge (lists, notes, dated items), and dispatching work to a
machine — `start_session` starts a session, it does not itself change data.

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

**Keep it that short.** Two things do not belong in it, and both actively hurt:

- **Heuristics for spotting noise** ("rambling", "thinking aloud", "addresses a third party").
  Dictating development work is rambling by nature and names a machine, so these fire hardest on the
  requests most expensive to lose.
- **Invented cost arguments** ("a missed command only costs a repeat"). A dropped 90-second dictated
  request is a real loss, and a wrong dispatch is real mess. Made-up asymmetries bias the decision
  with numbers nobody checked.

State facts the model cannot observe. Leave it the judgements it can make itself.
