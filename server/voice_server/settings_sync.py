"""The realtime subscription that keeps this server's state current: app_settings → modes,
voice_presence → the LED's 'another mic hears the speaker', voice_requests → the LED's 'working'.

A Supabase realtime socket can go HALF-OPEN (is_connected stays True while no events flow), which
silently stranded the ESP mute for days. So the listener never trusts the socket alone — it re-reads the
flags on a slow backstop poll AND recycles the socket on a timer."""

import asyncio
import logging
import time

from . import config, db, led, modes

log = logging.getLogger("voice")

SETTINGS_POLL_S = 60.0          # backstop re-read of the flags (catches a change realtime silently dropped);
                                # realtime applies changes instantly via _on_setting, so this is only the
                                # rare-miss net, and SETTINGS_SOCKET_MAX_S recycles a half-open socket independently
SETTINGS_SOCKET_MAX_S = 300.0   # recycle the realtime socket this often so a half-open one can't stay deaf
_MODE_KEYS = ["wakefree_enabled", "voice_muted", "voice_inject_mode", "wakefree_autooff_at", "esp_mic_enabled"]


def _on_setting(payload) -> None:
    try:
        rec = (payload.get("data") or {}).get("record") or {}
        modes.apply_setting(rec.get("key"), rec.get("value"))
    except Exception as e:  # noqa: BLE001
        log.error("settings apply failed: %s", e)


def _reconcile(s: dict) -> None:
    """Apply anything in a fresh read of the flags that drifted from the in-memory state — catches (and
    logs) a change realtime silently dropped, so a half-open socket can't leave the ESP mute / inject /
    wake-free stuck on a stale value."""
    st = modes.state
    for k, cur in (("voice_muted", st.suppressed), ("voice_inject_mode", st.inject),
                   ("wakefree_enabled", st.wakefree), ("esp_mic_enabled", not st.esp_off)):
        if k in s and modes.truthy(s[k]) != cur:
            log.warning("settings backstop: realtime missed %s → applying from poll", k)
            modes.apply_setting(k, s[k])
    if ("wakefree_autooff_at" in s
            and bool(str(s["wakefree_autooff_at"] or "").strip()) != st.ao_braked):
        log.warning("settings backstop: realtime missed wakefree_autooff_at → applying from poll")
        modes.apply_setting("wakefree_autooff_at", s["wakefree_autooff_at"])


async def listen() -> None:
    """Restore state from app_settings on boot, then react to hub changes live over realtime — so the
    ESP's mode survives server restarts and hub toggles even with the Mac off."""
    if not db.configured():
        log.warning("settings listener: no supabase creds; ESP state will rely on the Mac bridge only")
        return
    s = db.get_settings(_MODE_KEYS)
    for k in ("voice_muted", "wakefree_enabled", "voice_inject_mode", "wakefree_autooff_at", "esp_mic_enabled"):
        if k in s:
            modes.apply_setting(k, s[k])
    st = modes.state
    log.info("startup state from DB: wakefree=%s muted=%s inject=%s", st.wakefree, st.suppressed, st.inject)
    from realtime import AsyncRealtimeClient
    while True:
        sock = None
        try:
            sock = AsyncRealtimeClient(f"{config.SUPABASE_URL}/realtime/v1", config.SUPABASE_SERVICE_KEY,
                                       auto_reconnect=True)
            await sock.connect()
            ch = sock.channel("jared-voice-settings")
            ch.on_postgres_changes("UPDATE", callback=_on_setting, schema="public", table="app_settings")
            ch.on_postgres_changes("UPDATE", callback=led.on_presence, schema="public", table="voice_presence")
            ch.on_postgres_changes("INSERT", callback=led.on_request, schema="public", table="voice_requests")
            ch.on_postgres_changes("UPDATE", callback=led.on_request, schema="public", table="voice_requests")
            await ch.subscribe()
            log.info("settings listener subscribed (app_settings + voice_presence + voice_requests realtime)")
            started = time.monotonic()
            while sock.is_connected and (time.monotonic() - started) < SETTINGS_SOCKET_MAX_S:
                await asyncio.sleep(SETTINGS_POLL_S)
                try:
                    _reconcile(db.get_settings(_MODE_KEYS))
                except Exception as e:  # noqa: BLE001
                    log.warning("settings backstop poll failed: %s", e)
            log.info("settings listener recycling realtime socket (connected=%s)", sock.is_connected)
        except Exception as e:  # noqa: BLE001
            log.warning("settings listener error: %s; retry in 5s", e)
        try:
            if sock:
                await sock.close()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(5.0)
