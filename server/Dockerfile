FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# openWakeWord needs libgomp for onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the pretrained wake-word + melspectrogram/embedding models at build
# time so the container starts instantly and works offline.
RUN python -c "import openwakeword.utils as u; u.download_models()"

COPY server.py .

EXPOSE 8080
CMD ["python", "server.py"]
