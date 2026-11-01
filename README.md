# voice-pipeline

ESP32 mic → Fly backend → wake word → Groq transcription. The pipeline ends at a
**transcript string**; wiring that transcript into the rest of the system happens at
the handoff seam (`handle_transcript()` in `server/server.py`).

```
ESP32 (I2S mic) ──wss://jared-voice.fly.dev/ws/audio──▶  Fly: jared-voice
  continuous 16kHz/16-bit/mono PCM                         ├─ openWakeWord  (wake detect)
                                                           ├─ capture utterance (silence endpoint)
                                                           └─ Groq whisper-large-v3-turbo
                                                                └─ handle_transcript(text)  ◀── HANDOFF
```

## server/ (Fly app `jared-voice`)
- WebSocket server, openWakeWord, Groq STT. Always-on 2GB machine.
- Config via env: `GROQ_API_KEY` (secret), `WAKE_MODEL` (default `hey_rhasspy`),
  `WAKE_THRESHOLD`, `GROQ_MODEL`, `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` (where
  transcripts enqueue), `LED_TOKEN`, `VERIFY_TOKEN`, `VOICE_DEVICE_TOKEN` (see Auth below).
- Deploy: `./assemble.sh && flyctl deploy --now -a jared-voice`, from `server/`. The assemble step
  is not optional — the Govee sync script is copied in from the govee-mcp checkout (`CLAUDE.md`). Set
  the key: `fly secrets set GROQ_API_KEY=...`.
- The same image also runs the Govee inventory sync (`entrypoint.sh`). The voice worker is not here;
  it is its own app, below.

## fly-worker/ (Fly app `jared-voice-worker`)
- The voice worker (`worker/worker.py`, `WORKER_ID=fly`) alone on a `shared-cpu-1x` 2GB machine in
  `iad`. It claims first (`WORKER_PRIORITY=fly,pi,mac`); the Pi, then the Mac, are fallbacks
  (`docs/WORKER-PLACEMENT.md` has the measurements behind that order).
- A 1GB volume `worker_data` at `/data` holds Claude's run transcripts (`CLAUDE_CONFIG_DIR=/data/claude`,
  pruned after 30 days), the worker's state (`STATE_DIR=/data/voice-worker`) and `/data/worker.log`
  (everything the machine prints, trimmed daily to 50,000 lines).
- `entrypoint.sh` restarts the worker whenever it exits, passes Fly's stop signal on to it, and
  updates Claude Code daily.
- Deploy: `fly-worker/deploy.sh`, which assembles the build context from the checkouts and bakes in
  the newest Claude Code. It builds and pushes first, then waits (up to 5 minutes) until no request is
  claimed by `fly` before swapping the machine, because a turn running at the swap is orphaned and
  never answered. Secrets: `SUPABASE_URL`, `SUPABASE_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `GOVEE_API_KEY`, `LED_TOKEN`.
- It is a separate app from `jared-voice` because, sharing one, a deploy of either restarted both and
  the worker shared a CPU with transcription.

### The handoff seam
`handle_transcript(text)` logs the transcript and, if `SUPABASE_URL` and
`SUPABASE_SERVICE_KEY` are set, enqueues it into the Supabase `voice_requests` table
(normally by PATCHing the row opened at capture-accept to `pending`, else an `INSERT`).
Anon has no INSERT on `voice_requests`: a pending row is run by the worker as Jared with
every tool, so only server-side holders of the service key may write one. Downstream consumers read off that
queue. It's best-effort and never raises.

### Auth
Every HTTP control route requires a token in `?token=`: `/led-*`, `/enroll-*`, `/capture-*` and
`/wakefree-*` take `LED_TOKEN` (the workers and the Mac listener hold it), `/verify` takes
`VERIFY_TOKEN` (the hub holds it). The server refuses to start without both, or without
`SUPABASE_SERVICE_KEY`.

The ws handshake must present `VOICE_DEVICE_TOKEN` — via `?token=...` on the ws path, an
`X-Device-Token` header, or `Authorization: Bearer ...` — or it is closed (1008). While that secret is
unset every connection is accepted, and the connect log line says `(device token presented)` when the
puck sent one. The firmware sends `DEVICE_TOKEN` from its gitignored `config.h`; set the secret only
with `firmware/bin/enable-device-token.sh`, which flashes the puck, waits for it to present the token,
sets the secret, and removes it again if the puck is then rejected. Setting it any other way before
the puck sends the token locks the puck out.

## firmware/ (ESP32, PlatformIO)
- Copy `src/config.h.example` → `src/config.h`, fill WiFi + server + I2S mic pins and `DEVICE_TOKEN`
  (must equal `VOICE_DEVICE_TOKEN` on `jared-voice`; see Auth above).
- Build/flash: `~/.platformio/penv/bin/pio run -t upload` from `firmware/`.
- It's a dumb pump: reads the I2S mic and streams PCM continuously. No on-device logic.

## Where the rest of it is documented

- `CLAUDE.md` — what to know before editing: the build context under `server/`, the API-key trap,
  what is dead.
- `docs/ROUTING.md` — the path from a transcript to an action: the one Opus route, what every turn
  is told, and how sends are fenced per mic.
- `ARCHITECTURE.md` — the always-on machinery: workers and failover, wake-free capture, speaker ID,
  the feedback channels, and the `app_settings` catalog.
- Per-directory READMEs in `worker/`, `orchestrator/`, `mcp-voice/`, `voice-id/`.
