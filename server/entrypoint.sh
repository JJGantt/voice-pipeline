#!/usr/bin/env bash
set -e
# CLAUDE CODE STAYS CURRENT. The image's copy is whatever `npm install -g` fetched when that layer was
# last built — and the layer is cached, so it froze (2.1.197 while the Pi and the Mac ran 2.1.301), and
# anything installed into a running container is gone at the next restart. So check at boot and daily.
# Each version goes into its own prefix and the `claude` link moves only after the new one has run:
# an install that dies half way (it took the Pi's CLI down on 2026-10-15) leaves the old one in place.
update_claude() {
  local want have dir
  want=$(npm view @anthropic-ai/claude-code version 2>/dev/null) || return
  have=$(claude --version 2>/dev/null | awk '{print $1}')
  [ -n "$want" ] && [ "$want" != "$have" ] || return
  dir=/opt/claude-$want
  if npm install -g --prefix "$dir" "@anthropic-ai/claude-code@$want" >/dev/null 2>&1 \
     && "$dir/bin/claude" --version >/dev/null 2>&1; then
    ln -sfn "$dir/bin/claude" /usr/local/bin/claude.next && mv -T /usr/local/bin/claude.next /usr/local/bin/claude
    find /opt -maxdepth 1 -name 'claude-*' ! -path "$dir" -exec rm -rf {} +
    echo "claude updated $have -> $want" >&2
  else
    rm -rf "$dir"; echo "claude update to $want failed; still on $have" >&2
  fi
}
( set +e; while :; do update_claude; sleep 86400; done ) &
python3 /app/govee-mcp/sync_devices.py --loop &   # periodic Govee inventory → Supabase (the always-on home)
# The fallback worker is the last resort for every command, so it is restarted whenever it exits —
# a crash or an OOM-kill. Unsupervised, it vanished until the next deploy while server.py kept the
# machine looking up. The pause keeps a crash at startup from becoming a busy loop.
( set +e; while :; do python3 worker.py; echo "worker exited ($?); restarting in 5s" >&2; sleep 5; done ) &
exec python3 server.py                             # ESP wake-word + transcription (foreground)
