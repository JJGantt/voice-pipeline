"""Environment configuration, read once at import. Nothing here changes at runtime."""

import os

PORT = int(os.environ.get("PORT", "8080"))
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_rhasspy")            # "Hey Rhasspy" — equivalent wake words:
WAKE_MODEL_POWER = os.environ.get("WAKE_MODEL_POWER", "hey_jarvis") # "Hey Jarvis" — either one wakes capture;
                                                                    # routing is the worker's dynamic dispatch
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
GAIN = float(os.environ.get("GAIN", "15"))   # software boost for the quiet 16-bit mic
SILENCE_HANGOVER_S = float(os.environ.get("HANGOVER_S", "3.0"))  # trailing silence to end (env-tunable)
MAX_UTTERANCE_S = float(os.environ.get("MAX_UTTERANCE_S", "600"))  # absolute safety cap (10 min)

GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
# The second opinion: the other Groq Whisper, run on the same clip in parallel. The two mishear different
# words, so when their readings differ the worker hands the model both (voice_requests.alt_transcript,
# docs/ROUTING.md § A command can arrive with two transcripts). The hub does the same for its commands.
ALT_GROQ_MODEL = "whisper-large-v3" if GROQ_MODEL != "whisper-large-v3" else "whisper-large-v3-turbo"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")   # second transcription provider, after Groq

# Supabase enqueue target for transcripts. Every write here uses SUPABASE_SERVICE_KEY: anon has no
# INSERT on voice_requests, because a pending row is run by the worker as the owner with every tool.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
# The ESP's token on the ws handshake. Required: main() refuses to start without it, so the audio socket
# (a stream that becomes commands run with every tool) can never be open to anyone by default.
VOICE_DEVICE_TOKEN = os.environ.get("VOICE_DEVICE_TOKEN", "")
# Shared secret for every control route: the worker's /led-*, the Mac listener's /capture-* and
# /wakefree-*, and /enroll-*. Required — main() refuses to start without it.
LED_TOKEN = os.environ.get("LED_TOKEN", "")
# Shared with the hub, which asks /verify whether an uploaded clip sounds like the speaker. Required.
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
