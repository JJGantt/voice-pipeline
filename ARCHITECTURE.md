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
command survives into how it is processed. The one thing a device adds is where it was
(`voice_requests.location`, sent by the watch and the browser): the worker puts it on the prompt as a
single line of context and writes it to `STATE_DIR/cmd_loc`, which the data MCP appends to any
session the turn starts.

## Worker execution strategies

Which runner a turn uses is **not** a per-machine choice — the runners are stages of the same turn,
and every ordinary stage bills the **subscription**:

- **`SdkRunner`** — the warm Claude Agent SDK client: one long-lived headless Claude Code process
  holding the MCP servers (data, voice, lights, feeder and Gmail over stdio; iMessage over HTTP to the
  Mac), pinned to Sonnet + `SAFE_TOOLS` at connect, which include WebSearch and WebFetch. Each hub turn is a
  fresh `session_id` on that warm process, with the recent hub conversation replayed into the prompt.
  This is the **fast path** on the Mac and the Pi: the spawn cost `claude -p` pays per command — CLI
  plus four MCP servers, 3.6s on the Pi, ~42% of a simple turn before the model sees a token — is
  paid once at connect.
- **`run_claude`** — headless `claude -p`, also on the subscription. It streams `stream-json`, so
  tool calls (and the text between them) surface live on the activity bar. This runs the **escalation** (Opus, full toolset —
  rare, and already slow enough that the CLI cold start is noise), photo turns (vision needs the
  CLI's `Read`), and full-agent turns.
- **`ApiRunner`** — the direct `anthropic` Messages API on the Console key. Warm MCP stdio sessions
  started once, tool schemas inlined with the prompt, full agentic loop, no built-in Claude Code
  tools (web search and fetch are the API's server tools instead); the stable prefix (schemas + system, ~8k tokens) is **prompt-cached for 1 hour**
  (`cache_control ttl:1h` + the `extended-cache-ttl` beta). It is the funded **fallback**: it runs
  only when a subscription path errors, at the SAME model tier — errors never upgrade the model —
  and it is the only runner on Fly (no subscription login there), which runs Opus on it.

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

## One utterance can be several rows, and only the fullest one runs

Two always-on mics hear the same speech and endpoint at different pauses, so a single spoken thing
reaches `voice_requests` as two, three, sometimes four rows. Two rules keep that from becoming two,
three or four commands:

**Nothing is claimed while a mic is still hearing him.** Every capture source already stamps
`voice_presence.heard_jared_at` on each confirmed check; the worker now waits until every mic has been
quiet for `MIC_QUIET_S` (2.5s) before claiming anything, with `MAX_HOLD_S` as the backstop against a
wedged mic parking a command for ever. Short commands never notice — by the time "turn the light blue"
is enqueued, nothing has heard anything for seconds.

**Among the captures of one utterance, the longest transcript wins**, not the first to arrive. Rows
are grouped transitively by overlapping recording windows (`capture_start`/`capture_end`, padded for
clock skew), the fullest is claimed, and the rest are marked `(superseded …)` before it runs — marked,
never deleted, so a bad grouping is visible instead of a command that vanished.

This is not a refinement. Without it, on 2026-08-27, the Mac cut a 65-second dictation into two
mid-sentence fragments and enqueued them while the ESP was still recording; the fragments ran as
commands, and the ESP's complete 888-character capture — the actual instruction, which asked for a
session to be started — was discarded as a duplicate of them. Earliest-wins is the wrong rule
whenever one mic endpoints early, which is most of the time on dictation.

Captures are kept for **7 days** (`sweepRetention`, pi-desk): the only time a raw request is read is
shortly after something went wrong with it. The conversation is not affected — `voice_messages` has
no cascade from `voice_requests`, so the readable thread outlives the capture row.

**The interesting version is not built.** If the Mac hears the first half and the ESP the second — you
walk between rooms mid-sentence — longest-wins keeps one half and loses the other. Stitching the
halves together by their recording windows would make mic handoff work, and would be a genuinely nice
property. It is deliberately not attempted yet: a naive concatenation would say a clause twice inside
a command, which is worse than dropping one.

## Feedback is universal — never scoped to which input captured the command

Capture is arbitrary. Which device hears a wake word first (Mac mic vs ESP mic) is **not
deterministic** — it comes down to who's closest/loudest in the moment — and it doesn't matter.
Feedback must look identical and appear **everywhere** regardless of who happened to capture it. It's
one system; the surfaces act together. Never scope feedback to the capturing device.

Three signals, and only the first is local:

- **Sound at a mic — LOCAL, and barely shown.** Speech energy opening a capture means nothing yet (it is
  the TV as often as not), so it shows only as the Mac HUD's faint "listening" for the Mac's own mic,
  and as nothing at all on the ESP.
- **Hearing Jared — UNIVERSAL.** The instant any mic's speaker check confirms him it stamps
  `voice_presence.heard_jared_at`, and every surface renders that one row: the ESP light goes solid and
  the Mac HUD locks whether the Mac, the ESP, or both are the ones hearing him. This is the signal that
  answers "is anything listening to me", and it is deliberately not scoped to the capturing device.
- **Processing + response — UNIVERSAL.** "working" (the brain is running), "done", and the spoken
  reply reflect **one shared command lifecycle** that every surface subscribes to and renders the
  same. An ESP-captured command still lights the Mac HUD and speaks on the Mac; a Mac-captured
  command still blinks the ESP light. Drive these off the lifecycle in Supabase (`voice_requests`
  status + the speech channel), **never** off "did I enqueue this row."

These are **two independent channels** — visual state and a single spoken line. Don't conflate them.

**1. Visual state (HUD pop-ups + ESP light).** The state machine, driven by system state in Supabase
(`voice_requests` status) — NOT by the model:

```
hearing   → UNIVERSAL (every light / HUD, on any mic's confirmed check — voice_presence.heard_jared_at)
working   → UNIVERSAL (every screen / light, from capture-accept through the worker's turn — voice_requests)
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
- **Mac listener** holds one persistent realtime subscription (`RealtimeFeedback`): drives its on-screen
  HUD off `voice_requests` status (`claimed`→working / `done`→done) and off `voice_presence` ("Hearing
  you", from ANY mic), and shows `listening` locally for its own mic. (`voice_speech` is played by the
  Pi audio service, not here.)
- **Worker** drives the ESP light for every command — `/led-blink` on working, `/led-off` on done
  (jared-voice server's generic `/led-<state>` HTTP route).
- Nothing is `request_id`-gated: the Mac speaks and lights up for an ESP- or phone-captured command the
  same as for one it captured itself.
- **The ESP light is a single-owner state machine** (server-side, `hearing > working > idle`). Every
  input — the ESP's own confirmed capture, the Mac's `voice_presence` stamps over realtime, the
  capturers' and worker's `/led-blink|off` pings, and the `voice_requests` lifecycle over realtime —
  feeds the SAME flags and renders with precedence, so two paths can never fight the bulb. (The old
  code let the local capture machine and the worker's pings stomp each other last-writer-wins, which is
  what produced the solid→off→blink flicker when both mics heard one command.) There is ONE working
  backstop timer, re-armed on every working edge and cancelled on done; stacked per-ping timers used to
  switch the light off under a later command. Every change of the bulb's state is logged in `fly logs`
  as `LED <cmd> (<why>)`, and a failed send is logged rather than swallowed — when the light does
  nothing, that log answers whether the server ever asked it to.

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
  - **An accepted capture reaches BACK for the words before its trigger.** A capture that has not
    confirmed him within 3.5s is dropped as the TV — and if he is mid-sentence, the next capture opens
    on the next frame, so the first 3.5s of the command used to be gone (56 of 184 Mac commands in the
    week to 2026-09-10 began within 1s of such a drop; "scoop of protein powder to the smoothie" was one).
    Both mics now keep the last 8s of raw audio, and at capture-accept the speaker gate walks backward
    through it in 0.5s steps, prepending each 2s window that still scores as him (bar 0.22: 99% of his
    own clean windows pass it, TV tops out ~0.18), stopping at silence, another voice, or 5s.
    `capture_start` moves back with it, so the worker's grouping and the data MCP's "when did he say
    it" see the true start. `PREROLL_S`/`HISTORY_S`/`BACKFILL_*` are one setting written twice —
    `server.py` and `wakefree.py`.
  - **The confirmed span is verified once more before anything is transcribed.** The live gate only
    ever scores 2s slices, and one slice over the confirm bar commits the capture — which is how a TV
    voice fluking a single slice past 0.30 got transcribed and ran a model turn that answered
    `__IGNORE__` (321 such turns in the week to 2026-09-10). A longer stretch embeds far more steadily
    than any slice: his own 4s clips never scored under 0.23, 10s clips never under 0.37, while other
    voices (JFK, synthetic male voices) land at 0.00–0.16. So at accept, after the back-fill, the
    stretch from the first confirming window to the last (first 15s of it) is scored once, and a
    capture under `voiceid_accept_threshold` is dropped before STT, logged as `confirmed span sim=… —
    dropped` with every slice score it had. It is the SPAN, never the whole capture: the capture also
    holds the pre-confirm lead-in and the trailing windows that closed it, and on the far mic those
    are the TV — a real 14.9s ESP command whose confirmed windows scored 0.33/0.31/0.27 scored 0.20 as
    a whole and was dropped on 2026-09-11. Accepted captures log their span score too. **The row is
    0 (gate off) until those logs show what his far-field spans score on the ESP**; the close-mic
    recordings that set 0.22 say nothing about that mic.
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
| `voiceid_threshold` | `0.30` | wakefree + ESP server — "it's Jared" cosine threshold for the live 2s-slice gate |
| `voiceid_accept_threshold` | `0` | wakefree + ESP server — the confirmed-span bar at capture-accept; a capture under it is dropped before STT. `0` = off (measure only). No hub UI yet; set the row directly |
| `wakefree_pause_s` | `2.0` | ESP server only — the energy endpoint it falls back to while wake-free is on but ECAPA/the voiceprint has not loaded (and during enrollment). The speaker-gated path ignores it |
| `voiceprint` | — | wakefree (local) + ESP server — base64 ECAPA exemplar bank (uploaded by `enroll.py`) |
| `weight_reminder_*`, `surplus_cal` | — | hub server (reminders / nutrition) |
