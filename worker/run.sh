#!/usr/bin/env bash
# Launch the voice-command worker daemon.
#
# SUPABASE_KEY MUST be provided (the service_role key). Everything else has a
# sensible default that can be overridden by exporting it before running this.
#
# Examples:
#   SUPABASE_KEY=<service-role-key> ./run.sh            # Mac (default)
#   WORKER_ID=pi SUPABASE_KEY=<key> ./run.sh            # Pi
#   ./run.sh --once                                     # single iteration (test)

set -euo pipefail
cd "$(dirname "$0")"

# Local secrets (SUPABASE_KEY etc.) — gitignored.
[ -f .env ] && { set -a; . ./.env; set +a; }

export SUPABASE_URL="${SUPABASE_URL:-https://felyggqjjhltwokdfhop.supabase.co}"
export WORKER_ID="${WORKER_ID:-mac}"
export WORKER_PRIORITY="${WORKER_PRIORITY:-fly,pi,mac}"
export STATE_DIR="${STATE_DIR:-$HOME/.voice-worker}"
export CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export DATA_MCP_JS="${DATA_MCP_JS:-/Users/jaredgantt/repos/data-layer/dist/mcp.js}"

if [ -z "${SUPABASE_KEY:-}" ]; then
  echo "SUPABASE_KEY is not set. Export the service_role key first." >&2
  exit 2
fi

# Prefer a python3 that has the deps. The dedicated worker venv (the Pi uses this) wins; then the
# server venv (Mac); then Homebrew python3.11; then plain python3.
PY="python3"
WVENV="$(cd "$(dirname "$0")/.." && pwd)/.venv-worker/bin/python"
if [ -x "$WVENV" ]; then
  PY="$WVENV"
elif [ -x "$HOME/repos/voice-pipeline/server/.venv/bin/python" ]; then
  PY="$HOME/repos/voice-pipeline/server/.venv/bin/python"
elif [ -x /opt/homebrew/bin/python3.11 ]; then
  PY=/opt/homebrew/bin/python3.11
fi

exec "$PY" worker.py "$@"
