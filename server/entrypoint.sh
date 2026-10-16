#!/usr/bin/env bash
set -e
python3 /app/govee-mcp/sync_devices.py --loop &   # periodic Govee inventory → Supabase (the always-on home)
exec python3 server.py                             # ESP wake-word + transcription (foreground)
