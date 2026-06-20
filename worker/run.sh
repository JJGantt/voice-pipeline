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

export SUPABASE_URL="${SUPABASE_URL:-https://felyggqjjhltwokdfhop.supabase.co}"
export WORKER_ID="${WORKER_ID:-mac}"
export WORKER_PRIORITY="${WORKER_PRIORITY:-mac,pi}"
export STATE_DIR="${STATE_DIR:-$HOME/.voice-worker}"
export CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export DATA_MCP_JS="${DATA_MCP_JS:-/Users/jaredgantt/repos/data-layer/dist/mcp.js}"

if [ -z "${SUPABASE_KEY:-}" ]; then
  echo "SUPABASE_KEY is not set. Export the service_role key first." >&2
  exit 2
fi

# Prefer a python3 that has httpx. The voice-pipeline server venv already does;
# fall back to Homebrew python3.11 (also has httpx), then plain python3.
PY="python3"
if [ -x "$HOME/repos/voice-pipeline/server/.venv/bin/python" ]; then
  PY="$HOME/repos/voice-pipeline/server/.venv/bin/python"
elif [ -x /opt/homebrew/bin/python3.11 ]; then
  PY=/opt/homebrew/bin/python3.11
fi

exec "$PY" worker.py "$@"
