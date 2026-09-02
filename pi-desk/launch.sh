#!/usr/bin/env bash
# Start one Claude session. Run inside tmux by index.js; never called by hand.
#
#   launch.sh <repo-dir> <repo-name> <prompt-file>
#
# The prompt arrives as a FILE, not an argument. It is dictated speech — arbitrary text with quotes,
# apostrophes and newlines in it — and threading that through tmux's command string is a quoting
# problem with no safe answer. Reading it here means it is never shell syntax at any point.
set -uo pipefail
DIR="$1"; NAME="$2"; PROMPT_FILE="${3:-}"; RESUME_ID="${4:-}"; SESSION_ID="${5:-}"

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
unset ANTHROPIC_API_KEY
unset CLAUDE_CODE_OAUTH_TOKEN

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

# Browser preamble, prepended to the dispatched prompt. Two facts a session cannot work out for
# itself and must not stop to ask about, because nobody is watching a dispatched session:
#
#   - WHICH browser. Two Chrome extensions are signed into the account (this Pi's and the Mac's), and
#     a browser call refuses until one is chosen. There is no persistent default — `select_browser`
#     and `/chrome → Select browser…` last only for the session — so the id is injected per launch.
#     Read live from the pairing config rather than hardcoded: a re-pair changes it, and a stale id
#     would send the session to the wrong machine's browser.
#   - CLOSING tabs. Tabs are the session's to clean up; on a 4 GB box with no screen they otherwise
#     accumulate. tidy-tabs.sh is only a cap, not the mechanism.
DEVICE_ID="$(python3 -c 'import json,os,sys; print(json.load(open(os.path.expanduser("~/.claude.json"))).get("chromeExtension",{}).get("pairedDeviceId",""))' 2>/dev/null || true)"
BROWSER_NOTE="[Browser: this machine has its own Chromium on display :99, already signed in to Jared's accounts."
[ -n "$DEVICE_ID" ] && BROWSER_NOTE="$BROWSER_NOTE Two browsers are connected to the account; yours is deviceId $DEVICE_ID — call select_browser with it rather than asking which to use."
BROWSER_NOTE="$BROWSER_NOTE Close every tab you open when you are done with it.]"

if [ -n "$PROMPT_FILE" ] && [ -s "$PROMPT_FILE" ]; then
  PROMPT="$(cat "$PROMPT_FILE")"
  rm -f "$PROMPT_FILE"
  exec claude --model "$MODEL" --chrome "${RESUME[@]}" --remote-control "$NAME" "$BROWSER_NOTE

$PROMPT"
fi
exec claude --model "$MODEL" --chrome "${RESUME[@]}" --remote-control "$NAME"
