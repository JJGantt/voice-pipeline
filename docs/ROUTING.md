# How a voice turn is routed

What happens between Jared speaking and something being done, and the constraints that are easy to
break by "simplifying" them. Written after a change to the tool list silently disabled nutrition
logging — the mechanism below was described in a code comment, was read, and was broken anyway,
because the load-bearing half of it reads like an aside.

---

## The path

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

## The constraint that matters most

**`SAFE_TOOLS` must stay an ALLOW list. Never convert it to `--disallowedTools`.**

The two are not equivalent, and the difference is the entire escalation mechanism:

| | tool in schema? | on call | what Sonnet does |
|---|---|---|---|
| **omitted from `--allowedTools`** | **yes, visible** | denied | sees it, knows it needs it, emits `__ESCALATE__` |
| listed in `--disallowedTools` | **no, removed** | n/a | cannot see it — answers "that tool isn't available" |

Escalation depends on Sonnet being able to *see* a tool it may not *call*. Hiding the tool doesn't
make it safer; it removes the model's ability to recognise that a more capable model should take the
turn. When this was inverted, "log a bowl of cereal" produced *"the nutrition logging tool isn't
available to me right now, only the read side"* — a truthful answer to a question that should never
have been asked.

So a gated tool is gated by **omission**, never by denial.

## What is gated, and why

Gated (Opus only): data **writes** that need real domain knowledge — nutrition values, muscle maps,
maintenance math. `log_nutrition`, `stage_meal`, `commit_staged`, `log_weight`, `log_set`,
`upsert_catalog_exercise`, and their edit/delete siblings.

Not gated: everything else. Reads, actuators (lights, feeder, TV, voice settings), the low-stakes
writes that need no domain knowledge (lists, notes, dated items), and dispatching work to a machine
(`start_session` and friends — it starts a session, it does not itself change data).

## The cost of an allow list, and how it is contained

A hand-maintained list of exact tool-name strings rots. It has, twice:

- it named `create_reminder` / `complete_reminder` / `complete_list_item` for months after those
  tools were removed (reminders became dated list items in data-layer migration `00056`);
- it silently gated out every tool added since it was written, including the session tools — which is
  why dispatching work by voice failed no matter how the request was phrased.

`validate_safe_tools()` runs at startup and logs both directions: names in the list that no longer
exist, and data tools absent from the list. It cannot repair anything — it makes the rot audible
instead of silent. **When adding a tool to data-layer, check that log line.**

## The escalation is visible on the wall

An escalation used to be invisible: the bottom activity bar showed the prompt, then a pause, then
Opus's first tool call — indistinguishable from a slow turn. So the hop pushes its own activity frame
(`push_activity(..., "escalate")` → the hub's `/api/voice-activity`), and the bar shows it as its own
row *and* lights an `OPUS` chip that stays up for the rest of the command, since the later tool rows
overwrite the text. Both escalation paths send it: the `__ESCALATE__` sentinel and the fast-path
error fallback, plus the `__ESCALATE__`-leak re-run.

Photo turns go straight to Opus without ever touching Sonnet, so they are not an escalation and send
no such frame. The hub coerces any unknown activity state to `tool`, so a new state has to be added
there (`server.js`) as well as in the bar (`voiceagent.js`, `tv.js`) — see the hub's HUB-INTERNALS §4.

## The addressing directive

Transcribed turns (never typed ones) get `ADDRESSING_DIRECTIVE` appended. The mic is always on and
hears the whole room, so most of what arrives was not said to the assistant.

It is deliberately short. It carries only what the model cannot work out from the text itself:

1. the audio is room-wide, so most of it isn't addressed here;
2. the reply is read aloud, so an ignore must be the bare `__IGNORE__` sentinel and nothing else;
3. a machine named in a dispatch ("the Mac agent", "the Pi") is a **destination**, not a third party.

It used to be much longer, with a list of tell-tales for ignoring — "rambling", "thinking aloud",
"addresses a third party". Those were removed because they misfired on the case they were never meant
to catch: **dictating development work is rambling by nature**, full of restarts and asides, and it
names a machine. The heuristics fired on exactly the requests that were most expensive to lose.

It also briefly told the model to "use its own judgement", and carried invented cost arguments ("a
missed command only costs a repeat", "dispatching costs nothing if wrong"). Both were removed: the
first is the default and adding it only spends words, and the second was false in both directions.

**The rule this leaves:** state facts the model cannot observe. Do not hand it conclusions it can
reach itself, and never invent a cost model to justify one.
