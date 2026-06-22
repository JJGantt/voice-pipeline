#!/usr/bin/env bash
set -e
python3 worker.py &        # fly fallback worker (defers to mac/pi; ephemeral SDK)
exec python3 server.py     # ESP wake-word + transcription (foreground)
