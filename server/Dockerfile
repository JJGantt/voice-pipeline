# jared-voice: the ESP wake-word + transcription server, and the Govee inventory sync. The voice worker
# runs in its own app, jared-voice-worker (../fly-worker).
FROM node:20-bookworm-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# python (server + openWakeWord) and libgomp for onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Install everything in ONE resolution so speechbrain can't re-pull a CUDA torch. The +cpu local
# version on the pytorch CPU index sorts higher than PyPI's CUDA build, so pip picks the CPU wheel.
RUN pip3 install --no-cache-dir --break-system-packages \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      torch -r requirements.txt python-dotenv
# pre-download the wake-word + ECAPA speaker models so the container starts instantly / offline
RUN python3 -c "import openwakeword.utils as u; u.download_models()"
RUN python3 -c "from speechbrain.inference.speaker import EncoderClassifier as E; E.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', savedir='/app/.ecapa')"

# the Govee inventory sync job (devices → Supabase govee_devices)
COPY govee-mcp/sync_devices.py /app/govee-mcp/sync_devices.py

# app code
WORKDIR /app
COPY server.py endpointing.py ops_report.py entrypoint.sh ./
COPY voice_server ./voice_server

EXPOSE 8080
CMD ["./entrypoint.sh"]
