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
import struct
import time
import wave

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
SILENCE_RMS = 500             # int16 RMS below this counts as silence
SILENCE_HANGOVER_S = 0.8      # end utterance after this much trailing silence
MAX_UTTERANCE_S = 12.0        # hard cap
PREROLL_S = 0.5               # audio kept from just before the wake hit

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
    capturing = False
    utterance = bytearray()
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

                scores = oww.predict(frame)
                if scores.get(WAKE_MODEL, 0.0) >= WAKE_THRESHOLD:
                    log.info("wake word '%s' (%.2f)", WAKE_MODEL, scores[WAKE_MODEL])
                    capturing = True
                    utterance = bytearray(preroll)
                    preroll.clear()
                    started = time.monotonic()
                    last_voice = started
            else:
                utterance.extend(raw)
                now = time.monotonic()
                if rms(frame) >= SILENCE_RMS:
                    last_voice = now
                done = (now - last_voice >= SILENCE_HANGOVER_S) or \
                       (now - started >= MAX_UTTERANCE_S)
                if done:
                    dur = len(utterance) / 2 / SAMPLE_RATE
                    log.info("utterance captured: %.1fs", dur)
                    try:
                        text = await transcribe(bytes(utterance))
                        await handle_transcript(text)
                    except Exception as e:
                        log.exception("transcription failed: %s", e)
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
