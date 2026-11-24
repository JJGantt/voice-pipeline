#!/usr/bin/env bash
# Keep Claude Code current on this machine without ever leaving `claude` missing.
#
# Claude Code's own auto-updater is off here (DISABLE_AUTOUPDATER in fly.toml): it rewrites the global
# npm package in place, and /usr/local/bin/claude does not exist while it does, so a session launched in
# that window exits before claude starts (pi-desk: "no claude came up in <session>"). Every boot runs an
# image older than the newest release, so with it on, the first session of a boot started that rewrite
# and the next session launched a few seconds later could land in it.
#
# Here each version installs into a directory of its own on the volume, and the link moves to it in one
# rename: a launch finds the old version or the new one, never neither, and a session already running
# keeps the files it loaded. A version already on the volume is linked at boot without a download.
#
#   update-claude.sh link    at boot, before the host starts: use the newest version on the volume
#   update-claude.sh fetch   in the background: install the newest release if this machine lacks it
set -uo pipefail
shopt -s nullglob
ROOT=/data/claude-code
LINK=/usr/local/bin/claude
mkdir -p "$ROOT"

report() {
  echo "ERROR: [update-claude] $1" >&2
  OPS_MACHINE=fly OPS_SERVICE=fly-desk node -e \
    "require('/app/host/orchestrator/src/ops_report.cjs').report(process.argv[1]).then(() => process.exit(0))" \
    "[update-claude] $1" || true
}
point() { ln -sfn "$ROOT/$1/bin/claude" "$LINK.next" && mv -Tf "$LINK.next" "$LINK"; }
version() { "$1" --version 2>/dev/null | awk '{print $1}'; }
newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; }

case "${1:-}" in
  link)
    rm -rf "$ROOT"/.[0-9]*   # an install a stop interrupted
    have="$(version "$LINK")"
    best=""
    for d in "$ROOT"/*/; do
      v="$(basename "$d")"
      if [ -x "$d/bin/claude" ] && [ "$(version "$d/bin/claude")" = "$v" ]; then
        if [ -z "$best" ] || newer "$v" "$best"; then best="$v"; fi
      else rm -rf "$d"; fi   # a half-finished install
    done
    if [ -n "$best" ] && newer "$best" "$have"; then point "$best" && echo "[update-claude] using $best from the volume (image has $have)"
    elif [ "$best" != "$have" ]; then best=""; fi   # equal: it may be the copy in use, so it stays
    # Nothing runs yet, so every version but the one in use goes.
    for d in "$ROOT"/*/; do [ -d "$d" ] && [ "$(basename "$d")" != "$best" ] && rm -rf "$d"; done
    ;;
  fetch)
    want="$(timeout 30 npm view @anthropic-ai/claude-code version 2>/dev/null)"
    [ -n "$want" ] || { report "npm view found no Claude Code version; staying on $(version "$LINK")"; exit 0; }
    have="$(version "$LINK")"
    newer "$want" "$have" || exit 0
    tmp="$ROOT/.$want.$$"
    rm -rf "$tmp"
    if timeout 300 npm install -g --prefix "$tmp" --no-audit --no-fund "@anthropic-ai/claude-code@$want" >/dev/null 2>&1 \
       && [ "$(version "$tmp/bin/claude")" = "$want" ]; then
      rm -rf "${ROOT:?}/$want" && mv "$tmp" "$ROOT/$want" && point "$want" && echo "[update-claude] $have -> $want"
    else
      rm -rf "$tmp"
      report "installing Claude Code $want failed; staying on $have"
    fi
    ;;
  *) echo "usage: $0 link|fetch" >&2; exit 2 ;;
esac
exit 0
