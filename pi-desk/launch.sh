#!/usr/bin/env bash
# Start one Claude session. Run inside tmux by index.js; never called by hand.
#
#   launch.sh <repo-dir> <repo-name> <prompt-file> [resume-id] [session-id] [cgroup-dir] [incident-launcher]
#
# The prompt arrives as a FILE, not an argument. It is dictated speech — arbitrary text with quotes,
# apostrophes and newlines in it — and threading that through tmux's command string is a quoting
# problem with no safe answer. Reading it here means it is never shell syntax at any point.
set -uo pipefail
DIR="$1"; NAME="$2"; PROMPT_FILE="${3:-}"; RESUME_ID="${4:-}"; SESSION_ID="${5:-}"; CGROUP="${6:-}"; INCIDENT="${7:-}"
HERE="$(dirname "$0")"

# Into the session's own memory cgroup (pi-desk/cgroups.js) BEFORE claude starts, so claude, its tool
# servers and every command it runs are charged to this session's budget and killed with it on close.
[ -n "$CGROUP" ] && { echo $$ > "$CGROUP/cgroup.procs"; } 2>/dev/null

# Every command runs through tool.sh, which puts it ahead of claude in any out-of-memory kill.
export CLAUDE_CODE_SHELL_PREFIX="$HERE/tool.sh"

# The session's own id, so a report it publishes can say which session wrote it. The data MCP
# runs as a child of this process and inherits the environment, which is the only channel there
# is — nothing inside Claude Code knows the name the launcher gave it.
[ -n "$SESSION_ID" ] && export HUB_SESSION_ID="$SESSION_ID"

cd "$DIR" || exit 1

# Neither env credential may reach a session. There is a precedence chain, and each link shadows the
# one below it: ANTHROPIC_API_KEY > CLAUDE_CODE_OAUTH_TOKEN > the real login in ~/.claude/.credentials.json.
#
#   - ANTHROPIC_API_KEY silently bills the API instead of the subscription (worker.py strips it too;
#     verified by poisoning it — Claude broke even with a valid OAuth token beside it).
#   - CLAUDE_CODE_OAUTH_TOKEN is a headless/CI credential. It authenticates API calls fine, so a
#     session looks healthy, but it does NOT carry the claude.ai account linkage — so
#     `--remote-control` never registers and the session never appears in Claude Code. `/status`
#     reports "Session kind: interactive" and nothing else looks wrong, which is what made this
#     take a while to find.
#
# Stripping both leaves the interactive login as the credential, which is the only one that supports
# Remote Control — and, for the same reason, Claude in Chrome. Neither can attach to a credential
# with no claude.ai account behind it. `claude /login` must therefore have been run on this host at
# least once.
#
# Fly (DESK_MACHINE=fly) is the one exception: it has no interactive login, so the year-long token is
# its credential, and its sessions go without Remote Control and Claude in Chrome (below).
unset ANTHROPIC_API_KEY
[ "${DESK_MACHINE:-pi}" = fly ] || unset CLAUDE_CODE_OAUTH_TOKEN

# Model named explicitly rather than left to settings. A session started here runs unattended on a
# machine nobody is watching, so it should not silently inherit whatever the config happens to say —
# the Pi's settings were pinned to sonnet for months without that being anyone's intent.
MODEL="${PI_DESK_MODEL:-opus}"

# Resuming replays the recorded conversation, so an evicted session comes back knowing everything it
# knew. The id was captured at first launch from the transcript filename.
RESUME=()
[ -n "$RESUME_ID" ] && RESUME=(--resume "$RESUME_ID")

# The automation browser (repo: pi-browser) lives on display :99. Two things are needed and both are
# easy to get wrong:
#
#   - DISPLAY=:99, or Chrome integration reports "Status: Disabled" however correct everything else
#     is, and anything Claude Code opens — a permissions page, a login flow — appears on the wall TV
#     on :0 instead, where nobody can click it.
#   - --chrome on every session, not just ones that look browser-shaped. Nothing upstream classifies
#     the request: the voice handler escalates it verbatim and this session decides what to do. Tools
#     load at session start and cannot be acquired later, so a session that becomes a browser task on
#     a remote-control follow-up would otherwise be stuck. Browser tools arrive deferred (names only,
#     schemas fetched on use), so carrying them costs a couple of hundred tokens.
export DISPLAY="${PI_DESK_DISPLAY:-:99}"

# Make this session the kernel's preferred victim, ahead of the wall display.
#
# Chromium stamps its own renderers with oom_score_adj 200-300 — near the top of the kill list — while
# a Claude session sits at the default 0. So under real pressure the kernel takes the TV wall and
# leaves the idle session that caused the pressure, which is backwards and is exactly what happened
# on 2026-09-04: 13 renderer kills in an hour while three sessions held 800MB between them.
#
# 800 outranks Chromium's 300, so a session goes first now. It is set on the shell and inherited
# through `exec`, which matters: raising your OWN score needs no privilege, where lowering anything
# (protecting the wall directly) needs CAP_SYS_RESOURCE. kiosk-health.py does that half with sudo;
# this half has to work without it, because launch.sh must never need root.
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true

# Every session, fresh or resumed, is told what this machine is for. One --append-system-prompt,
# carrying a dispatch's own text too when there is one.
SYS_TEXT="$(cat "$HERE/pi-session.txt")"
# The flags that need the interactive login (Remote Control, the browser) are Pi-only.
EXTRA=(--chrome --remote-control "$NAME")
if [ "${DESK_MACHINE:-pi}" = fly ]; then
  SYS_TEXT="$(cat "$HERE/fly-session.txt")"
  EXTRA=()
fi

# An ops-responder incident session (index.js isIncident) starts through the health repo's launcher,
# which runs claude with that agent's narrow permissions, and never with the browser: it reads text
# anyone can write. health/responder/README.md § The incident agent's permissions.
CLAUDE=(claude)
if [ -n "$INCIDENT" ]; then
  [ -x "$INCIDENT" ] || { echo "launch.sh: no incident launcher at $INCIDENT" >&2; exit 1; }
  CLAUDE=("$INCIDENT")
  EXTRA=()
  [ "${DESK_MACHINE:-pi}" = fly ] || EXTRA=(--remote-control "$NAME")
fi

if [ -n "$PROMPT_FILE" ] && [ -s "$PROMPT_FILE" ]; then
  PROMPT="$(cat "$PROMPT_FILE")"
  rm -f "$PROMPT_FILE"
  # A fresh dispatch is told it IS the session the request was for, so words addressed to the
  # dispatcher ("start a session in X and have it...") are not read as an order to dispatch again.
  # A system prompt, so surfaces still show only the user's words. The Mac's orchestrator reads the same file.
  [ -z "$RESUME_ID" ] && SYS_TEXT="$(cat "$HERE/../orchestrator/dispatched-session.txt")"$'\n\n'"$SYS_TEXT"
  exec "${CLAUDE[@]}" --model "$MODEL" "${EXTRA[@]}" "${RESUME[@]}" --append-system-prompt "$SYS_TEXT" "$PROMPT"
fi
exec "${CLAUDE[@]}" --model "$MODEL" "${EXTRA[@]}" "${RESUME[@]}" --append-system-prompt "$SYS_TEXT"
