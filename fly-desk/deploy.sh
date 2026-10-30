#!/usr/bin/env bash
# Assemble the build context from the checkouts and deploy jared-desk-fly, with the newest Claude Code
# baked in. The copies are build inputs only (.gitignore); edit the sources.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data-layer govee-mcp gmail-mcp host
pick() { for d in "$@"; do [ -d "$d/.git" ] && { echo "$d"; return; }; done; echo "no checkout among: $*" >&2; exit 1; }
DL=$(pick ~/repos/data-layer ~/data-layer)
GV=$(pick ~/repos/govee-mcp ~/mcp-govee)
GM=$(pick ~/repos/gmail-mcp)
VP=$(cd .. && pwd)
mkdir -p data-layer govee-mcp gmail-mcp host/orchestrator
cp $DL/package.json $DL/tsconfig.json data-layer/
cp -R $DL/src data-layer/src
cp $GV/govee_mcp.py govee-mcp/
cp $GM/gmail_mcp.py gmail-mcp/
cp -R $VP/pi-desk host/pi-desk
cp -R $VP/orchestrator/src host/orchestrator/src
cp $VP/orchestrator/dispatched-session.txt host/orchestrator/
flyctl deploy --now -a jared-desk-fly --build-arg CLAUDE_CODE_VERSION="$(npm view @anthropic-ai/claude-code version)" "$@"
