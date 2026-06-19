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
- Config via env: `GROQ_API_KEY` (secret), `WAKE_MODEL` (default `hey_jarvis`),
  `WAKE_THRESHOLD`, `GROQ_MODEL`, `WEBHOOK_URL` (optional, where transcripts POST next).
- Deploy: `fly deploy` from `server/`. Set the key: `fly secrets set GROQ_API_KEY=...`.

### The handoff seam
`handle_transcript(text)` currently logs the transcript and, if `WEBHOOK_URL` is set,
POSTs `{"transcript": ..., "ts": ...}` to it. Replace/extend this to connect the voice
output to the status hub / Claude / actions.

## firmware/ (ESP32, PlatformIO)
- Copy `src/config.h.example` → `src/config.h`, fill WiFi + server + I2S mic pins.
- Build/flash: `~/.platformio/penv/bin/pio run -t upload` from `firmware/`.
- It's a dumb pump: reads the I2S mic and streams PCM continuously. No on-device logic.
