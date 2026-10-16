#!/usr/bin/env bash
set -e
mkdir -p /data/claude /data/voice-worker
# Everything this machine prints also lands in /data/worker.log, which survives restarts; `fly logs`
# keeps only the last 100 lines.
exec > >(tee -a /data/worker.log) 2>&1

# CLAUDE CODE STAYS CURRENT on a machine that runs for weeks between deploys. Each version goes into its
# own prefix and the `claude` link moves only after the new one has run, so an install that dies half
# way (it took the Pi's CLI down on 2026-10-15) leaves the old one in place.
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
# Daily, with the history on /data kept bounded: the worker log to its last 50,000 lines and Claude's
# run transcripts to 30 days.
( set +e; while :; do
    sleep 86400
    update_claude
    tail -n 50000 /data/worker.log > /data/worker.log.tmp && mv /data/worker.log.tmp /data/worker.log
    find /data/claude/projects -type f -mtime +30 -delete 2>/dev/null
  done ) &

# The worker is restarted whenever it exits — a crash or an OOM-kill — and the machine's whole job is
# this process, so the loop is the foreground. Fly's stop signal reaches this script, not the worker,
# so it is passed on: the worker announces it is stopping and others stop deferring to it.
set +e
stopping=0
trap 'stopping=1; kill -TERM "$pid" 2>/dev/null' TERM INT
while :; do
  python3 worker.py & pid=$!
  wait "$pid"
  if [ "$stopping" = 1 ]; then wait "$pid" 2>/dev/null; exit 0; fi
  echo "worker exited; restarting in 5s"
  sleep 5   # keeps a crash at startup from busy-looping
done
