# jared-voice-worker: the voice worker, alone on its own machine. It used to share jared-voice with the
# wake-word/transcription server, so every deploy of either restarted both and they shared one CPU.
FROM node:20-bookworm-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv ca-certificates iptables && rm -rf /var/lib/apt/lists/*

# Tailscale, the only route from here to the Mac's iMessage server (entrypoint.sh brings it up when the
# TAILSCALE_AUTHKEY secret is set). Kernel networking: Fly machines have /dev/net/tun, and a userspace
# tailnet would need every MCP client to go through its proxy.
COPY --from=docker.io/tailscale/tailscale:v1.104.2 /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/

# CLAUDE_CODE_VERSION is set by deploy.sh to the newest release, so this layer rebuilds whenever one
# ships and a fresh machine boots current. entrypoint.sh updates a long-running machine daily.
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# `mcp<2`: govee_mcp.py imports mcp.server.fastmcp, which 2.x removed — an unpinned rebuild leaves the
# worker with no lights tool and no error anywhere but the server's stderr.
RUN pip3 install --no-cache-dir --break-system-packages \
      httpx==0.27.2 realtime==2.31.0 claude-agent-sdk 'mcp<2' python-dotenv

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

COPY govee-mcp /app/govee-mcp

# Gmail MCP, in a venv of its own: it is written against mcp 2.x, which the govee server above cannot
# run on. The credential is three Fly secrets (GOOGLE_CLIENT_ID / _SECRET / _REFRESH_TOKEN), passed to it
# by write_mcp_config — the same refresh token the Mac and the Pi hold in gmail-mcp/.env.
RUN python3 -m venv /opt/gmail-venv && /opt/gmail-venv/bin/pip install --no-cache-dir \
      'mcp==2.1.1' google-api-python-client==2.200.0 google-auth==2.57.0 requests python-dotenv
COPY gmail-mcp /app/gmail-mcp

WORKDIR /app
COPY worker.py entrypoint.sh ./

# /data is the volume: transcripts (CLAUDE_CONFIG_DIR), the worker's log and its state survive restarts.
ENV DATA_MCP_JS=/app/data-layer/dist/mcp.js \
    VOICE_MCP_JS=/app/mcp-voice/src/index.js \
    GOVEE_PY=python3 \
    GOVEE_MCP_PY=/app/govee-mcp/govee_mcp.py \
    GMAIL_PY=/opt/gmail-venv/bin/python \
    GMAIL_MCP_PY=/app/gmail-mcp/gmail_mcp.py \
    CLAUDE_CONFIG_DIR=/data/claude \
    STATE_DIR=/data/voice-worker \
    WORKER_ID=fly \
    WORKER_PRIORITY=fly,pi,mac

CMD ["./entrypoint.sh"]
