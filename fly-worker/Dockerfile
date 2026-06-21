# Fly fallback voice worker. Drains the same voice_requests queue as the Mac/Pi workers, but ALWAYS via
# the Agent SDK on the Anthropic API (WORKER_ID=fly → routing 'sdk'), since Fly has no Claude
# subscription. Lowest priority (mac,pi,fly) → only claims when both Mac and Pi are absent.
FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ca-certificates && rm -rf /var/lib/apt/lists/*

# The Agent SDK drives the Claude Code CLI under the hood; install both it and the python deps.
RUN npm install -g @anthropic-ai/claude-code
RUN pip3 install --no-cache-dir --break-system-packages claude-agent-sdk httpx

# data MCP — build the TypeScript to dist/mcp.js
WORKDIR /app/data-layer
COPY data-layer/package.json data-layer/tsconfig.json ./
RUN npm install --no-audit --no-fund
COPY data-layer/src ./src
RUN npm run build

# voice MCP — plain JS + its deps
WORKDIR /app/mcp-voice
COPY mcp-voice/package.json mcp-voice/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY mcp-voice/src ./src

# the worker itself
WORKDIR /app/worker
COPY worker/worker.py ./

ENV DATA_MCP_JS=/app/data-layer/dist/mcp.js \
    VOICE_MCP_JS=/app/mcp-voice/src/index.js \
    WORKER_ID=fly \
    WORKER_PRIORITY=mac,pi,fly
CMD ["python3", "worker.py"]
