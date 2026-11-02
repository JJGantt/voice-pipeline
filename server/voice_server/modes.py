"""The ESP's listening modes, as app_settings and the control routes set them.

Fly is the ALWAYS-ON host for the ESP; the Mac is an intermittent device. So the server reads its own
state (wake-free, mute, inject, the brake) straight from app_settings — on boot AND live over realtime
(settings_sync) — instead of waiting for the Mac listener to push it. A server restart or a Mac that's
asleep can no longer strand the ESP in the wrong mode. The Mac's HTTP bridges remain only as a redundant
fast-path; the database is the source of truth."""

import logging
from dataclasses import dataclass

from . import voiceid

log = logging.getLogger("voice")


@dataclass
class Modes:
    # Push-to-talk active anywhere (Hammerspoon dictation) → the ESP ignores ALL audio: the mute is
    # global, not Mac-only. Set by /capture-off|on and by app_settings.voice_muted, which holds it exactly
    # as long as push-to-talk is held.
    suppressed: bool = False
    # Voice inject mode (hub toggle voice_inject_mode): when ON, the Mac mic is the ONLY listener — speech
    # is pasted into the focused Mac window. The ESP must go deaf for it (Mac-only listening).
    inject: bool = False
    # The ESP's own off switch (app_settings.esp_mic_enabled, absent = on). Off, it drops every frame the
    # way inject mode does, so neither its wake word nor wake-free can start a capture.
    esp_off: bool = False
    # Wake-free (speaker-ID) mode, congruent with the Mac: when ON, the ESP captures on speech ENERGY (no
    # wake word) and verifies the utterance is the enrolled voice (ECAPA + the shared voiceprint) before
    # enqueueing. When OFF, wake words. The model + voiceprint load lazily on turn-on (hence the 2 GB box).
    wakefree: bool = False
    ao_braked: bool = False   # governor brake marker (wakefree_autooff_at set) → run the passive voice sensor

    def deaf(self) -> bool:
        """Push-to-talk mute, inject mode (Mac-only), or switched off: every frame is dropped."""
        return self.suppressed or self.inject or self.esp_off


state = Modes()


def truthy(v) -> bool:
    return str(v).strip().lower() not in ("0", "false", "off", "no", "", "none")


def apply_setting(key, value) -> None:
    """Apply one app_settings change to the server's own state (no Mac bridge required)."""
    if key == "esp_mic_enabled":
        state.esp_off = not truthy(value)
        log.info("ESP mic %s (esp_mic_enabled)", "OFF" if state.esp_off else "on")
        return
    if key == "voice_inject_mode":
        state.inject = truthy(value)
        log.info("voice inject mode %s (from app_settings) — ESP %s", "ON" if state.inject else "off",
                 "muted" if state.inject else "live")
        return
    if key == "wakefree_enabled":
        on = truthy(value)
        if on and not state.wakefree:
            state.wakefree = True; voiceid.load_bg()
            log.info("wake-free ON (from app_settings)")
        elif not on and state.wakefree:
            state.wakefree = False
            log.info("wake-free OFF (from app_settings)")
    elif key == "voice_muted":
        state.suppressed = truthy(value)
        log.info("global mute %s (from app_settings)", "ON" if state.suppressed else "off")
    elif key == "wakefree_autooff_at":
        braked = bool(str(value or "").strip())
        if braked and not state.ao_braked:
            log.info("governor brake ON — passive voice sensor armed")
            if not voiceid.ready():
                voiceid.load_bg()   # the sensor needs ECAPA even though wake-free itself is off
        elif not braked and state.ao_braked:
            log.info("governor brake off — sensor idle")
        state.ao_braked = braked
    elif key in ("voiceid_threshold", "voiceid_accept_threshold", "wakefree_pause_s", "voiceprint") and state.wakefree:
        voiceid.load_bg()   # refresh thresholds / voiceprint live
