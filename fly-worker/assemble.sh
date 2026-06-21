#!/usr/bin/env bash
# Assemble the build context from the source repos, then `flyctl deploy`. The copied trees are gitignored.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data-layer mcp-voice worker
mkdir -p data-layer worker
cp ~/repos/data-layer/package.json ~/repos/data-layer/tsconfig.json data-layer/
cp -R ~/repos/data-layer/src data-layer/src
mkdir -p mcp-voice
cp ~/repos/voice-pipeline/mcp-voice/package.json ~/repos/voice-pipeline/mcp-voice/package-lock.json mcp-voice/ 2>/dev/null || true
cp -R ~/repos/voice-pipeline/mcp-voice/src mcp-voice/src
cp ~/repos/voice-pipeline/worker/worker.py worker/
printf 'claude-agent-sdk\nhttpx\n' > worker/requirements.txt
echo "assembled — now: flyctl deploy --now -a jared-voice-worker"
