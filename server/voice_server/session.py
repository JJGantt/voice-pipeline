"""One ESP websocket session: the audio frame loop, the capture state machine (wake word or wake-free
speech energy → endpoint → speaker decision → accept), and handing an accepted capture to transcription
and the lifecycle row. Everything per-capture lives in connection()'s locals; cross-module state lives in
the modules that own it (esp.link, led.state, modes.state, voiceid.state, enroll.state)."""

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import numpy as np

from . import config, enroll, esp, led, lifecycle, modes, presence, voiceid
from .audio import FRAME_SAMPLES, SAMPLE_RATE, audio_quality, rms
from .auth import client_token, token_ok
from .tasks import spawn
from .transcribe import transcribe, transcribe_alt
from .wake import new_wake_model

log = logging.getLogger("voice")

# Endpointing: adaptive to the room's measured noise floor (no hardcoded level).
# The floor is estimated from the ambient audio just before the wake word (TV
# included); speech is whatever rises clearly above it, so it self-calibrates.
AMBIENT_WINDOW_S = 3.0        # rolling window used to estimate the noise floor
FLOOR_MULT = 1.75            # speech only has to rise modestly above the room floor to START a capture —
                            # ECAPA (the voiceprint gate) is the real discriminator, so the energy trigger
                            # should be lenient. Was 3.0 (needed ~3x over a loud TV to even record); 1.4 was an
                            # overcorrection that fired on nearly all ambient TV noise, keeping ECAPA pinned.
                            # 1.75 = your quieter / far-field voice still triggers, with fewer wasted checks.
FLOOR_MIN_MARGIN = 100       # ...and at least this far above floor (guards floor≈0)
MIN_UTTERANCE_S = 0.6        # never endpoint before this (avoids instant cut)
LONG_UTTERANCE_WARN_S = 300.0  # warn if a single capture runs this long

# Wake-free speaker-gated endpointing (replaces energy-vs-noise-floor, which a loud TV corrupts — it
# both clips you mid-sentence and holds the capture open for ages). Keep the capture alive on YOUR
# verified presence: re-score the trailing window with ECAPA off the event loop and close only after
# you've been absent (no confirmed-speaker window) for the hangover. Tune from the logged vad/endpoint lines.
SENSE_EVERY_S = 5.0          # brake-sensor cadence: at most one ECAPA check per this much room speech
VAD_FIRST_CHECK_S = 1.8      # wait for this much audio before the first check
VAD_KEEPALIVE_MIN = 0.14     # once confirmed, KEEP the capture alive until the match falls to clearly-not-you
                             # / silence (~0.14, just above the ≤0.11 room/TV noise ceiling). ABSOLUTE and
                             # DECOUPLED from the confirm bar (voiceid.state.thresh) on purpose: if this tracked
                             # the bar with a margin, lowering voiceid_threshold dragged it UNDER the noise floor and
                             # captures held open forever (0.20 threshold made it 0.04 — a capture ran 78s until
                             # push-to-talk aborted it). Keep this fixed regardless of how low the confirm bar goes.
# Everything that decides a capture's SPAN (the VAD window and cadence, the absent-miss ramp, the no-speaker
# abort, the pre-roll, the back-fill and the whole-clip accept bar) is shared with the Mac's wake-free
# capture, because the two mics must endpoint identically: shared/endpointing.py, copied into this build
# context by assemble.sh. The reasoning and the measurements behind each value are there.
from endpointing import (  # noqa: E402
    VAD_WINDOW_S, VAD_CHECK_EVERY_S, VAD_NO_JARED_ABORT_S, absent_misses as _absent_misses, PREROLL_S,
    HISTORY_S, ACCEPT_MAX_S,
)


def _speaker_gated() -> bool:
    return modes.state.wakefree and voiceid.ready() and enroll.state.left == 0


