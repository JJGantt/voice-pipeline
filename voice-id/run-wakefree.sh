#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Reuse the listener's secrets (SUPABASE_KEY, GROQ_API_KEY).
ENVF="/Users/jaredgantt/repos/voice-pipeline/mac-listener/.env"
[ -f "$ENVF" ] && { set -a; . "$ENVF"; set +a; }
export SUPABASE_URL="${SUPABASE_URL:-https://felyggqjjhltwokdfhop.supabase.co}"
exec ./.venv/bin/python wakefree.py
