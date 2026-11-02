"""Speech to text for ESP captures: Groq Whisper, one retry, then OpenAI's Whisper; plus the second
Groq reading. This is the async, cloud-only transcriber of the always-on Fly box. The Mac's listeners
(mac-listener, voice-id/wakefree.py) and the worker's local Whisper are separate processes on other
machines with other backends, so they keep their own."""

import asyncio
import logging
import os
import re

import httpx
from groq import Groq

from . import config
from .audio import pcm_to_wav

log = logging.getLogger("voice")
groq = Groq(api_key=os.environ["GROQ_API_KEY"])

# WHISPER WRITES "Thank you." FOR A CLIP WITH NO SPEECH IN IT: silence, breath, a room. A transcript
# that is that phrase and nothing else is treated as nothing heard, so it never becomes a command. The
# cost is that saying only "thank you" is dropped too. The same rule is in mac-listener/listener.py,
# worker/voice_worker/config.py and status-hub's server/lib/transcribe.js.
_NOTHING_SAID = re.compile(r"^\W*thank\s+you\W*$", re.IGNORECASE)


def _heard(text: str) -> str:
    return "" if _NOTHING_SAID.match(text) else text


async def transcribe_alt(pcm: bytes) -> str | None:
    """The second reading, from ALT_GROQ_MODEL. Best effort: no retry and no OpenAI fallback, because a
    command is never held up or lost for want of its second opinion."""
    try:
        result = await asyncio.to_thread(lambda: groq.audio.transcriptions.create(
            file=("utterance.wav", pcm_to_wav(pcm)), model=config.ALT_GROQ_MODEL,
            response_format="text", language="en"))
    except Exception as e:  # noqa: BLE001
        log.warning("second transcriber (%s) failed: %s", config.ALT_GROQ_MODEL, e)
        return None
    return _heard((result if isinstance(result, str) else getattr(result, "text", "")).strip()) or None


async def transcribe(pcm: bytes) -> str:
    wav = pcm_to_wav(pcm)
    # Groq SDK is sync; run off the event loop.
    def _call():
        return groq.audio.transcriptions.create(
            file=("utterance.wav", wav),
            model=config.GROQ_MODEL,
            response_format="text",
            # English, always: auto-detection guesses per clip and has turned English speech into Icelandic.
            language="en",
        )
    try:
        result = await asyncio.to_thread(_call)
    except Exception as e:  # noqa: BLE001 — one retry for transient Groq/network blips
        log.warning("Groq transcription failed (%s) — retrying once", e)
        await asyncio.sleep(0.5)
        try:
            result = await asyncio.to_thread(_call)
        except Exception as e2:  # noqa: BLE001 — Groq is down: OpenAI's Whisper is the second provider
            if not config.OPENAI_API_KEY:
                raise
            log.warning("Groq failed again (%s) — trying OpenAI", e2)
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post("https://api.openai.com/v1/audio/transcriptions",
                                 headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                                 data={"model": "whisper-1", "response_format": "text", "language": "en"},
                                 files={"file": ("utterance.wav", wav, "audio/wav")})
                r.raise_for_status()   # a failure here propagates to the caller, as before
                result = r.text
    return _heard((result if isinstance(result, str) else getattr(result, "text", "")).strip())
