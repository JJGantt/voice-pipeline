#!/usr/bin/env bash
# Flash the ESP with src/config.h's DEVICE_TOKEN, then make jared-voice require it.
#
# Order is the whole point: VOICE_DEVICE_TOKEN on Fly locks out any ESP that does not send it, so the
# secret is set only after the server has logged a connection that presented a token, and removed
# again if the ESP is rejected afterwards. Run with the ESP plugged into this Mac over USB.
set -euo pipefail
cd "$(dirname "$0")/.."
APP=jared-voice

tok=$(sed -n 's/^#define DEVICE_TOKEN "\([0-9a-f]*\)".*/\1/p' src/config.h)
[ "${#tok}" -ge 32 ] || { echo "src/config.h has no hex DEVICE_TOKEN (see config.h.example)"; exit 1; }
[ -n "${FLY_API_TOKEN:-}" ] || export FLY_API_TOKEN="$(awk -F': ' '/^access_token:/{print $2}' ~/.fly/config.yml)"

# Log lines since a moment, ANSI colour stripped (each line starts with its UTC timestamp).
since() { fly logs -a "$APP" --no-tail 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk -v t="$1" '$1 >= t'; }

t_flash=$(date -u +%Y-%m-%dT%H:%M:%SZ)
~/.platformio/penv/bin/pio run -t upload

# Wait for the server to see the flashed ESP present its token.
echo "waiting for the ESP to reconnect with its token..."
for _ in $(seq 1 40); do
  if since "$t_flash" | grep -q "device token presented"; then found=1; break; fi
  sleep 3
done
[ "${found:-}" = 1 ] || { echo "FAILED: no connection presenting a token; VOICE_DEVICE_TOKEN left unset"; exit 1; }

t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf 'VOICE_DEVICE_TOKEN=%s\n' "$tok" | fly secrets import -a "$APP" >/dev/null
echo "VOICE_DEVICE_TOKEN set; waiting for the restarted server to accept the ESP..."
for _ in $(seq 1 40); do
  recent=$(since "$t0")
  if grep -q "rejected unauthenticated" <<<"$recent"; then
    fly secrets unset VOICE_DEVICE_TOKEN -a "$APP" >/dev/null
    echo "FAILED: the server rejected the ESP; VOICE_DEVICE_TOKEN removed again"; exit 1
  fi
  if grep -q "voice backend up" <<<"$recent" && grep -q "device token presented" <<<"$recent"; then
    echo "OK: the ESP is connected and the server now requires its token"; exit 0
  fi
  sleep 3
done
echo "UNKNOWN: no connection seen after the restart; check: fly logs -a $APP"; exit 1
