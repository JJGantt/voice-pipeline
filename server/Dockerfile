# jared-voice: ESP wake-word + transcription server AND the fly fallback worker, in one always-on
# machine (the worker defers to Mac/Pi and only runs when both are down, so it shares this box for free).
FROM node:20-bookworm-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# python (server + worker + openWakeWord) and libgomp for onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*

# Claude CLI (the Agent SDK drives it for the fallback path) + python deps for both processes
RUN npm install -g @anthropic-ai/claude-code
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt claude-agent-sdk
# pre-download the wake-word models so the container starts instantly / offline
RUN python3 -c "import openwakeword.utils as u; u.download_models()"

# data MCP — build the TypeScript to dist/mcp.js
WORKDIR /app/data-layer
COPY data-layer/package.json data-layer/tsconfig.json ./
RUN npm install --no-audit --no-fund
COPY data-layer/src ./src
RUN npm run build

# voice MCP — plain JS + deps
WORKDIR /app/mcp-voice
COPY mcp-voice/package.json mcp-voice/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY mcp-voice/src ./src

# app code
WORKDIR /app
COPY server.py worker.py entrypoint.sh ./

ENV DATA_MCP_JS=/app/data-layer/dist/mcp.js \
    VOICE_MCP_JS=/app/mcp-voice/src/index.js \
    WORKER_ID=fly \
    WORKER_PRIORITY=mac,pi,fly

EXPOSE 8080
CMD ["./entrypoint.sh"]
