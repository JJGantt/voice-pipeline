#!/usr/bin/env bash
# Assemble the jared-voice build context (transcriber + Govee inventory sync), then `flyctl deploy`.
# The voice worker deploys separately: ../fly-worker/deploy.sh.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data-layer mcp-voice govee-mcp worker.py   # data-layer, mcp-voice and worker.py are left over from when the worker lived here
# The checkouts live at different paths on the Mac (~/repos/...) and the Pi (~/data-layer, ~/mcp-govee).
# Take the first that is a git checkout: the Pi also has a stale, non-git ~/repos/govee-mcp.
pick() { for d in "$@"; do [ -d "$d/.git" ] && { echo "$d"; return; }; done; echo "no checkout among: $*" >&2; exit 1; }
GV=$(pick ~/repos/govee-mcp ~/mcp-govee)
mkdir -p govee-mcp
cp $GV/sync_devices.py govee-mcp/
echo "assembled — now: flyctl deploy --now -a jared-voice"
