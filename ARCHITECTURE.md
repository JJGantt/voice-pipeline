# Voice / Hub Agent — Architecture

The canonical definition of how the hub agent works. **There are exactly two routes.** Every way of
talking to the agent resolves to one of them. If a change would add a third behavior, it's wrong —
fix it back to two.

## The two routes

| | **Efficiency** | **Accuracy** |
|---|---|---|
| Goal | raw speed | correctness (slow is fine) |
| Model | Haiku | Opus + heavy thinking |
| Engine | **Messages API** (`ApiRunner`) — only the MCP tools, no Claude Code overhead | **subscription `claude -p`** (`run_claude`) |
| Devices | whichever is fastest (Mac / Pi / Fly — device-agnostic) | Mac → Pi → Fly. Fly has no subscription, so it uses the **API** with the same model + thinking |
| Billing | API credits (pennies/turn) | free (subscription) on Mac/Pi; API on Fly only |

That's it. Two routes, two execution strategies in the worker (`ApiRunner` and `run_claude`). Nothing else.

## How you reach each route

Every input is thin — it only captures the request and **enqueues one row** to Supabase
`voice_requests` `{transcript, source, tier}`. The worker reads `tier` and picks the route.

| Input | Route | tier |
|---|---|---|
| ✦ Sparkle button (web FAB) | Efficiency | `fast` |
| "hey_rhasspy" wake word (ESP + Mac mic) | Efficiency | `fast` |
| 🧠 Brain button (web FAB) | Accuracy | `power` |
| "hey_jarvis" wake word (ESP + Mac mic) | Accuracy | `power` |
| Typed chat box (web) | **per the chat-box setting** | resolved from `app_settings.chatbox_route` (default `power`) |

The buttons and wake words mirror each other. The **only** input without a fixed tier is the typed
chat box, and it follows the one remaining setting (`chatbox_route` = `fast` | `power`). That setting
governs *only* the chat box now — nothing else.

## The pieces (modular boundaries)

```
INPUTS (capture + enqueue only — no model logic):
  status-hub web UI: 2 FAB buttons + typed box     ─┐
  ESP mic (jared-voice server): 2 wake words        ─┤→ Supabase voice_requests {transcript, source, tier}
  Mac mic (mac-listener): 2 wake words              ─┘
                                                        │
WORKER (voice-pipeline/worker/worker.py — the ONE brain):
  claims a request, resolves the route, runs it, writes the reply to voice_messages
    tier == fast   → ApiRunner   (Messages API, Haiku, MCP-only)        [+ subscription fallback on error]
    tier == power  → run_claude  (subscription claude -p, Opus+thinking) [Fly: ApiRunner think=True]
                                                        │
TOOLS (the only capabilities — shared by both routes):  data · voice · govee   (MCP, stdio)
SOURCE OF TRUTH:  Supabase (felyggqjjhltwokdfhop)

DEVICES:
  Mac  — primary worker, has the Claude subscription (claude -p). launchd: com.jaredgantt.voice-worker
  Pi   — fallback worker (WORKER_PRIORITY=mac,pi). aarch64, active host.
  Fly  — cloud fallback (fly-worker/). No subscription → accuracy route uses the API there.
```

## Worker execution strategies

- **`ApiRunner`** (efficiency): direct `anthropic` Messages API. Warm MCP stdio sessions started once;
  tool schemas inlined with the prompt; full agentic loop (sequential tool calls → final reply). No
  built-in Claude Code tools, so nothing to hallucinate or discover.
- **`run_claude`** (accuracy): headless `claude -p` on the subscription (no API billing). Opus, with
  the `POWER_DIRECTIVE` (careful decomposition + `ultrathink` to unlock max thinking).

There is no third strategy. The old warm-Agent-SDK path (`SdkRunner`) was removed — it used the
`claude_code` preset (the bloat) and didn't belong to either route.

## Device fallback

- **Worker claim priority** is `WORKER_PRIORITY` (default `mac,pi`): the highest-priority worker with a
  fresh heartbeat claims; others defer. Mac → Pi today.
- **Fly** (`fly-worker/`) is the last resort and currently **dormant/undeployed**. When deployed, the
  accuracy route on Fly uses the API (`ApiRunner` with thinking) since there's no subscription there.

### Known gaps (don't pretend these are done)
- Fast route picks a device by **priority**, not a true "fastest-wins" race. Priority is a proxy for now.
- The Fly fallback worker is not deployed.

## The chat-box setting (`chatbox_route`)

`app_settings.chatbox_route` ∈ `{fast, power}`, default `power`. Set in the hub Settings panel. Applies
**only** to the typed chat box. The two buttons and the wake words carry their own tier and ignore it.
