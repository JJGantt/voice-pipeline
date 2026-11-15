#!/usr/bin/env bash
set -e
mkdir -p /data/claude /data/voice-worker /data/codex
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
# The history on /data kept bounded: the worker log to its last 50,000 lines and Claude's run
# transcripts to 7 days, at boot and then daily. Nothing reads old transcripts back; they run about
# 30MB a day, so a 30-day window fills the 1GB volume, and pruning only after 24h of uptime never
# runs on a machine that restarts more often than that.
prune_data() {
  tail -n 50000 /data/worker.log > /data/worker.log.tmp && mv /data/worker.log.tmp /data/worker.log
  find /data/claude/projects -type f -mtime +7 -delete 2>/dev/null
  find /data/claude/projects -mindepth 1 -type d -empty -delete 2>/dev/null
}
( set +e; prune_data; while :; do
    sleep 86400
    update_claude
    prune_data
  done ) &

# TAILSCALE: the route to the Mac's iMessage server, which listens only on the home network and the
# tailnet. State lives on the volume so a restart rejoins as the same node instead of minting a new one.
# Nothing here retries: a tailnet that will not come up costs the iMessage tools until the next restart,
# and every other tool is unaffected.
# MESSAGES_TAILNET_URL (a Fly secret) is the Mac's iMessage server at its tailnet address.
if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  mkdir -p /data/tailscale /var/run/tailscale
  tailscaled --state=/data/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock >/dev/null 2>&1 &
  if tailscale up --auth-key="$TAILSCALE_AUTHKEY" --hostname=jared-voice-worker --accept-dns=false --timeout=30s; then
    export MESSAGES_MCP_URL="$MESSAGES_TAILNET_URL"
    echo "tailscale up; iMessage at $MESSAGES_MCP_URL"
  else
    echo "tailscale did not come up; no iMessage tools this run" >&2
  fi
fi

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
