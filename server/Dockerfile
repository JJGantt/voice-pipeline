# jared-voice: ESP wake-word + transcription server AND the fly fallback worker, in one always-on
# machine (the worker defers to Mac/Pi and only runs when both are down, so it shares this box for free).
FROM node:20-bookworm-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# python (server + worker + openWakeWord) and libgomp for onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*

# Claude CLI (the Agent SDK drives it for the fallback path) + python deps for both processes
# CLAUDE_CODE_VERSION is set by assemble.sh to the newest release, so this layer rebuilds whenever one
# ships and a fresh machine boots current. Unparameterised, the builder reused a cached layer and Fly
# booted months behind the Pi and the Mac. entrypoint.sh still updates a long-running machine daily.
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
COPY requirements.txt .
# `mcp<2`: govee_mcp.py imports mcp.server.fastmcp, which 2.x renamed — an unpinned rebuild leaves the
# fallback with no lights tool and no error anywhere but the MCP server's stderr.
# Install everything in ONE resolution so speechbrain can't re-pull a CUDA torch. The +cpu local
# version on the pytorch CPU index sorts higher than PyPI's CUDA build, so pip picks the CPU wheel.
RUN pip3 install --no-cache-dir --break-system-packages \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      torch -r requirements.txt claude-agent-sdk 'mcp<2' python-dotenv
# pre-download the wake-word + ECAPA speaker models so the container starts instantly / offline
RUN python3 -c "import openwakeword.utils as u; u.download_models()"
RUN python3 -c "from speechbrain.inference.speaker import EncoderClassifier as E; E.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', savedir='/app/.ecapa')"

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

# govee MCP (cloud light control for the fallback) + the inventory sync job
COPY govee-mcp /app/govee-mcp

# app code
WORKDIR /app
COPY server.py worker.py entrypoint.sh ./

ENV DATA_MCP_JS=/app/data-layer/dist/mcp.js \
    VOICE_MCP_JS=/app/mcp-voice/src/index.js \
    GOVEE_PY=python3 \
    GOVEE_MCP_PY=/app/govee-mcp/govee_mcp.py \
    WORKER_ID=fly \
    WORKER_PRIORITY=mac,pi,fly

EXPOSE 8080
CMD ["./entrypoint.sh"]
