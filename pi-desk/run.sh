#!/usr/bin/env bash
# Launch the Pi desk-command consumer.
#
# Borrows the voice worker's .env for SUPABASE_KEY and CLAUDE_CODE_OAUTH_TOKEN rather than keeping a
# second copy of the same secrets on the same machine. ANTHROPIC_API_KEY may well be in there too —
# that is fine here, because launch.sh strips it from each session's environment, which is the only
# place it would do harm.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ../worker/.env ] && { set -a; . ../worker/.env; set +a; }
[ -f .env ] && { set -a; . ./.env; set +a; }
exec node index.js
