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
  Fly  — cloud fallback, bundled into the jared-voice server image (server/, WORKER_ID=fly). No subscription → accuracy route uses the API there.
```

## Worker execution strategies

- **`ApiRunner`** (efficiency): direct `anthropic` Messages API. Warm MCP stdio sessions started once;
  tool schemas inlined with the prompt; full agentic loop (sequential tool calls → final reply). No
  built-in Claude Code tools, so nothing to hallucinate or discover. The stable prefix (tool schemas +
  system, ~8k tokens) is **prompt-cached for 1 hour** (`cache_control ttl:1h` + the `extended-cache-ttl`
  beta), so warm commands pay ~10% on that prefix — what makes always-on / wake-free affordable.
- **`run_claude`** (accuracy): headless `claude -p` on the subscription (no API billing). Opus, with
  the `POWER_DIRECTIVE` (careful decomposition + `ultrathink` to unlock max thinking).

There is no third strategy. The old warm-Agent-SDK path (`SdkRunner`) was removed — it used the
`claude_code` preset (the bloat) and didn't belong to either route.

## Device fallback

- **Worker claim priority** is `WORKER_PRIORITY` (default `mac,pi`): the highest-priority worker with a
  fresh heartbeat claims; others defer. Mac → Pi today.
- **Fly** is the last resort, bundled into the jared-voice server image (built from `server/` by
  `assemble.sh`; runs `worker.py` as `WORKER_ID=fly`, deferring to Mac/Pi). Its accuracy route uses the
  API (`ApiRunner` with thinking) since there's no subscription there.

### Known gaps (don't pretend these are done)
- Fast route picks a device by **priority**, not a true "fastest-wins" race. Priority is a proxy for now.
- The Fly fallback worker is not deployed.

## The chat-box setting (`chatbox_route`)

`app_settings.chatbox_route` ∈ `{fast, power}`, default `power`. Set in the hub Settings panel. Applies
**only** to the typed chat box. The two buttons and the wake words carry their own tier and ignore it.

## Feedback is universal — never scoped to which input captured the command

Capture is arbitrary. Which device hears a wake word first (Mac mic vs ESP mic) is **not
deterministic** — it comes down to who's closest/loudest in the moment — and it doesn't matter.
Feedback must look identical and appear **everywhere** regardless of who happened to capture it. It's
one system; the surfaces act together. Never scope feedback to the capturing device.

Two kinds of feedback, two scopes:

- **Capture indicator — LOCAL.** Only the device with the open mic can show "I'm listening": the Mac
  HUD pill on the Mac mic, the ESP light on the ESP mic. This is the one inherently-local signal — a
  device only knows about its own microphone.
- **Processing + response — UNIVERSAL.** "working" (the brain is running), "done", and the spoken
  reply reflect **one shared command lifecycle** that every surface subscribes to and renders the
  same. An ESP-captured command still lights the Mac HUD and speaks on the Mac; a Mac-captured
  command still blinks the ESP light. Drive these off the lifecycle in Supabase (`voice_requests`
  status + the speech channel), **never** off "did I enqueue this row."

These are **two independent channels** — visual state and a single spoken line. Don't conflate them.

**1. Visual state (HUD pop-ups + ESP light).** The state machine, driven by system state in Supabase
(`voice_requests` status) — NOT by the model:

```
listening → LOCAL     (only the capturing device's mic indicator)
working   → UNIVERSAL (every screen / light, the instant the worker claims the turn)
done      → UNIVERSAL (every screen / light clears when the turn ends)
```

**2. The spoken line (the `say` tool).** This is **not** a state transition and it does **not**
narrate the lifecycle. It is the model saying **one** succinct thing about what it's doing — e.g.
"changing the light to blue" — fired *alongside* the action tool so synthesis overlaps execution
instead of stacking after it. Present tense, one short utterance per command (the `fly`/`mac` voice
sources get a directive instructing exactly this). The model's final text is **not** spoken. The `say`
tool lives in the `voice` MCP (`mcp-voice`) and inserts one row into the `voice_speech` table.

So three distinct streams, none of which is the others: display thread = `voice_messages`; transient
activity/state = the visual lifecycle above (+ `push_activity` tool events on the web activity bar);
audio = the one `voice_speech` line. Only the last is played aloud.

**Implemented (live, not aspirational):**
- **Mac listener** holds one persistent realtime subscription (`RealtimeFeedback`): plays every
  `voice_speech` line aloud (Kokoro via an on-demand warm daemon — see below), drives its on-screen HUD
  off `voice_requests` status (`claimed`→working / `done`→done), and shows `listening` locally for its
  own mic.
- **Worker** drives the ESP light for every command — `/led-blink` on working, `/led-off` on done
  (jared-voice server's generic `/led-<state>` HTTP route).
- Nothing is `request_id`-gated: the Mac speaks and lights up for an ESP- or phone-captured command the
  same as for one it captured itself.
- **The ESP light is a single-owner state machine** (server-side, `working > capturing > idle`). Every
  change — the ESP's own capture, the worker's `/led-blink|off`, and the Mac's at-enqueue `/led-blink` —
  feeds the SAME two flags and renders with precedence, so two paths can never fight the bulb. (The old
  code let the local capture machine and the worker's pings stomp each other last-writer-wins, which is
  what produced the solid→off→blink flicker when both mics heard one command.) `/led-off` (done) is the
  one reset that clears everything; an enqueued capture stays solid until the worker's blink takes over,
  so the light never dips to off mid-command.

### TTS engine (Kokoro, on-demand)
The spoken line plays through a local Kokoro daemon (`~/scripts/kokoro_daemon.py`). It is **not** kept
resident (the model is ~1 GB): every capture source pre-warms it the **instant speech capture starts**,
so the ~7s model load overlaps capture + STT + the model's thinking and the engine is hot by reply
time. Warmed by: the wake word firing (`listener.py`), wake-free speech-energy start (`wakefree.py`),
and — as a backstop for the ESP path — any claimed `voice_request` (`listener.py`). It binds its socket
immediately and queues lines that arrive mid-load, and **self-exits after 30s idle** to give the RAM
back. Zero permanent cost, no cold delay. (Warming at the worker *claim* alone was too late: the claim
lands only ~2–3 s before the spoken line, but the cold load needs ~7 s — hence the old ~5 s delay.)

While **wake-free mode is on**, the listener *pins* the daemon warm (a `/tmp/kokoro.pin` file the idle
loop honours) and warms it on the toggle — so spaced-out commands in an active session never hit a cold
reload between them. Turning wake-free off removes the pin and the daemon idle-exits as usual.

## Wake-free mode (speaker-ID gated) — ONE system, every source

A toggleable always-listening mode: no wake word — you just talk, and your **voice** is the gate. It's
a **global** mode (`app_settings.wakefree_enabled`), congruent across every listener: ON → no wake
word, every source (Mac *and* ESP) gates on Jared's voice; OFF → the two wake words everywhere.

- **Speaker ID + one shared voiceprint.** ECAPA-TDNN. `enroll.py` saves a bank of per-phrase embeddings
  (`exemplars.npy`) **and uploads it to `app_settings.voiceprint`** so the ESP/Fly server uses the exact
  same one. An utterance is verified by cosine vs the closest exemplars (top-3 mean); ≥ `voiceid_threshold`
  (default 0.30) = Jared. Enroll from **separate command-shaped phrases** (short ones + far-away
  `--append` passes) — like-against-like coverage is what makes short/distant commands verify, not
  slices of one monologue.
- **Capture — same logic on both ends.** Segment on speech energy → end on a ~2s pause
  (`wakefree_pause_s`) → trim silence → verify the speaker on the whole utterance → only then transcribe
  (Groq) + enqueue `voice_requests {tier: wakefree_route}`. The TV / other people are filtered for free (never
  transcribed); the worker's `__IGNORE__` directive drops any of *your* speech that isn't a command.
  - **Mac:** `voice-id/wakefree.py` (ECAPA local, on-demand).
  - **ESP:** the `jared-voice` Fly server runs the same ECAPA + the shared voiceprint (lazy-loaded on
    turn-on; the box is **2 GB** for it) and captures on energy instead of openWakeWord while on.
- **Route (Efficiency / Accuracy).** The wake-*word* path picks the route per-utterance from which word
  fired (`Hey Rhasspy` → fast/Haiku, `Hey Jarvis` → power/Opus). Wake-free has no wake word to
  disambiguate, so it runs on **one configurable route**, `app_settings.wakefree_route` ∈ `{fast, power}`
  (default `fast`), set in the hub. `wakefree.py` reads it live per command; the listener bridges changes
  to the ESP (`jared-voice/wakefree-route?v=…`). It is a mode, not per-command — flip it to send the
  whole wake-free session to Opus.

### State lives in the DB — every host reads it itself (the Mac is NOT the source of truth)
`app_settings` (written by the hub) is the single source of truth. **Fly is the always-on host that
runs the ESP precisely because the Mac is intermittent**, so the Fly server reads its own state
straight from `app_settings` — on boot AND live via its own realtime subscription (`_settings_listener`
in `server.py`): `wakefree_enabled` (→ load ECAPA + capture mode), `voice_muted` (→ global mute),
`wakefree_route`, `voiceid_threshold`/`wakefree_pause_s`/`voiceprint`. A server restart or a sleeping
Mac can no longer strand the ESP in the wrong mode — it restores itself from the DB.
- The Mac listener still mirrors these to the ESP over HTTP (`/wakefree-on|off`, `/capture-off|on`,
  `/wakefree-route`) as a **redundant fast-path**, and still does the Mac-local work (mic handoff,
  start/stop `wakefree.py`). But that bridge is no longer load-bearing — if the Mac is off, Fly already
  has the state. (The LED is likewise host-independent: the worker that drives it fails over Mac→Pi→Fly.)
- `wakefree.py` (Mac) reads `wakefree_route` live per command; the worker maps `tier` → model.

### Global mute — any explicit input deafens every always-on listener
An **explicit, hard-coded-route** command (push-to-talk dictation, or a hub FAB button) silences every
*auto-routing* listener while it's in flight, so nothing else captures or re-routes it. One shared flag,
`app_settings.voice_muted`:
- **Set by:** the FAB buttons (web, while recording) and push-to-talk (Hammerspoon).
- **Respected by:** the Mac listener (mutes its mic / pauses `wakefree.py`) and the ESP (the listener
  pings `jared-voice/capture-off|on`; the server drops all audio, 90s safety auto-clear).
Push-to-talk also fires an instant Mac SIGUSR; the FAB reaches the listener via realtime. Either way
every source goes deaf together.

### Mic ownership (Mac)
The listener captures (wake word) iff **neither** the global mute nor wake-free holds the mic
(`_muted` / `_wakefree` / `_capture` events). When wake-free is on, `wakefree.py` owns the mic and the
listener parks (keeping `RealtimeFeedback` alive for speech + HUD).

## Settings (`app_settings`, hub Settings panel)

All live config is key/value rows in Supabase `app_settings`, edited from the hub gear, read by the
relevant process. `app_settings` is in the realtime publication so the Mac reacts event-driven (and
bridges to the ESP over HTTP).

| key | default | read by |
|---|---|---|
| `chatbox_route` | `power` | worker — route for the typed chat box only |
| `session_reset_s` | `300` | worker — idle seconds before the hub conversation resets (was 2h) |
| `wakefree_enabled` | `0` | listener — global wake-free on/off (mic handoff + ESP bridge) |
| `wakefree_route` | `fast` | wakefree (live) + ESP server — Efficiency/Accuracy route for wake-free commands |
| `voice_muted` | `0` | listener + ESP — global mute (set by FAB / push-to-talk) |
| `voiceid_threshold` | `0.30` | wakefree + ESP server — "it's Jared" cosine threshold |
| `wakefree_pause_s` | `2.0` | wakefree + ESP server — trailing-silence that ends an utterance |
| `voiceprint` | — | wakefree (local) + ESP server — base64 ECAPA exemplar bank (uploaded by `enroll.py`) |
| `weight_reminder_*`, `surplus_cal` | — | hub server (reminders / nutrition) |
