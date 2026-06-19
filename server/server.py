"""Voice pipeline backend.

Receives a continuous 16 kHz / 16-bit / mono PCM audio stream from the ESP32 over
a WebSocket, runs openWakeWord on it, and on a wake-word hit captures the following
utterance (ending on trailing silence) and transcribes it with Groq's whisper.

The pipeline STOPS at the transcript. `handle_transcript()` is the handoff seam:
it logs the text and, if WEBHOOK_URL is set, POSTs it onward. Wire the rest of the
system in there.
"""

import asyncio
import io
import logging
import os
import time
import wave
from collections import deque

import httpx
import numpy as np
import websockets
from groq import Groq
from openwakeword.model import Model

# ---- config (env) ----------------------------------------------------------
PORT = int(os.environ.get("PORT", "8080"))
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_jarvis")  # openWakeWord pretrained name
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # optional: where transcripts go next

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1280          # 80 ms @ 16 kHz — openWakeWord's expected chunk
# Endpointing: adaptive to the room's measured noise floor (no hardcoded level).
# The floor is estimated from the ambient audio just before the wake word (TV
# included); speech is whatever rises clearly above it, so it self-calibrates.
AMBIENT_WINDOW_S = 3.0        # rolling window used to estimate the noise floor
FLOOR_MULT = 3.0             # speech must exceed floor * this
FLOOR_MIN_MARGIN = 150       # ...and at least this far above floor (guards floor≈0)
SILENCE_HANGOVER_S = 1.2     # end after this much trailing below-threshold audio
MIN_UTTERANCE_S = 0.6        # never endpoint before this (avoids instant cut)
MAX_UTTERANCE_S = 12.0       # hard cap
PREROLL_S = 0.5              # audio kept from just before the wake hit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

groq = Groq(api_key=os.environ["GROQ_API_KEY"])
oww = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


async def transcribe(pcm: bytes) -> str:
    wav = pcm_to_wav(pcm)
    # Groq SDK is sync; run off the event loop.
    def _call():
        return groq.audio.transcriptions.create(
            file=("utterance.wav", wav),
            model=GROQ_MODEL,
            response_format="text",
        )
    result = await asyncio.to_thread(_call)
    return (result if isinstance(result, str) else getattr(result, "text", "")).strip()


async def handle_transcript(text: str):
    """HANDOFF SEAM — the rest of the system hooks in here."""
    log.info("TRANSCRIPT: %r", text)
    if WEBHOOK_URL and text:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(WEBHOOK_URL, json={"transcript": text, "ts": time.time()})
        except Exception as e:
            log.warning("webhook POST failed: %s", e)


async def connection(ws):
    peer = getattr(ws, "remote_address", "?")
    log.info("ESP32 connected: %s", peer)
    oww.reset()

    pending = bytearray()           # raw bytes not yet aligned to a frame
    preroll = bytearray()           # rolling pre-wake audio
    preroll_max = int(PREROLL_S * SAMPLE_RATE) * 2
    ambient = deque(maxlen=max(1, int(AMBIENT_WINDOW_S * SAMPLE_RATE / FRAME_SAMPLES)))
    capturing = False
    utterance = bytearray()
    threshold = 0.0                 # adaptive speech/silence level, set at wake
    last_voice = 0.0
    started = 0.0

    async for msg in ws:
        if not isinstance(msg, (bytes, bytearray)):
            continue
        pending.extend(msg)

        while len(pending) >= FRAME_SAMPLES * 2:
            raw = bytes(pending[: FRAME_SAMPLES * 2])
            del pending[: FRAME_SAMPLES * 2]
            frame = np.frombuffer(raw, dtype=np.int16)

            if not capturing:
                preroll.extend(raw)
                if len(preroll) > preroll_max:
                    del preroll[: len(preroll) - preroll_max]
                ambient.append(rms(frame))      # track the room's noise floor

                scores = oww.predict(frame)
                if scores.get(WAKE_MODEL, 0.0) >= WAKE_THRESHOLD:
                    floor = float(np.median(ambient)) if ambient else 0.0
                    threshold = max(floor * FLOOR_MULT, floor + FLOOR_MIN_MARGIN)
                    log.info("wake word '%s' (%.2f)  floor=%.0f thresh=%.0f",
                             WAKE_MODEL, scores[WAKE_MODEL], floor, threshold)
                    await ws.send("LED_BLINK")   # listening
                    capturing = True
                    utterance = bytearray(preroll)
                    preroll.clear()
                    started = time.monotonic()
                    last_voice = started
            else:
                utterance.extend(raw)
                now = time.monotonic()
                if rms(frame) > threshold:
                    last_voice = now
                elapsed = now - started
                done = (elapsed >= MIN_UTTERANCE_S and now - last_voice >= SILENCE_HANGOVER_S) \
                       or elapsed >= MAX_UTTERANCE_S
                if done:
                    arr = np.frombuffer(bytes(utterance), dtype=np.int16)
                    dur = arr.size / SAMPLE_RATE
                    reason = "silence" if (now - last_voice >= SILENCE_HANGOVER_S) else "maxlen"
                    peak = int(np.abs(arr).max()) if arr.size else 0
                    log.info("utterance: %.1fs peak=%d rms=%.0f end=%s (thresh=%.0f)",
                             dur, peak, rms(arr), reason, threshold)
                    try:
                        text = await transcribe(bytes(utterance))
                        await handle_transcript(text)
                    except Exception as e:
                        log.exception("transcription failed: %s", e)
                    await ws.send("LED_OFF")     # back to idle
                    capturing = False
                    utterance = bytearray()
                    oww.reset()

    log.info("ESP32 disconnected: %s", peer)


async def main():
    log.info("voice backend up on :%d  wake=%s  stt=%s", PORT, WAKE_MODEL, GROQ_MODEL)
    async with websockets.serve(connection, "0.0.0.0", PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
