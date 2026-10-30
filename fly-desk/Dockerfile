# jared-desk-fly: dispatched Claude sessions, when neither the Pi nor the Mac can take one. The session
# host is pi-desk itself, run with DESK_MACHINE=fly.
FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git tmux curl ca-certificates jq python3 python3-pip python3-venv procps less openssh-client \
    && rm -rf /var/lib/apt/lists/*

# gh, from GitHub's own apt repository.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh && rm -rf /var/lib/apt/lists/*

# flyctl and the supabase CLI, as release binaries.
RUN curl -fsSL https://fly.io/install.sh | FLYCTL_INSTALL=/usr/local sh \
    && ln -sf /usr/local/bin/flyctl /usr/local/bin/fly
RUN curl -fsSL https://github.com/supabase/cli/releases/latest/download/supabase_linux_amd64.tar.gz \
       | tar -xz -C /usr/local/bin supabase

ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# MCP servers: data (built), govee (mcp<2, see fly-worker/Dockerfile), gmail (its own venv, mcp 2.x).
RUN pip3 install --no-cache-dir --break-system-packages httpx==0.27.2 'mcp<2' python-dotenv
WORKDIR /app/data-layer
COPY data-layer/package.json data-layer/tsconfig.json ./
RUN npm install --no-audit --no-fund
COPY data-layer/src ./src
RUN npm run build
COPY govee-mcp /app/govee-mcp
RUN python3 -m venv /opt/gmail-venv && /opt/gmail-venv/bin/pip install --no-cache-dir \
      'mcp==2.1.1' google-api-python-client==2.200.0 google-auth==2.57.0 requests python-dotenv
COPY gmail-mcp /app/gmail-mcp

# The session host: pi-desk and the orchestrator modules it shares, at the same relative layout. Only
# the two packages those modules import — the orchestrator's node-pty is for the Mac's terminal.
WORKDIR /app/host
RUN cd /app/host && npm init -y >/dev/null && npm pkg set type=module \
    && npm install --no-audit --no-fund @supabase/supabase-js@2 ws@8
COPY host/ /app/host/
COPY entrypoint.sh /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
