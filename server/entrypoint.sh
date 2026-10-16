#!/usr/bin/env bash
set -e
python3 /app/govee-mcp/sync_devices.py --loop &   # periodic Govee inventory → Supabase (the always-on home)
# The fallback worker is the last resort for every command, so it is restarted whenever it exits —
# a crash or an OOM-kill. Unsupervised, it vanished until the next deploy while server.py kept the
# machine looking up. The pause keeps a crash at startup from becoming a busy loop.
( set +e; while :; do python3 worker.py; echo "worker exited ($?); restarting in 5s" >&2; sleep 5; done ) &
exec python3 server.py                             # ESP wake-word + transcription (foreground)
