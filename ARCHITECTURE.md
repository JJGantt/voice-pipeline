# Voice / Hub Agent — Architecture

The always-on machinery: who captures speech, who processes it, and how every surface stays in step.
How a single turn is *routed* once it reaches the worker is `docs/ROUTING.md`; `CLAUDE.md` is what to
know before editing anything here.

## The pieces (modular boundaries)

```
INPUTS (capture + enqueue only — no model logic):
  status-hub web UI: the voice FAB + typed box     ─┐
  ESP mic (jared-voice server): wake word           ─┤→ Supabase voice_requests {transcript, source}
  Mac mic (mac-listener / wakefree.py)              ─┘
                                                        │
WORKER (worker/worker.py — the ONE brain):
  claims a request, runs it, writes the reply to voice_messages
    hub turn      → Sonnet on SAFE_TOOLS, escalating to a fresh Opus run (docs/ROUTING.md)
    full-agent    → the unrestricted Claude Code agent in `agent_cwd`, its own persistent session
                                                        │
TOOLS (the only capabilities): data · voice · govee   (MCP, stdio)
SOURCE OF TRUTH:  Supabase (felyggqjjhltwokdfhop)

DEVICES (claim order pi → mac → fly):
  Pi   — first claimer (systemd voice-worker.service). Also runs pi-desk, the desk-command consumer.
  Mac  — fallback (launchd com.jaredgantt.voice-worker). Has the Claude subscription, like the Pi.
  Fly  — last resort, bundled into the jared-voice server image (server/, WORKER_ID=fly).
```

Inputs are thin by design: they capture and enqueue one row, and nothing about which device heard the
command survives into how it is processed.

## Worker execution strategies

- **`run_claude`** — headless `claude -p` on the **subscription** (no API billing). This is the path
  on the Mac and the Pi. It streams `stream-json`, so tool calls surface live on the activity bar.
- **`ApiRunner`** — the direct `anthropic` Messages API, used on Fly, which has no subscription
  login. Warm MCP stdio sessions started once, tool schemas inlined with the prompt, full agentic
  loop, no built-in Claude Code tools. The stable prefix (schemas + system, ~8k tokens) is
  **prompt-cached for 1 hour** (`cache_control ttl:1h` + the `extended-cache-ttl` beta), which is
  what makes always-on affordable.

Photo turns are subscription-only: the CLI's `Read` tool is how a model views an image file, so a
photo request that reaches Fly is refused rather than run blind.

## Device fallback

- **Worker claim priority** is `WORKER_PRIORITY`, and `worker/run.sh` sets it to **`pi,mac,fly`** on
  every machine that launches through it: the highest-priority worker with a fresh heartbeat claims
  and the others defer, so the **Pi takes a command whenever it is up** and the Mac is the fallback.
  That is deliberate — a session started by voice must outlive the laptop closing. `worker.py`'s own
  in-file default (`mac,pi`) is only what applies if something starts it without `run.sh`. Claiming
  is an atomic `PATCH ... status=eq.pending`, so it is safe even when two workers race.
- **Worker affinity** overrides priority while a hub context window is live: the window stays pinned
  to the worker that owns it, so a conversation and its machine-local context aren't lost to a
  needless switch. Affinity releases when the window resets or the owner goes silent past
  `OWNER_GRACE_S`.
- A deferring worker drops to a **slower heartbeat and poll** (still inside `HEARTBEAT_FRESH_S`, so it
  never looks dead to the workers below it) and ramps back up when the primary goes quiet.

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
word, every source (Mac *and* ESP) gates on Jared's voice; OFF → the wake word everywhere.

- **Speaker ID + one shared voiceprint.** ECAPA-TDNN. `enroll.py` saves a bank of per-phrase embeddings
  (`exemplars.npy`) **and uploads it to `app_settings.voiceprint`** so the ESP/Fly server uses the exact
  same one. An utterance is verified by cosine vs the closest exemplars (top-3 mean); ≥ `voiceid_threshold`
  (default 0.30) = Jared. Enroll from **separate command-shaped phrases** (short ones + far-away
  `--append` passes) — like-against-like coverage is what makes short/distant commands verify, not
  slices of one monologue.
