#!/usr/bin/env bash
# Flash the ESP with src/config.h's DEVICE_TOKEN and wait for jared-voice to accept it.
#
# jared-voice always requires VOICE_DEVICE_TOKEN (it will not start without it), so an ESP running
# firmware without the token cannot connect at all until this has been run. The Fly secret must equal
# config.h's DEVICE_TOKEN; this script sets it to that before flashing. Run with the ESP plugged into
# this Mac over USB.
set -euo pipefail
cd "$(dirname "$0")/.."
APP=jared-voice

tok=$(sed -n 's/^#define DEVICE_TOKEN "\([0-9a-f]*\)".*/\1/p' src/config.h)
[ "${#tok}" -ge 32 ] || { echo "src/config.h has no hex DEVICE_TOKEN (see config.h.example)"; exit 1; }
[ -n "${FLY_API_TOKEN:-}" ] || export FLY_API_TOKEN="$(awk -F': ' '/^access_token:/{print $2}' ~/.fly/config.yml)"

# Log lines since a moment, ANSI colour stripped (each line starts with its UTC timestamp).
since() { fly logs -a "$APP" --no-tail 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk -v t="$1" '$1 >= t'; }

printf 'VOICE_DEVICE_TOKEN=%s\n' "$tok" | fly secrets import -a "$APP" >/dev/null   # restarts the server if it changed

t_flash=$(date -u +%Y-%m-%dT%H:%M:%SZ)
~/.platformio/penv/bin/pio run -t upload

echo "waiting for the ESP to reconnect with its token..."
for _ in $(seq 1 40); do
  recent=$(since "$t_flash")
  if grep -q "device token presented" <<<"$recent"; then echo "OK: the ESP is connected with its token"; exit 0; fi
  sleep 3
done
if grep -q "rejected unauthenticated" <<<"$(since "$t_flash")"; then
  echo "FAILED: the server rejected the ESP's token (config.h and the Fly secret disagree?)"; exit 1
fi
echo "UNKNOWN: no connection seen; check: fly logs -a $APP"; exit 1
