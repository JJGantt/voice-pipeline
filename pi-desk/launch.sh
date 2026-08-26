#!/usr/bin/env bash
# Start one Claude session. Run inside tmux by index.js; never called by hand.
#
#   launch.sh <repo-dir> <repo-name> <prompt-file>
#
# The prompt arrives as a FILE, not an argument. It is dictated speech — arbitrary text with quotes,
# apostrophes and newlines in it — and threading that through tmux's command string is a quoting
# problem with no safe answer. Reading it here means it is never shell syntax at any point.
set -uo pipefail
DIR="$1"; NAME="$2"; PROMPT_FILE="${3:-}"

cd "$DIR" || exit 1

# Claude Code prefers ANTHROPIC_API_KEY over CLAUDE_CODE_OAUTH_TOKEN when both are present, which
# would silently bill the API instead of the subscription. Verified by experiment: poisoning the API
# key breaks Claude even with a valid OAuth token beside it. worker.py strips it for the same reason.
unset ANTHROPIC_API_KEY

if [ -n "$PROMPT_FILE" ] && [ -s "$PROMPT_FILE" ]; then
  PROMPT="$(cat "$PROMPT_FILE")"
  rm -f "$PROMPT_FILE"
  exec claude --remote-control "$NAME" "$PROMPT"
fi
exec claude --remote-control "$NAME"