- **Capture — same logic on both ends.** Segment on speech energy → hold the capture open on Jared's
  *verified presence* (re-score the trailing 2s with ECAPA every 0.7s) → close after N consecutive
  non-confirming checks → trim silence → transcribe (Groq) + enqueue a `voice_requests` row. The TV /
  other people are filtered for free (never transcribed); the worker's `__IGNORE__` directive drops any
  of *your* speech that isn't a command.
  - **N rises with how long he has been talking** (`absent_misses()` in `wakefree.py`,
    `_absent_misses()` in `server.py`): 1 miss (~2s of pause tolerated) for a short command, up to 5
    (~4.8s) past ~32s of speech. A one-breath command should end the instant he stops rather than
    record the room afterwards; a long dictated one is full of thinking-pauses, and cutting one in half
    loses the entire command — which costs far more than a few seconds of trailing silence.
  - **Mac:** `voice-id/wakefree.py` (ECAPA local, on-demand).
  - **ESP:** the `jared-voice` Fly server runs the same ECAPA + the shared voiceprint (lazy-loaded on
    turn-on; the box is **2 GB** for it) and captures on energy instead of openWakeWord while on.

### State lives in the DB — every host reads it itself (the Mac is NOT the source of truth)
`app_settings` (written by the hub) is the single source of truth. **Fly is the always-on host that
runs the ESP precisely because the Mac is intermittent**, so the Fly server reads its own state
straight from `app_settings` — on boot AND live via its own realtime subscription (`_settings_listener`
in `server.py`): `wakefree_enabled` (→ load ECAPA + capture mode), `voice_muted` (→ global mute),
`voice_inject_mode`, `voiceid_threshold`/`wakefree_pause_s`/`voiceprint`. A server restart or a
sleeping Mac cannot strand the ESP in the wrong mode — it restores itself from the DB.
- The Mac listener still mirrors these to the ESP over HTTP (`/wakefree-on|off`, `/capture-off|on`,
  `/wakefree-route`) as a **redundant fast-path**, and still does the Mac-local work (mic handoff,
  start/stop `wakefree.py`). But that bridge is no longer load-bearing — if the Mac is off, Fly already
  has the state. (The LED is likewise host-independent: the worker that drives it fails over Mac→Pi→Fly.)

### Global mute — any explicit input deafens every always-on listener
An explicit command — push-to-talk dictation, or the hub's voice FAB — silences every always-on
listener while it is in flight, so nothing else captures the same speech. One shared flag,
`app_settings.voice_muted`:
- **Set by:** the voice FAB (web, while recording) and push-to-talk (Hammerspoon).
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
| `session_reset_s` | `300` | worker — idle seconds before the hub conversation resets |
| `wakefree_enabled` | `0` | listener + ESP server — global wake-free on/off (mic handoff + ESP bridge) |
| `voice_muted` | `0` | listener + ESP — global mute (set by the FAB / push-to-talk) |
| `voice_inject_mode` | `0` | listener + ESP — Mac mic pastes into the focused window; the ESP goes deaf |
| `agent_mode` / `agent_cwd` | `0` | worker — full-agent (coding) mode and its working directory |
| `synth_on_mac` | `0` | worker — synthesize the reply on the Mac and ship PCM to the Pi to play |
| `voiceid_threshold` | `0.30` | wakefree + ESP server — "it's Jared" cosine threshold |
| `wakefree_pause_s` | `2.0` | ESP server only — the energy endpoint it falls back to while wake-free is on but ECAPA/the voiceprint has not loaded (and during enrollment). The speaker-gated path ignores it |
| `voiceprint` | — | wakefree (local) + ESP server — base64 ECAPA exemplar bank (uploaded by `enroll.py`) |
| `weight_reminder_*`, `surplus_cal` | — | hub server (reminders / nutrition) |