async def connection(ws):
    peer = getattr(ws, "remote_address", "?")
    # Device-token gate, always on (an unset VOICE_DEVICE_TOKEN never matches, and main() refuses it).
    if not token_ok(client_token(ws), config.VOICE_DEVICE_TOKEN):
        log.warning("rejected unauthenticated/invalid connection: %s", peer)
        await ws.close(code=1008, reason="invalid device token")
        return
    log.info("ESP32 connected: %s (device token presented)", peer)
    esp.link.ws = ws
    esp.back()
    led.state.working = False
    led.state.capturing = False
    led.state.last_sent = None      # the firmware boots with the LED off
    oww = await asyncio.to_thread(new_wake_model)   # this stream's own wake-word state
    gain = config.GAIN
    vid, md = voiceid.state, modes.state

    pending = bytearray()           # raw bytes not yet aligned to a frame
    history = bytearray()           # rolling RAW audio, always kept (capturing or not): the pre-roll is its tail,
                                    # the back-fill reaches further into it
    history_max = int(HISTORY_S * SAMPLE_RATE) * 2
    history_snap = b""              # audio older than the pre-roll, frozen at each trigger for the back-fill
    preroll_max = int(PREROLL_S * SAMPLE_RATE) * 2
    sims_seen: list = []            # every speaker-check score of the current capture (logged once, at its end)
    sense_buf = bytearray()         # brake-sensor rolling window (red mode only; preroll is too short for ECAPA)
    sense_max = int(VAD_WINDOW_S * SAMPLE_RATE) * 2
    sense_task = None               # in-flight sensor ECAPA check
    last_sense = 0.0                # throttle: last sensor check start (monotonic)
    ambient = deque(maxlen=max(1, int(AMBIENT_WINDOW_S * SAMPLE_RATE / FRAME_SAMPLES)))
    capturing = False
    utterance = bytearray()
    woke = False                    # capture trigger fired (either wake word, or speech energy in wake-free)
    by_wake = False                 # the capture in progress was started by a wake word (see `if woke:`)
    threshold = 0.0                 # adaptive speech/silence level, set at wake
    capture_floor = 0.0             # noise floor measured at capture start (for endpoint diagnostics)
    last_voice = 0.0
    started = 0.0
    seed_len_s = 0.0             # seconds of pre-roll prepended at trigger; subtracted from the VAD's audio_s
                                # so a longer pre-roll can never shorten the speaker-verification window
    # Wake-free speaker-gated VAD state (reset at each capture start):
    check_task = None               # in-flight ECAPA check (one at a time, off the event loop)
    last_check = 0.0                # monotonic time the last check was launched
    miss = 0                        # consecutive non-confirming checks since the last confirm
    jared_seen = False              # have we confirmed the speaker at all this capture?
    pending_span = (0, 0)           # byte span of the window the in-flight check is scoring
    conf_lo, conf_hi = 0, 0         # byte span from the first confirming window to the last — the part of
                                    # the capture that is THE SPEAKER, which is what the whole-clip check scores
    mute_flush = False              # mute landed mid-capture on audio worth keeping → close it, don't bin it
    last_confirm = None             # wall-clock of the last confirm (send-suppression vs the other mic)

    async for msg in esp.device_frames(ws, peer):
        if not isinstance(msg, (bytes, bytearray)):
            # Non-audio control text from the device (e.g. "RSSI -67" signal telemetry). Log it so
            # WiFi signal strength is visible in `fly logs`, then keep going — it isn't audio.
            if isinstance(msg, str) and msg.startswith("RSSI"):
                log.info("device %s %s dBm", peer, msg)
            continue
        pending.extend(msg)
        esp.link.last_frame = time.monotonic()   # ESP-mic liveness (feeds the alive beacon)

        while len(pending) >= FRAME_SAMPLES * 2:
            raw = bytes(pending[: FRAME_SAMPLES * 2])
            del pending[: FRAME_SAMPLES * 2]
            frame = np.frombuffer(raw, dtype=np.int16)
            if gain != 1.0:
                # The boost feeds DETECTION ONLY (energy floors + wake word) — the buffered audio (raw)
                # stays un-gained. Gaining the stored audio (the old behavior) clipped loud/close speech
                # to the int16 rail (peak=32767), and a clipped waveform distorts ECAPA embeddings so
                # badly that the speaker scores as a stranger (sim 0.03-0.16 vs thr 0.30). ECAPA and Whisper
                # are level-robust — they need fidelity, not volume; each normalizes at point of use.
                frame = np.clip(frame.astype(np.float32) * gain, -32768, 32767).astype(np.int16)

            if md.deaf():   # push-to-talk mute, inject mode (Mac-only), or switched off
                if capturing:
                    # The mute exists so the ESP doesn't ALSO capture the dictation you're about to
                    # start — that argument covers audio recorded AFTER the mute, and only that.
                    # Whatever was recorded BEFORE it is a separate, already-spoken utterance, so
                    # finalize it through the normal close path instead of binning it. Only a capture
                    # that never earned the right to be sent is still discarded: speaker-gated audio
                    # that never confirmed you (noise/TV), or one too short to transcribe.
                    _sg = _speaker_gated()
                    _dur = len(utterance) / 2 / SAMPLE_RATE
                    if (jared_seen or not _sg) and _dur >= MIN_UTTERANCE_S:
                        log.info("capture FLUSHED at %s (%.1fs, %s) — finalizing, not discarding",
                                 "mute" if md.suppressed else "inject", _dur,
                                 "speaker-confirmed" if _sg else "wake-word")
                        mute_flush = True      # consumed by the capture branch below → close, reason='mute-flush'
                    else:
                        log.info("capture ABORTED mid-utterance (%.1fs in) by %s — %s",
                                 _dur, "mute" if md.suppressed else "inject",
                                 "never confirmed you" if _sg else "too short")
                        if check_task is not None:
                            check_task.cancel()
                            check_task = None
                        capturing = False
                        utterance = bytearray()
                        oww.reset()
                        led.state.capturing = False
                        await led.render(ws, "capture aborted by mute")
                        history.clear()      # nothing recorded around a dictation may be back-filled later
                        continue
                else:
                    history.clear()
                    continue

            history.extend(raw)
            if len(history) > history_max:
                del history[: len(history) - history_max]

            if not capturing:
                ambient.append(rms(frame))      # track the room's noise floor
                floor = float(np.median(ambient)) if ambient else 0.0
                speech_level = max(floor * FLOOR_MULT, floor + FLOOR_MIN_MARGIN)

                if md.wakefree:
                    # No wake word — start on speech energy; the speaker is verified at close.
                    woke = rms(frame) > speech_level
                    fast_score = power_score = 0.0
                else:
                    scores = oww.predict(frame)
                    power_score = scores.get(config.WAKE_MODEL_POWER, 0.0)
                    fast_score = scores.get(config.WAKE_MODEL, 0.0)
                    woke = max(power_score, fast_score) >= config.WAKE_THRESHOLD   # either wake word — same route
                    # Governor brake (red mode): PASSIVE voice sensor. ECAPA stays warm and stamps the
                    # shared heard_jared_at on confirmed voice, so the worker holds the brake while the
                    # conversation continues and re-arms once the room goes quiet. Nothing is captured,
                    # transcribed, or judged here — and the wake words above keep working regardless.
                    if md.ao_braked and voiceid.ready():
                        sense_buf.extend(raw)
                        if len(sense_buf) > sense_max:
                            del sense_buf[: len(sense_buf) - sense_max]
                        _mono = time.monotonic()
                        if (sense_task is None and _mono - last_sense >= SENSE_EVERY_S
                                and len(sense_buf) >= sense_max and rms(frame) > speech_level):
                            last_sense = _mono
                            sense_task = asyncio.create_task(voiceid.check_speaker(
                                np.frombuffer(bytes(sense_buf), dtype=np.int16), _mono))
                        if sense_task is not None and sense_task.done():
                            try:
                                _sim, _ = sense_task.result()
                            except Exception:  # noqa: BLE001
                                _sim = 0.0
                            sense_task = None
                            if _sim >= vid.thresh:
                                spawn(presence.publish(True))
                    elif sense_buf:
                        sense_buf.clear()
                if woke:
                    # A wake word is the speaker addressing the assistant on purpose, so that command is NOT
                    # ambient (voice_requests.always_on): the worker treats it like a button press. Only a
                    # wake-free capture, started on speech energy alone, is room audio.
                    by_wake = not md.wakefree
                    threshold = speech_level
                    capture_floor = floor     # remember the room floor that set this threshold (diagnostics)
                    capturing = True          # wake-free: LED stays as-is until ECAPA confirms it's the enrolled speaker
                                              # (raw energy is not confirmation); wake-word lights below
                    utterance = bytearray(history[-preroll_max:])
                    history_snap = bytes(history[:-preroll_max]) if len(history) > preroll_max else b""
                    seed_len_s = len(utterance) / 2 / SAMPLE_RATE   # pre-roll length — excluded from VAD timing below
                    sims_seen = []
                    started = time.monotonic()
                    started_wall = datetime.now(timezone.utc)   # wall-clock start of the recording window
                    last_voice = started
                    if check_task is not None:
                        check_task.cancel()
                    check_task, last_check, miss, jared_seen, last_confirm = None, 0.0, 0, False, None
                    conf_lo = conf_hi = 0
                    if md.wakefree:
                        log.info("wake-free: speech — capturing…")
                    else:
                        log.info("wake! (fast=%.2f power=%.2f)", fast_score, power_score)
                        # Congruent with wake-free's confirmed state: the wake word IS the confirmation,
                        # so the LED goes solid ("hearing you") the instant it fires — not at capture close.
                        led.state.capturing = True
                        await led.render(ws, "wake word")
            else:
                if not mute_flush:
                    utterance.extend(raw)   # post-mute audio is the dictation — never append it
                now = time.monotonic()
                elapsed = now - started
                hang = vid.pause if md.wakefree else config.SILENCE_HANGOVER_S

                # Speaker-gated endpointing for wake-free (when ECAPA + voiceprint are loaded): keep the
                # capture alive on the speaker's verified presence, not on energy. Falls back to the energy
                # endpoint for wake-word captures (or before the voiceprint loads).
                speaker_gated = _speaker_gated()
                close = False
                reason = ""
                is_noise = False           # True → never confirmed you (drop without enqueue)
                if mute_flush:
                    close, reason, mute_flush = True, "mute-flush", False
                elif speaker_gated:
                    audio_s = len(utterance) / 2 / SAMPLE_RATE - seed_len_s   # POST-trigger audio only — the
                    # pre-roll must NOT count toward VAD_FIRST_CHECK_S / VAD_NO_JARED_ABORT_S (that inflation is
                    # what made the earlier pre-roll abort captures before they could confirm you)
                    # Launch one ECAPA check at a time on the trailing window, OFF the event loop so
                    # frame capture keeps flowing while it runs (~0.4s for a 2.5s window on this box).
                    if check_task is None and audio_s >= VAD_FIRST_CHECK_S and now - last_check >= VAD_CHECK_EVERY_S:
                        win = bytes(utterance[-int(VAD_WINDOW_S * SAMPLE_RATE) * 2:])
                        pending_span = (len(utterance) - len(win), len(utterance))
                        last_check = now
                        check_task = asyncio.create_task(voiceid.check_speaker(np.frombuffer(win, dtype=np.int16), now))
                    if check_task is not None and check_task.done():
                        try:
                            sim, _ = check_task.result()
                        except Exception:
                            sim = 0.0
                        check_task = None
                        thr = min(vid.thresh, VAD_KEEPALIVE_MIN) if jared_seen else vid.thresh
                        sims_seen.append(sim)    # every score is reported once, on the capture's closing line
                        if sim >= thr:
                            miss = 0
                            conf_hi = pending_span[1]
                            if not jared_seen:
                                jared_seen = True
                                conf_lo = pending_span[0]
                                log.info("wake-free: Jared confirmed (sim=%.2f after %.1fs) — tracking", sim, audio_s)
                                led.state.capturing = True        # LED → solid: confirmed it's YOU
                                await led.render(ws, "confirmed Jared")
                            spawn(presence.publish(True))   # shared 'Hearing the speaker' truth
                            last_confirm = datetime.now(timezone.utc)
                        elif jared_seen:
                            miss += 1
                            log.info("wake-free: vad sim=%.2f<thr=%.2f miss=%d/%d audio_s=%.1f",
                                     sim, thr, miss, _absent_misses(audio_s), audio_s)
                    if elapsed >= config.MAX_UTTERANCE_S:
                        close, reason = True, "max"
                    elif jared_seen and miss >= _absent_misses(audio_s):  # consecutive non-confirms → you've stopped
                        close, reason = True, "absent"
                    elif not jared_seen and audio_s >= VAD_NO_JARED_ABORT_S:  # never you → it's noise
                        close, reason, is_noise = True, "noise-abort", True
                else:
                    if rms(frame) > threshold:
                        last_voice = now
                    if (elapsed >= MIN_UTTERANCE_S and now - last_voice >= hang) or elapsed >= config.MAX_UTTERANCE_S:
                        close, reason = True, ("max" if elapsed >= config.MAX_UTTERANCE_S else "silence")

                if not close:
                    continue

                # ---- close the capture ----
                if check_task is not None:
                    check_task.cancel()
                    check_task = None
                end_wall = datetime.now(timezone.utc)   # wall-clock end of the recording window
                dur = len(utterance) / 2 / SAMPLE_RATE
                _arr = np.frombuffer(bytes(utterance), dtype=np.int16)   # RAW un-gained audio
                # floor/thresh are tracked in the gained (detection) domain — divide by the gain so every
                # number in this line is in the same raw-mic units. peak=32767 here now means the MIC
                # itself clipped (acoustic overload), not the software gain.
                _urms, _floor = rms(_arr), capture_floor / gain
                _snr = 20.0 * math.log10(_urms / _floor) if _urms > 0 and _floor > 0 else 0.0
                log.info("endpoint: dur=%.1fs reason=%s jared_seen=%s floor=%.0f thresh=%.0f utt_rms=%.0f peak=%d "
                         "(raw units) | snr=%.1fdB %s",
                         dur, reason, (jared_seen if speaker_gated else "-"), _floor, threshold / gain,
                         _urms, int(np.abs(_arr).max()) if _arr.size else 0, _snr,
                         audio_quality(_arr, (end_wall - started_wall).total_seconds(), seed_len_s))
                truncated = reason == "max"
                if truncated:
                    log.warning("utterance hit %ds cap while still active — truncated", int(config.MAX_UTTERANCE_S))
                elif dur >= LONG_UTTERANCE_WARN_S:
                    log.warning("long utterance: %.0fs", dur)

                # Enroll mode: this utterance is a voiceprint phrase, not a command — embed it into the
                # bank and skip verification/transcription entirely.
                if enroll.state.left > 0 and md.wakefree:
                    await enroll.sample(_arr, capture_floor / gain, ws)
                    led.state.capturing = False
                    capturing = False
                    utterance = bytearray()
                    oww.reset()
                    continue

                # Speaker decision: the gated path already tracked you live (noise-abort = never you →
                # drop). The energy path (wake words) still verifies the whole utterance once, as before.
                if speaker_gated:
                    if is_noise:
                        log.info("wake-free: never confirmed Jared (%.1fs, sims %s) — dropped", dur,
                                 "/".join(f"{x:.2f}" for x in sims_seen) or "none")
                        led.state.capturing = False
                        await led.render(ws, "noise-abort")
                        capturing = False
                        utterance = bytearray()
                        continue
                    # Send-suppression (shared presence): if the OTHER mic still has you — heard you within
                    # 1.5s and at least as recently as this ESP did — this capture is a PARTIAL. Drop it; that
                    # mic will send the complete one. (Mac is primary: ESP yields ties via the −0.3s margin.)
                    #
                    # EXCEPT on a mute flush. The mute is GLOBAL — it silenced the other mic in the same
                    # instant (the Mac just logs "paused (push-to-talk)" and bins its buffer), so the
                    # "complete one" this would yield to is never coming and the utterance is lost by both.
                    # Yielding is only correct while the other mic is still live to finish the job.
                    other = None if reason == "mute-flush" else await presence.other_heard_jared()
                    if other is not None and last_confirm is not None:
                        _n = datetime.now(timezone.utc)
                        if (_n - other).total_seconds() <= 1.5 and other >= last_confirm - timedelta(seconds=0.3):
                            log.info("wake-free: another mic still has you — suppressing this ESP partial (%.1fs)", dur)
                            led.state.capturing = False
                            await led.render(ws, "partial suppressed")
                            capturing = False
                            utterance = bytearray()
                            continue
                elif md.wakefree:
                    sim = voiceid.verify(_arr)
                    if sim < vid.thresh:
                        log.info("wake-free: not Jared (sim=%.2f) — dropped", sim)
                        led.state.capturing = False
                        await led.render(ws, "not Jared")
                        capturing = False
                        utterance = bytearray()
                        continue
                    log.info("wake-free: Jared (sim=%.2f)", sim)

                # ---- CAPTURE ACCEPTED: the command is committed from this instant. Open its lifecycle
                # row (status 'transcribing' — pi-audio turns the floor lamp amber on this insert) and
                # flip the LED from solid (hearing you) to blink (working on it) NOW, before STT. The
                # indicators answer "did my command go through?", so they start the moment we KNOW
                # we're going to work, not when the worker eventually claims it.
                # Reach back for the start of the sentence the trigger (or a noise-abort restart) missed.
                back = 0
                if speaker_gated and history_snap:
                    back = await asyncio.to_thread(voiceid.backfill, np.frombuffer(history_snap, dtype=np.int16))
                    if back:
                        utterance[:0] = history_snap[-back * 2:]
                        back_s = back / SAMPLE_RATE
                        seed_len_s += back_s
                        started_wall -= timedelta(seconds=back_s)
                        dur += back_s
                        log.info("wake-free: back-filled %.1fs of his voice from before the trigger", back_s)
                # The confirmed span, once, against the accept bar — see ACCEPT_THRESHOLD in endpointing.py.
                # Scored on the stretch from the first confirming window to the last (plus any back-fill, which
                # proved itself window by window) — NOT the whole capture. The capture also holds the
                # pre-confirm lead-in and the trailing windows that closed it, and on this far mic those are the
                # TV: a real 14.9s command whose confirmed windows scored 0.33/0.31/0.27 scored 0.20 as a whole
                # clip because ~5s of TV after the speaker stopped were inside it.
                if speaker_gated:
                    if back:
                        span_lo, span_hi = 0, conf_hi + back * 2
                    else:
                        span_lo, span_hi = conf_lo, conf_hi
                    span = np.frombuffer(bytes(utterance[span_lo:span_hi]), dtype=np.int16)[:int(ACCEPT_MAX_S * SAMPLE_RATE)]
                    whole = await asyncio.to_thread(voiceid.verify, span)
                    windows = "/".join(f"{x:.2f}" for x in sims_seen) or "none"
                    if whole < vid.accept:
                        log.info("wake-free: confirmed span sim=%.2f < %.2f — another voice slipped a 2s window past "
                                 "the gate; dropped (%.1fs, span %.1fs, windows %s)", whole, vid.accept, dur,
                                 len(span) / SAMPLE_RATE, windows)
                        led.state.capturing = False
                        await led.render(ws, "whole clip not Jared")
                        capturing = False
                        utterance = bytearray()
                        oww.reset()
                        continue
                    log.info("wake-free: confirmed span sim=%.2f (span %.1fs of %.1fs, windows %s)",
                             whole, len(span) / SAMPLE_RATE, dur, windows)
                rid = await lifecycle.open_request(started_wall.isoformat(), end_wall.isoformat(), always_on=not by_wake)
                led.state.capturing = False
                led.state.working = True
                await led.render(ws, "capture accepted")
                led.arm_safety(ws)   # backstop in case no done ever arrives
                enqueued = False
                try:
                    second = asyncio.create_task(transcribe_alt(bytes(utterance)))
                    text = await transcribe(bytes(utterance))
                    enqueued = await lifecycle.handle_transcript(text, dur, truncated,
                                                                 started_wall.isoformat(), end_wall.isoformat(),
                                                                 rid=rid, alt=await second, always_on=not by_wake)
                except Exception as e:
                    log.exception("transcription failed: %s", e)
                    spawn(lifecycle.notify_transcribe_failure())
                if not enqueued:
                    # Nothing intelligible / enqueue failed → close the lifecycle row (lamp off) and idle the LED.
                    if rid:
                        spawn(lifecycle.close_request_error(rid, "empty or failed transcription"))
                    led.state.working = False
                    led.state.capturing = False
                    await led.render(ws, "nothing to run")
                capturing = False
                utterance = bytearray()
                oww.reset()

    if esp.link.ws is ws:
        esp.link.ws = None
        esp.gone()
    log.info("ESP32 disconnected: %s", peer)
