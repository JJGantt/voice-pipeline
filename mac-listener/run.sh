#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }
export SUPABASE_URL="${SUPABASE_URL:-https://felyggqjjhltwokdfhop.supabase.co}"
exec ./.venv/bin/python listener.py
