#!/usr/bin/env bash
# Assemble the combined jared-voice build context (transcriber + fallback worker), then `flyctl deploy`.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data-layer mcp-voice govee-mcp worker.py
# The checkouts live at different paths on the Mac (~/repos/...) and the Pi (~/data-layer, ~/mcp-govee).
# Take the first that is a git checkout: the Pi also has a stale, non-git ~/repos/govee-mcp.
pick() { for d in "$@"; do [ -d "$d/.git" ] && { echo "$d"; return; }; done; echo "no checkout among: $*" >&2; exit 1; }
DL=$(pick ~/repos/data-layer ~/data-layer)
GV=$(pick ~/repos/govee-mcp ~/mcp-govee)
VP=$(cd .. && pwd)
mkdir -p data-layer
cp $DL/package.json $DL/tsconfig.json data-layer/
cp -R $DL/src data-layer/src
mkdir -p mcp-voice
cp $VP/mcp-voice/package.json $VP/mcp-voice/package-lock.json mcp-voice/ 2>/dev/null || true
cp -R $VP/mcp-voice/src mcp-voice/src
mkdir -p govee-mcp
cp $GV/govee_mcp.py $GV/sync_devices.py govee-mcp/
cp $VP/worker/worker.py worker.py
echo "assembled — now: flyctl deploy --now -a jared-voice"
