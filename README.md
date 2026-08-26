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
- WebSocket server, openWakeWord, Groq STT. Always-on 1GB machine.
- Config via env: `GROQ_API_KEY` (secret), `WAKE_MODEL` (default `hey_rhasspy`),
  `WAKE_THRESHOLD`, `GROQ_MODEL`, `SUPABASE_URL` + `SUPABASE_ANON_KEY` (where
  transcripts enqueue), `VOICE_DEVICE_TOKEN` (optional ws auth — see below).
- Deploy: `./assemble.sh && flyctl deploy --now -a jared-voice`, from `server/`. The assemble step
  is not optional — most of that directory is copied in from elsewhere (`CLAUDE.md`). Set the key:
  `fly secrets set GROQ_API_KEY=...`.
- The same image also runs the **fallback worker** and the Govee inventory sync (`entrypoint.sh`).

### The handoff seam
`handle_transcript(text)` logs the transcript and, if `SUPABASE_URL` and
`SUPABASE_ANON_KEY` are set, enqueues it into the Supabase `voice_requests` table
(`INSERT` of `{"transcript": ..., "source": "fly"}` via the anon key — INSERT-only
by RLS; the table fills the rest with defaults). Downstream consumers read off that
queue. It's best-effort and never raises.

### WebSocket auth
If `VOICE_DEVICE_TOKEN` is set, the ws handshake must present that token — via
`?token=...` on the ws path, an `X-Device-Token` header, or `Authorization: Bearer
...` — or the connection is closed (1008). If it's unset, all connections are
accepted, so existing firmware keeps working until it's updated to send the token.

## firmware/ (ESP32, PlatformIO)
- Copy `src/config.h.example` → `src/config.h`, fill WiFi + server + I2S mic pins.
- Build/flash: `~/.platformio/penv/bin/pio run -t upload` from `firmware/`.
- It's a dumb pump: reads the I2S mic and streams PCM continuously. No on-device logic.

## Where the rest of it is documented

- `CLAUDE.md` — what to know before editing: the build context under `server/`, the API-key trap,
  what is dead.
- `docs/ROUTING.md` — the path from a transcript to an action, which tools the fast Sonnet path may
  use, and why `SAFE_TOOLS` must stay an allow list rather than a deny list.
- `ARCHITECTURE.md` — the always-on machinery: workers and failover, wake-free capture, speaker ID,
  the feedback channels, and the `app_settings` catalog.
- Per-directory READMEs in `worker/`, `orchestrator/`, `mcp-voice/`, `voice-id/`.
