#!/usr/bin/env bash
# Start one Codex session — launch.sh's counterpart for `desk_sessions.agent = 'codex'`. Run inside tmux
# by index.js on the Pi and Fly, and in a Terminal tab by the Mac's orchestrator; never called by hand.
#
#   launch-codex.sh <repo-dir> <repo-name> <prompt-file> [resume-id] [session-id] [cgroup-dir]
#
# The prompt arrives as a FILE for the same reason as in launch.sh: dictated speech is not shell syntax.
set -uo pipefail
DIR="$1"; NAME="$2"; PROMPT_FILE="${3:-}"; RESUME_ID="${4:-}"; SESSION_ID="${5:-}"; CGROUP="${6:-}"
HERE="$(dirname "$0")"

# The session's memory cgroup, before codex starts (pi-desk/cgroups.js), so it and everything it runs are
# charged to this session and killed with it.
[ -n "$CGROUP" ] && { echo $$ > "$CGROUP/cgroup.procs"; } 2>/dev/null

# The data MCP reads both: HUB_SESSION_ID names the session (a report it publishes, end_session on
# itself), and DATA_AGENT=codex makes a session it dispatches a Codex session too — it is running on
# Codex because Claude could not take the work. Codex starts MCP servers with a scrubbed environment, so
# they are forwarded by name below (`env_vars`).
[ -n "$SESSION_ID" ] && export HUB_SESSION_ID="$SESSION_ID"
export DATA_AGENT=codex

cd "$DIR" || exit 1

# Neither Claude credential belongs to a Codex session, and the API key must not reach anything it runs.
unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN

# The kernel's preferred victim ahead of the wall display, as in launch.sh.
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true

# What a Claude session gets as --append-system-prompt, as Codex developer instructions. `-c` parses its
# value as TOML, and a JSON string is a valid TOML basic string, so node encodes it.
# The Mac's orchestrator launches its Codex sessions through this script too (DESK_MACHINE=mac); a Mac
# session gets no machine note, as a Claude one there gets none.
case "${DESK_MACHINE:-pi}" in
  fly) SYS_TEXT="$(cat "$HERE/fly-session.txt")" ;;
  mac) SYS_TEXT="" ;;
  *)   SYS_TEXT="$(cat "$HERE/pi-session.txt")" ;;
esac
PROMPT=""
if [ -n "$PROMPT_FILE" ] && [ -s "$PROMPT_FILE" ]; then
  PROMPT="$(cat "$PROMPT_FILE")"
  rm -f "$PROMPT_FILE"
  [ -z "$RESUME_ID" ] && SYS_TEXT="$(cat "$HERE/../orchestrator/dispatched-session.txt")"${SYS_TEXT:+$'\n\n'"$SYS_TEXT"}
fi
DEV="$(printf '%s' "$SYS_TEXT" | node -e 'process.stdout.write(JSON.stringify(require("fs").readFileSync(0,"utf8")))')"

# Full access with no approvals, as Codex is run by hand: nobody is at this terminal to answer one.
# MCP tools need their own approval mode, or approval_policy=never refuses every call. The update check
# is off because its prompt would sit on the screen waiting for a keypress.
OPTS=(-a never -s danger-full-access -C "$DIR"
      -c check_for_update_on_startup=false
      -c 'mcp_servers.data.env_vars=["HUB_SESSION_ID","DATA_AGENT"]'
      -c 'mcp_servers.data.default_tools_approval_mode="approve"')
[ -n "$SYS_TEXT" ] && OPTS+=(-c "developer_instructions=$DEV")
[ -n "${PI_DESK_CODEX_MODEL:-}" ] && OPTS+=(-m "$PI_DESK_CODEX_MODEL")

if [ -n "$RESUME_ID" ]; then
  [ -n "$PROMPT" ] && exec codex resume "${OPTS[@]}" "$RESUME_ID" "$PROMPT"
  exec codex resume "${OPTS[@]}" "$RESUME_ID"
fi
[ -n "$PROMPT" ] && exec codex "${OPTS[@]}" "$PROMPT"
exec codex "${OPTS[@]}"
