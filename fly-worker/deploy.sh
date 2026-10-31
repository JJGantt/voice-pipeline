#!/usr/bin/env bash
# Assemble the worker's build context from the checkouts and deploy it to jared-voice-worker, with the
# newest Claude Code baked in. The assembled copies are build inputs only (.gitignore); edit the sources.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data-layer mcp-voice govee-mcp gmail-mcp worker.py ops_report.py
pick() { for d in "$@"; do [ -d "$d/.git" ] && { echo "$d"; return; }; done; echo "no checkout among: $*" >&2; exit 1; }
DL=$(pick ~/repos/data-layer ~/data-layer)
GV=$(pick ~/repos/govee-mcp ~/mcp-govee)
GM=$(pick ~/repos/gmail-mcp)
VP=$(cd .. && pwd)
mkdir -p data-layer mcp-voice govee-mcp gmail-mcp
cp $DL/package.json $DL/tsconfig.json data-layer/
cp -R $DL/src data-layer/src
cp $VP/mcp-voice/package.json $VP/mcp-voice/package-lock.json mcp-voice/ 2>/dev/null || true
cp -R $VP/mcp-voice/src mcp-voice/src
cp $GV/govee_mcp.py govee-mcp/
cp $GM/gmail_mcp.py gmail-mcp/   # the script only: its .env holds the credential, which Fly keeps as secrets
cp $VP/worker/worker.py worker.py
cp $VP/worker/ops_report.py ops_report.py   # health/responder reporter, which worker.py imports
flyctl deploy --now -a jared-voice-worker --build-arg CLAUDE_CODE_VERSION="$(npm view @anthropic-ai/claude-code version)" "$@"
