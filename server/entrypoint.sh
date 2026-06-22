#!/usr/bin/env bash
set -e
python3 /app/govee-mcp/sync_devices.py --loop &   # periodic Govee inventory → Supabase (the always-on home)
python3 worker.py &                                # fly fallback worker (defers to mac/pi; ephemeral SDK)
exec python3 server.py                             # ESP wake-word + transcription (foreground)
