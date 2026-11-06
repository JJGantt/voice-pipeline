#!/usr/bin/env bash
# Assemble the worker's build context from the checkouts and deploy it to jared-voice-worker, with the
# newest Claude Code baked in. The assembled copies are build inputs only (.gitignore); edit the sources.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data-layer mcp-voice govee-mcp gmail-mcp worker.py voice_worker requirements.txt
pick() { for d in "$@"; do [ -d "$d/.git" ] && { echo "$d"; return; }; done; echo "no checkout among: $*" >&2; exit 1; }
DL=${DATA_LAYER_DIR:-$(pick ~/repos/data-layer ~/data-layer)}
# The image carries this checkout's src/, so a stale one silently ships old tools to every voice turn.
git -C "$DL" fetch -q origin && [ "$(git -C "$DL" rev-list --count HEAD..origin/main)" = 0 ] \
  || { echo "$DL is behind origin/main; update it or set DATA_LAYER_DIR" >&2; exit 1; }
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
cp -R $VP/worker/voice_worker voice_worker && rm -rf voice_worker/__pycache__
cp $VP/worker/requirements.txt requirements.txt   # the worker's Python packages, ops-report among them
# Build and push first, then swap the machine only while the worker is idle. A deploy stops the worker
# with about 6-8s of grace (Fly follows SIGINT with SIGTERM and entrypoint.sh exits on it), and a turn
# still running then is closed as an orphan and never answered; a build takes minutes, so gating only
# the swap keeps that window to the few seconds the machine update itself takes.
LABEL="deploy-$(date +%s)"
flyctl deploy --build-only --push -a jared-voice-worker --image-label "$LABEL" \
  --build-arg CLAUDE_CODE_VERSION="$(npm view @anthropic-ai/claude-code version)" "$@"
wait_idle() {
  local url="${SUPABASE_URL:-}" key="${SUPABASE_KEY:-}" busy i
  if [ -z "$key" ] && [ -f "$DL/.env" ]; then
    url=$(sed -n 's/^SUPABASE_URL=//p' "$DL/.env"); key=$(sed -n 's/^SUPABASE_KEY=//p' "$DL/.env")
  fi
  if [ -z "$key" ]; then echo "no Supabase key here; deploying without waiting for an idle worker" >&2; return; fi
  for i in $(seq 1 60); do   # 5 minutes at most; a hub turn is well under that
    busy=$(curl -fsS "$url/rest/v1/voice_requests?status=eq.claimed&claimed_by=eq.fly&select=id" \
           -H "apikey: $key" -H "Authorization: Bearer $key") || { echo "idle check failed; deploying anyway" >&2; return; }
    [ "$busy" = "[]" ] && return
    [ "$i" = 1 ] && echo "worker is mid-turn; waiting for it to finish before swapping the machine"
    sleep 5
  done
  echo "worker still busy after 5 minutes; deploying anyway" >&2
}
wait_idle
flyctl deploy --now -a jared-voice-worker --image "registry.fly.io/jared-voice-worker:$LABEL"
