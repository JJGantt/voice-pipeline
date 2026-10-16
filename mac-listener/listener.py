#!/usr/bin/env python3
"""Always-on Mac voice listener.

openWakeWord runs continuously on the built-in mic with two equivalent wake words ("Hey Rhasspy" /
"Hey Jarvis" — either one wakes capture; routing is the worker's dynamic dispatch). On a wake hit it
captures the following utterance (ending on a trailing silence), transcribes it with Groq
whisper-large-v3-turbo when GROQ_API_KEY is set else local whisper.cpp, strips the wake phrase, and
enqueues the transcript into Supabase `voice_requests` with source='mac' — the exact same queue the ESP/Fly path
feeds. The Mac worker then drains it into the shared 'hub' conversation, identical to every other
front-end. The Mac is just another set of ears. Live state (listening → working → done) is shown
on screen via the Hammerspoon overlay HUD (see hud()).
"""

import asyncio
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone

import numpy as np
import sounddevice as sd
import httpx
from openwakeword.model import Model

# ---- config (env, with sane defaults) -------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")   # service_role or anon — only needs INSERT on voice_requests
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_rhasspy")            # "Hey Rhasspy" — equivalent wake words:
WAKE_MODEL_POWER = os.environ.get("WAKE_MODEL_POWER", "hey_jarvis") # "Hey Jarvis" — either one wakes capture
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
WHISPER_BIN = os.path.expanduser(os.environ.get("WHISPER_BIN", "~/whisper.cpp/build/bin/whisper-cli"))
WHISPER_MODEL = os.path.expanduser(os.environ.get("WHISPER_MODEL", "~/whisper.cpp/models/ggml-medium.en.bin"))
# Transcription: Groq whisper-large-v3-turbo when a key is set (faster + more accurate, same as the
# ESP/web path); otherwise local whisper.cpp (offline, no key). Set GROQ_API_KEY to switch over.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")

SAMPLE_RATE = 16000
FRAME = 1280            # 80 ms @ 16 kHz — openWakeWord's expected chunk
HANGOVER_S = float(os.environ.get("HANGOVER_S", "3.0"))   # trailing silence that ends an utterance
MAX_UTTER_S = 30.0
MIN_UTTER_S = 0.4      # ignore sub-0.4s "utterances" (just the wake tail)

# Mic-ownership state machine. Two things take the mic from the wake-word listener: the GLOBAL mute
# (app_settings.voice_muted — set by ANY explicit input: push-to-talk dictation OR a hub FAB button)
# and wake-free mode (wakefree_enabled → wakefree.py owns the mic). The listener captures iff neither
# holds it. The mute is ONE shared flag so every source (Mac + ESP) goes deaf together; push-to-talk
# also fires instantly via SIGUSR (Hammerspoon) and propagates to the ESP by writing the flag.
WAKEFREE_RUN = os.path.expanduser("~/repos/voice-pipeline/voice-id/run-wakefree.sh")
_muted = threading.Event()      # set = global voice_muted (an explicit command is being captured)
_mic_on = threading.Event()     # clear = THIS Mac's mic is off (app_settings.mac_mic_enabled); ESP unaffected
_mic_on.set()                   # default ON — a missing/unreadable setting must never silently deafen the Mac
_wakefree = threading.Event()   # set = wake-free mode on
_sensor = threading.Event()     # set = governor brake → wakefree.py runs as a PASSIVE voice sensor
_sensor_proc = None             # the sensor child (mic NOT yielded — wake words keep running here)
_capture = threading.Event()    # set = the listener should own the mic (wake-word)
_capture.set()

# CoreAudio liveness watchdog (same design as wakefree.py): the input stream is opened ONCE and never
# stopped — mic yields are a software mute (frames read + discarded). Stopping a CoreAudio stream from
# a paused state is what deadlocked in the HAL mutex on Jun 25 and silently killed the Mac mic for six
# days. Frames arrive at a fixed hardware cadence, so "no frame in >10s while we expect them" can only
# mean the stream is wedged → os.execv(run.sh): replace this process image in place (same PID, no Python
# unwinding — as safe as os._exit for the HAL-mutex concern) so the restart NEVER depends on launchd.
# Relying on launchd KeepAlive is what took the mic down for 24h on Jul 3: this macOS parks the respawn
# of an exited agent ("pended nondemand spawn = inefficient") indefinitely; only a demand spawn
# (launchctl kickstart) is reliable. os._exit(3) remains only as the fallback if execv itself fails —
# the worker's listener supervisor then kickstarts the job. _expect_frames is cleared around
# known-blocking work (Groq transcription) where reads legitimately pause.
_last_frame = time.monotonic()
_expect_frames = threading.Event()
_expect_frames.set()


def _self_reexec(reason: str) -> None:
    """Replace this process image in place (same PID) — a fresh process means a fresh FD table AND a fresh
    CoreAudio HAL client, the one reliable recovery from a wedged audio stack or fd exhaustion, and it never
    depends on launchd (whose KeepAlive respawn this macOS parks indefinitely). os._exit(3) is the last
    resort if execv itself fails; the worker's listener supervisor then kickstarts the job.

    Deliberately does NOT talk to Hammerspoon on the way out. This used to clear the HUD with a
    subprocess.run(..., timeout=2), which SIGKILLs the `hs` client when Hammerspoon is slow to answer.
    Hammerspoon then replies into a dead CFMessagePort, and its error path logs THROUGH that same port —
    ipc_callback → logError → ipc_sendMessage → runloop → the next queued request, nested. During a
    lid-closed wedge storm (~1,400 re-execs overnight on 2026-08-01) it recursed until the stack blew and
    Hammerspoon died with EXC_BREAKPOINT. Startup's hud("off") clears any frozen pill instead — the
    persisted HUD state means that clear still fires exactly once."""
    log(f"self-heal: re-execing in place — {reason}")
    run_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.sh")
    try:
        os.execv(run_sh, [run_sh])   # same PID, fresh process image — launchd never consulted
    except OSError:
        os._exit(3)   # last resort; the worker's listener supervisor kickstarts the job


def _raise_fd_limit() -> None:
    """macOS ships a 256 soft fd limit; a descriptor leak (or just many concurrent sockets/models) reaches
    it fast, and getaddrinfo then fails with a DNS-looking '[Errno 8] nodename nor servname' that masks the
    real cause. Raise the soft limit toward the hard cap so any residual leak takes vastly longer to bite —
    the belt to the self-heal's braces."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(hard, 8192)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log(f"raised fd soft limit {soft} → {target} (hard {hard})")
    except Exception as e:  # noqa: BLE001
        log(f"could not raise fd limit: {e}")


def _lid_closed() -> bool:
    """True when the laptop lid is shut. macOS powers the audio hardware down with the lid, but
    `caffeinate` (the long-running terminal sessions) keeps this process scheduled — so frames stop for
    a legitimate reason while we're still awake to misread it as a wedge. Consulted ONLY when the
    watchdog is about to re-exec, so the healthy path never pays for the subprocess. Unreadable state
    counts as open: self-healing a live stream is recoverable, refusing to self-heal is not."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return False
    return '"AppleClamshellState" = Yes' in out


def _open_input_stream():
    """Open the capture stream, waiting out a closed lid instead of dying into a respawn loop.

    CoreAudio refuses to bind an input stream while the audio hardware is powered down, and the failure
    is an unhandled PortAudioError — so launchd KeepAlive turns every attempt into a fresh Python plus a
    wake-model load. That is the other half of the 2026-08-01 night: 1,436 of them, alongside the
    watchdog's own re-execs. Waiting costs one ioreg every 5s. A PortAudioError with the lid OPEN is a
    real fault and still propagates."""
    waited = False
    while True:
        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME, dtype="int16", channels=1)
        try:
            stream.start()      # where a powered-down HAL actually fails; a second start() in `with` is a no-op
        except sd.PortAudioError as exc:
            stream.close()
            if not _lid_closed():
                raise
            if not waited:
                log(f"lid closed — CoreAudio won't bind an input stream ({exc}); waiting for it to open")
                waited = True
            time.sleep(5)
            continue
        if waited:
            log("lid open — capture stream bound")
        return stream


def _watchdog() -> None:
    """Re-exec on a wedged stream — but never while the lid is shut. A closed lid means no frames AND no
    working HAL to bind a new stream to, so re-execing just wedges the replacement 10s later: that is the
    ~15s loop that ran ~1,400 times overnight on 2026-08-01, burned the night on CPU, and flooded
    Hammerspoon's IPC port until it crashed. Holding costs nothing — the mic is dead either way while the
    lid is down — and the moment it opens, the same >10s test judges the stream, measured from the
    reopening rather than from hours of powered-down silence.

    `baseline` is the watchdog's own reference rather than _last_frame directly: _last_frame stays the
    honest mic-liveness signal that _alive_beacon reports to the hub, so a lid-closed hold never tells
    the hub the Mac mic is alive."""
    lid_shut = False
    baseline = time.monotonic()
    while True:
        time.sleep(5)
        if not _expect_frames.is_set():     # known-blocking work (Groq transcription) — reads legitimately pause
            baseline = time.monotonic()
            continue
        baseline = max(baseline, _last_frame)
        if time.monotonic() - baseline <= 10:
            continue
        if _lid_closed():
            if not lid_shut:
                log("lid closed — audio hardware is down, not a wedged stream; holding until it opens")
                lid_shut = True
            baseline = time.monotonic()     # re-arm, so the wedge test starts over when the lid opens
            continue
        if lid_shut:
            log("lid open — audio hardware back; judging the stream from here")
            lid_shut = False
        _self_reexec("no audio frames for >10s — CoreAudio input stream wedged")


def _alive_beacon() -> None:
    """Mic-liveness beacon: stamp voice_presence.alive_at every 30s WHILE frames are flowing (gated on
    _last_frame, not on the process merely existing). The hub keys the 'Mac mic' indicator on whichever
    process OWNS the mic — mac-wakefree when wake-free is on, mac-listener otherwise."""
    while True:
        if time.monotonic() - _last_frame < 10:
            try:
                httpx.post(f"{SUPABASE_URL}/rest/v1/voice_presence?on_conflict=source",
                           headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                    "Content-Type": "application/json",
                                    "Prefer": "resolution=merge-duplicates,return=minimal"},
                           json={"source": "mac-listener",
                                 "alive_at": datetime.now(timezone.utc).isoformat()}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(30)


def _recompute_capture() -> None:
    (_capture.clear if (_muted.is_set() or _wakefree.is_set() or not _mic_on.is_set()) else _capture.set)()


JARED_VOICE_URL = os.environ.get("JARED_VOICE_URL", "https://jared-voice.fly.dev").rstrip("/")


def _ping_esp_capture(on: bool) -> None:
    """Tell the jared-voice server to mute/unmute the ESP, so the global mute reaches it too."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/capture-{'off' if on else 'on'}", timeout=3)
    except Exception:  # noqa: BLE001
        pass


def _ping_esp_wakefree(on: bool) -> None:
    """Bridge the global wake-free mode to the ESP — it switches to speaker-ID capture (on) or back to
    wake words (off), congruent with the Mac. Generous timeout: the server lazy-loads ECAPA on turn-on."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/wakefree-{'on' if on else 'off'}", timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _apply_mute(on: bool) -> None:
    """Apply the global mute EVERYWHERE: this Mac (listener mic / wakefree) AND the ESP. One explicit
    command in flight = every source goes deaf. The ESP gets it two ways: an instant HTTP ping AND the
    `voice_muted` DB flag (the source of truth the Fly server reads). Driving it off the DB flag means
    the mute is held EXACTLY as long as push-to-talk is active — no arbitrary timeout can wake the ESP
    mid-dictation (that 90s auto-clear was the bug).

    THE MUTE IS HELD BY NAME, not set. This Mac writes `voice_mute_mac`; the hub recomputes
    `voice_muted` from every holder, so a watch or a phone finishing its own recording cannot wake the
    room while a dictation here is still running. That is exactly what happened on 2026-09-23: a watch
    recording ended 0.7s after push-to-talk muted, wrote the old single flag to 0, and wake-free heard
    the rest of the dictation and ran it as a command."""
    if on == _muted.is_set():
        return
    (_muted.set if on else _muted.clear)()
    _recompute_capture()
    if _wakefree.is_set():
        subprocess.Popen(["pkill", "-USR1" if on else "-USR2", "-f", "wakefree.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=_ping_esp_capture, args=(on,), daemon=True).start()
    held = datetime.now(timezone.utc).isoformat() if on else ""
    threading.Thread(target=_write_setting, args=("voice_mute_mac", held), daemon=True).start()


def set_mac_mic(on: bool) -> None:
    """Mac-mic-only kill switch (app_settings.mac_mic_enabled). Silences THIS Mac — the wake-word loop
    and wakefree.py — while the ESP stays fully live, so the puck can be exercised in isolation.

    Deliberately NOT voice_muted: that flag is global and deafens every source, and push-to-talk owns
    it. This one is per-device and leaves that plumbing untouched, so dictating still mutes the ESP
    (otherwise the puck would hear and act on everything you dictate)."""
    if on == _mic_on.is_set():
        return
    # Cleared FIRST so the supervisor sees the new state and won't respawn the kill below.
    (_mic_on.set if on else _mic_on.clear)()
    _recompute_capture()
    if _wakefree.is_set():
        if on:
            if _wf_proc is None or _wf_proc.poll() is not None:
                _spawn_wakefree()
        else:
            # Stop the child outright rather than USR1-pausing it: the pause signal races the child's
            # handler installation, and Python's DEFAULT SIGUSR1 action is to terminate — signalling a
            # just-spawned child killed it (rc=-30) and the supervisor respawned it LISTENING. Killing
            # is also what "mic off" should mean: the mic is fully released, not held muted.
            subprocess.Popen(["pkill", "-f", "wakefree.py"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not on:
        set_sensor(False)          # the passive sensor holds a mic stream too — mic off stops it
    elif not _wakefree.is_set() and _read_setting("wakefree_autooff_at"):
        set_sensor(True)           # mic back while still braked → resume sensing
    log(f"mac mic {'ON' if on else 'OFF'} (mac_mic_enabled) — ESP unaffected")


def _on_ptt_start(*_):   # Hammerspoon double-tap → global mute (Mac instant + ESP via /capture)
    _apply_mute(True)


def _on_ptt_stop(*_):
    _apply_mute(False)


_wf_proc: subprocess.Popen | None = None   # current wakefree.py child — supervised, respawned on death
_wf_deaths: list[float] = []               # monotonic times of recent rapid deaths → crash-loop detection
WF_RAPID_S = 30            # a child that dies in under this never got healthy (a real session runs minutes)
WF_STORM_N = 5            # this many rapid deaths in the window = the audio stack is wedged, not the child
WF_STORM_WINDOW_S = 120
WF_BACKOFF_MAX_S = 8


def _spawn_wakefree() -> None:
    """Start wakefree.py and supervise it: a daemon thread blocks on wait() (event-driven, no polling)
    and respawns the child if it dies while wake-free mode is still on — e.g. its CoreAudio watchdog
    self-exited on a wedged stream. Without this, a dead child silently took the Mac mic down for days.

    Self-heal: a persistently wedged CoreAudio subsystem kills the child within seconds, over and over —
    respawning can't fix a wedge that lives in the audio stack, not the child. That crash-loop used to (a)
    leak a log fd every spawn until the listener hit [Errno 24] and went fully deaf, and (b) spin ~hot. So
    now we close the parent's log handle each spawn, back off exponentially on a fast loop, and after
    WF_STORM_N rapid deaths escalate to the same in-place re-exec the mic watchdog uses — a fresh image
    clears both the wedged HAL client and any fd pressure. A healthy run (>WF_RAPID_S) resets the counter."""
    global _wf_proc
    started = time.monotonic()
    wflog = None
    try:
        wflog = open(os.path.expanduser("~/repos/voice-pipeline/voice-id/wakefree.out.log"), "a")
        _wf_proc = subprocess.Popen([WAKEFREE_RUN], stdout=wflog, stderr=wflog)  # logged so capture/speaker-ID/inject is visible
    except Exception as e:  # noqa: BLE001
        log(f"failed to start wakefree: {e}")
        return
    finally:
        if wflog is not None:
            wflog.close()   # the child inherited its own dup; the parent MUST NOT keep this fd (leaked one per respawn → [Errno 24])

    def _supervise(proc: subprocess.Popen) -> None:
        rc = proc.wait()
        if not (_wakefree.is_set() and _mic_on.is_set() and proc is _wf_proc):
            return                      # deliberate stop (mode off / mic off) or a superseded child
        if rc == 2:
            log("wakefree.py exited rc=2 (fatal config — missing key/exemplars); NOT respawning")
            return
        now = time.monotonic()
        lifetime = now - started
        _wf_deaths[:] = [t for t in _wf_deaths if now - t < WF_STORM_WINDOW_S]   # only deaths in the window count
        if lifetime >= WF_RAPID_S:
            _wf_deaths.clear()          # a healthy session clears the crash-loop history
            backoff = 1
        else:
            _wf_deaths.append(now)
            if len(_wf_deaths) >= WF_STORM_N:
                _self_reexec(f"wakefree crash-loop ({WF_STORM_N} deaths in <{WF_STORM_WINDOW_S}s) — audio stack wedged, not the child")
                return                  # unreachable: execv replaced the image
            backoff = min(WF_BACKOFF_MAX_S, 2 ** len(_wf_deaths))
        log(f"wakefree.py died rc={rc} after {lifetime:.0f}s while wake-free is on — respawning in {backoff}s")
        time.sleep(backoff)
        if _wakefree.is_set() and proc is _wf_proc:
            _spawn_wakefree()

    threading.Thread(target=_supervise, args=(_wf_proc,), daemon=True).start()


def set_wakefree(on: bool) -> None:
    """Toggle wake-free mode (driven by the hub setting). On → listener yields the mic and starts
    wakefree.py; Off → stop wakefree.py and reclaim the mic for wake-word capture."""
    if on and not _wakefree.is_set():
        set_sensor(False)   # live wake-free stamps the same presence — the passive sensor is redundant
        _wakefree.set(); _recompute_capture()
        log("wake-free ON — yielding mic, starting wakefree.py (+ ESP)")
        if _mic_on.is_set():
            _spawn_wakefree()
        else:
            log("  …but mac_mic_enabled=0 — not starting wakefree.py; the ESP carries wake-free alone")
        # Pin the TTS engine warm for the whole session (don't let it idle-exit between commands and
        # cold-reload), and warm it now so the first command isn't delayed.
        _pin_tts(True); warm_tts()
        threading.Thread(target=_ping_esp_wakefree, args=(True,), daemon=True).start()
    elif not on and _wakefree.is_set():
        log("wake-free OFF — stopping wakefree.py, reclaiming mic (+ ESP)")
        _wakefree.clear(); _recompute_capture()   # cleared FIRST so the supervisor won't respawn the kill
        subprocess.Popen(["pkill", "-f", "wakefree.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _pin_tts(False)   # session over → let the daemon idle-exit and give the ~1 GB back
        threading.Thread(target=_ping_esp_wakefree, args=(False,), daemon=True).start()
        # The governor's off writes the brake marker BEFORE flipping the toggle, so by the time this
        # event arrives the marker is already set and its own event found wake-free still on. Re-check
        # here or the sensor never starts on an auto-off.
        if _read_setting("wakefree_autooff_at"):
            set_sensor(True)


def set_sensor(on: bool) -> None:
    """Governor brake (wakefree_autooff_at set) → run wakefree.py as a PASSIVE voice sensor: ECAPA
    stays warm and stamps the shared heard_jared_at on confirmed voice, so the worker holds the brake
    while the room conversation continues and re-arms always-on once it goes quiet. The listener KEEPS
    the mic — wake words keep working (macOS lets both processes read the same input; wake-free mode
    already runs both streams, just with the roles reversed). The sensor never captures, transcribes,
    or enqueues. No-op while wake-free itself is on."""
    global _sensor_proc
    if on:
        if _sensor.is_set() or _wakefree.is_set() or not _mic_on.is_set():
            return
        _sensor.set()
        _spawn_sensor()
        log("governor brake ON — voice sensor started (mic stays with wake words)")
    else:
        if not _sensor.is_set():
            return
        _sensor.clear()   # cleared FIRST so the supervisor won't respawn the kill
        proc, _sensor_proc = _sensor_proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
        log("governor brake off — voice sensor stopped")


def _spawn_sensor() -> None:
    global _sensor_proc
    started = time.monotonic()
    slog = None
    try:
        slog = open(os.path.expanduser("~/repos/voice-pipeline/voice-id/wakefree.out.log"), "a")
        _sensor_proc = subprocess.Popen([WAKEFREE_RUN], stdout=slog, stderr=slog,
                                        env={**os.environ, "WAKEFREE_SENSOR": "1"})
    except Exception as e:  # noqa: BLE001
        log(f"failed to start voice sensor: {e}")
        return
    finally:
        if slog is not None:
            slog.close()   # same fd rule as _spawn_wakefree: the child holds its own dup

    def _supervise(proc: subprocess.Popen) -> None:
        rc = proc.wait()
        if not (_sensor.is_set() and _mic_on.is_set() and proc is _sensor_proc) or rc == 2:
            return   # deliberate stop, superseded, or fatal config
        # Best-effort respawn with a flat backoff — no storm escalation like the real wakefree gets: a
        # sensor that keeps dying only makes re-arm happen EARLIER (absence of stamps reads as quiet).
        time.sleep(max(0.0, 10.0 - (time.monotonic() - started)))
        if _sensor.is_set() and _mic_on.is_set() and proc is _sensor_proc:
            _spawn_sensor()

    threading.Thread(target=_supervise, args=(_sensor_proc,), daemon=True).start()


# Flag the Claude Code hooks read: present → speak my replies (Kokoro) + write them spoken-word-friendly.
# Maintained here from the hub's voice_inject_mode (this replaces the old Cmd+Opt+C conversation toggle).
INJECT_FLAG = os.path.expanduser("~/.hammerspoon/voice-inject-mode")


def set_inject_mode(on: bool) -> None:
    """Voice inject mode (hub toggle `voice_inject_mode`). On → ensure always-listening (wake-free) is up
    so the Mac mic drives it, and write the inject flag so the transcript is pasted into the focused window.
    Speech is a SEPARATE switch (voice_speak_replies) — inject mode no longer implies spoken replies. Off →
    remove the flag and restore wake-free to its own hub setting. The ESP is gated off by the Fly server
    itself (it reads voice_inject_mode), so the Mac stays the only listener."""
    if on:
        if not _wakefree.is_set():
            set_wakefree(True)
        try:
            open(INJECT_FLAG, "w").close()
        except OSError:
            pass
        log("inject mode ON — Mac-mic → focused window; ESP gated off by the server (speech is a separate toggle)")
    else:
        try:
            os.remove(INJECT_FLAG)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        set_wakefree(_read_setting("wakefree_enabled"))   # inject forced wake-free on; restore its own toggle
        log("inject mode OFF — wake-free restored to its hub setting")


# Speech-reply switch (hub toggle `voice_speak_replies`), independent of inject mode: mirrors to the
# voice-speak flag that the Stop / PreToolUse hooks (voice_speak.py) gate on. Default off → the final
# reply is injected silently; on → it's read aloud via the warm Kokoro daemon.
SPEAK_FLAG = os.path.expanduser("~/.hammerspoon/voice-speak")


def set_speak_replies(on: bool) -> None:
    if on:
        try:
            open(SPEAK_FLAG, "w").close()
        except OSError:
            pass
        log("speak replies ON — final reply read aloud")
    else:
        try:
            os.remove(SPEAK_FLAG)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        log("speak replies OFF — replies stay silent")


def _read_setting(key: str, default: bool = False) -> bool:
    """`default` is returned when the row is absent OR unreadable — so a switch that must fail SAFE
    (mac_mic_enabled) isn't silently flipped off by a missing row or a blip in connectivity."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": f"eq.{key}", "select": "value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        rows = r.json()
        if not rows:
            return default
        return str(rows[0].get("value")) not in ("0", "false", "off", "no", "", "None")
    except Exception:  # noqa: BLE001
        return default


def _write_setting(key: str, value: str) -> None:
    """Write a hub setting, CREATING it if it is not there yet. This is how the Mac holds the global mute
    (`voice_mute_mac`, which the hub folds into `voice_muted`), and an update alone does nothing to a row
    that does not exist: the hold never reached the table, `voice_muted` stayed 0, and the Fly server's
    settings backstop took that 0 as a missed update and unmuted the ESP mid-dictation, so the puck heard
    the rest and ran it as a command."""
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings",
                       params={"on_conflict": "key"},
                       headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                "Content-Type": "application/json",
                                "Prefer": "resolution=merge-duplicates,return=minimal"},
                       json={"key": key, "value": value}, timeout=8)
        if r.status_code >= 300:
            log(f"setting {key} not written: {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        log(f"setting {key} not written: {e}")


def _reconcile_settings() -> None:
    """Re-read the mic-state hub settings and re-apply them. Recycling the realtime socket every ~5 min
    reconnects, but it does NOT recover STATE from an UPDATE that arrived while the socket was down or
    half-open — if the flip to wake-free-on was that missed event, the listener stays stuck off forever
    (the 4.3-day 'Mac down'). Re-asserting the current DB values on every (re)connect self-heals that
    within one recycle. set_wakefree / _apply_mute both early-return when already in state, so this is a
    no-op in the normal case. Best-effort; runs off the realtime loop so the httpx reads never block it."""
    try:
        # mac_mic_enabled FIRST: set_wakefree consults it, so reading it last made every restart
        # spawn wakefree.py (a ~1 GB ECAPA load) only to kill it 0.7s later.
        set_mac_mic(_read_setting("mac_mic_enabled", default=True))
        set_wakefree(_read_setting("wakefree_enabled"))
        _apply_mute(_read_setting("voice_muted"))
    except Exception as e:  # noqa: BLE001
        log(f"settings reconcile error: {e}")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} [mac-listener] {msg}", flush=True)


# ---- local feedback: a screen-level state HUD + Raspy's reply spoken aloud -----------------------
SPEAK_SH = os.path.expanduser("~/scripts/speak.sh")
HS_BIN = os.environ.get("HS_BIN", "/opt/homebrew/bin/hs")   # Hammerspoon IPC CLI
VOICE_DAEMON = os.path.expanduser("~/scripts/voice_daemon.sh")  # starts the warm Kokoro TTS engine
# Last state we actually pushed to Hammerspoon. On DISK, not in memory: execv wipes memory, so an
# in-process variable would forget the pill state on every self-heal and re-send the same clear.
HUD_STATE_FILE = os.path.expanduser("~/.cache/voice-mac-listener/hud-state")
_hud_send_lock = threading.Lock()


def hud(state: str) -> None:
    """Drive the Hammerspoon overlay HUD (listening/working/done/error/off). Fire-and-forget so it
    never adds latency to the wake→capture path. The HUD is a screen-level canvas that shows over
    full-screen apps and through Focus/DnD — unlike the old osascript banner, which macOS silently
    suppressed (attributed to Script Editor, killed in full-screen and Do-Not-Disturb).

    Redundant states are dropped rather than sent. Hammerspoon's IPC port is not safe to flood: its
    error path logs back through the same port and re-enters the runloop, so a queue of requests
    recurses instead of unwinding (it crashed that way on 2026-08-01). Nothing about the HUD needs a
    repeat of a state Hammerspoon is already showing, and the dedupe is what keeps a wedged-audio
    restart loop — one hud("off") per re-exec, every ~15s for hours — down to a single call."""
    try:
        with _hud_send_lock:
            try:
                with open(HUD_STATE_FILE) as fh:
                    if fh.read().strip() == state:
                        return
            except OSError:
                pass                                    # no record yet → send it
            os.makedirs(os.path.dirname(HUD_STATE_FILE), exist_ok=True)
            with open(HUD_STATE_FILE, "w") as fh:
                fh.write(state)
            subprocess.Popen([HS_BIN, "-c", f'VoiceHUD("{state}")'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


# ---- Central HUD state, driven off the SHARED voice state (voice_presence + voice_requests) -----------
# Same precedence as the ESP LED and the floor lamp: Hearing-you (locked) > Working > idle. So the pill
# shows 'Hearing you' the instant any mic confirms you — even over a processing command — which is the
# feedback that was missing (nothing showed until 'working' before).
_hud_hearing = False
_hud_working = False
_hud_hearing_timer = None
_hud_lock = threading.Lock()


def _hud_render() -> None:
    if _hud_hearing:
        hud("locked")
    elif _hud_working:
        hud("working")
    else:
        hud("off")


def _hud_hearing_on() -> None:
    global _hud_hearing, _hud_hearing_timer
    with _hud_lock:
        _hud_hearing = True
        if _hud_hearing_timer:
            _hud_hearing_timer.cancel()
        _hud_hearing_timer = threading.Timer(1.6, _hud_hearing_off)
        _hud_hearing_timer.daemon = True
        _hud_hearing_timer.start()
    _hud_render()


def _hud_hearing_off() -> None:
    global _hud_hearing
    with _hud_lock:
        _hud_hearing = False
    _hud_render()


def warm_tts() -> None:
    """Pre-warm the Kokoro TTS daemon the moment a command is incoming, so its ~8s model load happens
    DURING capture + the model's thinking and the engine is already hot when the reply is spoken. The
    daemon self-exits after idle, so this only holds the ~1 GB around active voice use — never
    permanently. Idempotent + fire-and-forget (voice_daemon.sh no-ops if it's already running)."""
    try:
        subprocess.Popen([VOICE_DAEMON, "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


KOKORO_PIN = "/tmp/kokoro.pin"   # present = keep the TTS daemon warm (don't idle-exit); see kokoro_daemon.py


def _pin_tts(on: bool) -> None:
    """Pin/unpin the TTS daemon warm. While wake-free is ON we keep the engine resident for the whole
    session so spaced-out commands never hit a cold ~7s reload; OFF lets it idle-exit and free the RAM."""
    try:
        if on:
            open(KOKORO_PIN, "w").close()
        else:
            os.path.exists(KOKORO_PIN) and os.remove(KOKORO_PIN)
    except Exception:  # noqa: BLE001
        pass


def _speak(text: str) -> None:
    """Play a reply aloud via Kokoro (speak.sh), in its own thread so neither the realtime loop nor
    the wake loop is ever blocked by synthesis/playback."""
    try:
        subprocess.run([SPEAK_SH], input=text, text=True, capture_output=True, timeout=90)
    except Exception as e:  # noqa: BLE001
        log(f"speak failed: {e}")


# ---- universal feedback (event-driven) -----------------------------------------------------------
# ONE persistent Supabase realtime subscription that drives ALL universal voice feedback on this Mac,
# regardless of which input captured the command (see voice-pipeline/ARCHITECTURE.md, "Feedback is
# universal"):
#   · voice_speech INSERT   → play the assistant's single spoken line aloud (Kokoro). This is the only
#                             audio channel; the model emits it via the `say` MCP tool.
#   · voice_requests UPDATE → drive the HUD state: status 'claimed' → working, 'done'/'error' → done.
# The local 'listening' state stays on the wake loop below — only this Mac knows its own mic is open.
# No polling, no fixed wait; both tables are in the supabase_realtime publication (00016 / 00021 /
# 00024). The lib server-assigns a distinct id per binding and only invokes the matching callback,
# so the two bindings never cross-fire. Mirrors the worker's RealtimeWaker (same AsyncRealtimeClient).
class RealtimeFeedback:
    def __init__(self):
        self._loop = None
        self._sock = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, name="rt-feedback", daemon=True).start()
        log("realtime feedback starting (speech on voice_speech + universal HUD on voice_requests)")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            log(f"realtime feedback loop exited: {e}")

    async def _serve(self) -> None:
        from realtime import AsyncRealtimeClient
        # Outer loop rebuilds the client if connect()/subscribe() itself fails (e.g. no network at
        # boot); auto_reconnect handles transient drops once connected.
        while True:
            try:
                self._sock = AsyncRealtimeClient(f"{SUPABASE_URL}/realtime/v1", SUPABASE_KEY,
                                                 auto_reconnect=True)
                await self._sock.connect()
                ch = self._sock.channel("mac-listener-feedback")
                ch.on_postgres_changes("INSERT", callback=self._on_speech,
                                       schema="public", table="voice_speech")
                ch.on_postgres_changes("UPDATE", callback=self._on_request,
                                       schema="public", table="voice_requests")
                ch.on_postgres_changes("UPDATE", callback=self._on_setting,
                                       schema="public", table="app_settings")
                ch.on_postgres_changes("UPDATE", callback=self._on_presence,
                                       schema="public", table="voice_presence")
                await ch.subscribe(self._on_sub)
                # Re-assert current DB state on every (re)connect: recycling reconnects, but a STATE-changing
                # UPDATE that landed while the socket was down/half-open is lost — which stranded the mic
                # wake-free-off for 4.3 days. Reconcile self-heals that within one recycle (off-thread so
                # its blocking reads never stall the realtime loop).
                threading.Thread(target=_reconcile_settings, daemon=True).start()
                # A realtime socket can go HALF-OPEN (is_connected stays True while no events flow), which
                # silently stops the mute + wake-free reactions until a manual restart — the same failure
                # the Fly server hit. Don't trust is_connected forever: recycle the socket every ~5 min so a
                # half-open one is torn down and rebuilt instead of staying deaf.
                started = time.monotonic()
                while self._sock.is_connected and (time.monotonic() - started) < 300.0:
                    await asyncio.sleep(1.0)
                log("realtime feedback recycling socket (proactive half-open guard)")
            except Exception as e:  # noqa: BLE001
                log(f"realtime feedback error: {e}; retrying in 5s")
            try:
                if self._sock:
                    await self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5.0)

    def _on_speech(self, payload) -> None:
        """voice_speech is now spoken by the Pi audio service (warm Kokoro → the TV) — the single audio
        owner — so the Mac no longer plays it (avoids a double-play). Kept subscribed only for visibility;
        the Mac-as-speaker option can return later as a hub-toggled output source."""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        text = (rec.get("text") or "").strip()
        if text:
            log(f"voice_speech (spoken on the Pi/TV now, not here): {text[:80]!r}")

    def _on_setting(self, payload) -> None:
        """A hub setting changed → react to the wake-free toggle and the global mute. Event-driven."""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        key = rec.get("key")
        truthy = str(rec.get("value")) not in ("0", "false", "off", "no", "", "None")
        if key == "wakefree_enabled":
            set_wakefree(truthy)
        elif key == "wakefree_autooff_at":   # governor brake marker → passive voice sensor while red
            set_sensor(truthy)
        elif key == "voice_muted":      # global mute set by any explicit input (PTT / FAB)
            _apply_mute(truthy)
        elif key == "mac_mic_enabled":     # per-device: silence THIS Mac's mic, ESP stays live
            set_mac_mic(truthy)
        elif key == "voice_inject_mode":   # hands-free: Mac-mic speech → focused window (ESP off)
            set_inject_mode(truthy)
        elif key == "voice_speak_replies":   # read the final reply aloud (independent of inject mode)
            set_speak_replies(truthy)

    def _on_request(self, payload) -> None:
        """A command's status changed (any source) → reflect it on the HUD, and pre-warm TTS on claim.
        Universal. (Warming lives here, not on a voice_requests INSERT binding, because realtime-py
        doesn't reliably support two bindings on the same table; 'claimed' is ~0.3s after INSERT, so
        the timing is unchanged. This Mac's OWN commands are warmed even earlier by the wake loop.)"""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        global _hud_working
        status = str(rec.get("status") or "")
        if status == "claimed":
            warm_tts()              # any source's command is now being worked → make the TTS engine hot
            _hud_working = True
            _hud_render()           # shows 'working' UNLESS you're currently being heard (then it stays locked)
        elif status in ("done", "error"):
            _hud_working = False
            if _hud_hearing:
                _hud_render()
            else:
                hud("done" if status == "done" else "error")   # brief flash → fades

    def _on_presence(self, payload) -> None:
        """Any mic confirmed Jared (shared voice_presence) → HUD 'Hearing you'. Central, not just this Mac's mic."""
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        hj = rec.get("heard_jared_at")
        try:
            fresh = bool(hj) and (datetime.now(timezone.utc) - datetime.fromisoformat(hj)).total_seconds() <= 1.5
        except Exception:  # noqa: BLE001
            fresh = False
        if fresh:
            _hud_hearing_on()

    def _on_sub(self, state, err=None) -> None:
        log(f"realtime feedback subscription: {state}{(' err=' + str(err)) if err else ''}")


def strip_wake(text: str) -> str:
    """Drop a leading wake phrase the STT often captures (tolerant of mishears)."""
    text = re.sub(
        r"^\W*(hey|hi|ok|okay)?\W*(rhasspy|rhaspy|raspy|rasbee|rasby|raspberry|rasp)\b[\s,.!:;-]*",
        "", text or "", count=1, flags=re.IGNORECASE,
    ).strip()
    return re.sub(
        r"^\W*(hey|hi|ok|okay)?\W*(jarvis|jarvas|jervis)\b[\s,.!:;-]*",
        "", text, count=1, flags=re.IGNORECASE,
    ).strip()


def _write_wav(pcm: np.ndarray) -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return path


def _groq_transcribe(path: str):
    """whisper-large-v3-turbo on Groq — faster + more accurate than local. None → fall back to local."""
    try:
        with open(path, "rb") as f:
            r = httpx.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("clip.wav", f, "audio/wav")},
                # English, always: auto-detection guesses per clip and has produced Icelandic.
                data={"model": GROQ_MODEL, "response_format": "text", "language": "en"},
                timeout=30.0,
            )
        if r.status_code == 200:
            return r.text.strip()
        log(f"groq {r.status_code} — falling back to local whisper")
    except Exception as e:  # noqa: BLE001
        log(f"groq error ({e}) — falling back to local whisper")
    return None


# WHISPER WRITES "Thank you." FOR A CLIP WITH NO SPEECH IN IT: silence, breath, a room. A transcript
# that is that phrase and nothing else is treated as nothing heard, so it never becomes a command. The
# cost is that saying only "thank you" is dropped too. The same rule is in server/server.py and status-hub's server/lib/transcribe.js.
_NOTHING_SAID = re.compile(r"^\W*thank\s+you\W*$", re.IGNORECASE)


def _heard(text: str) -> str:
    return "" if _NOTHING_SAID.match(text) else text


def transcribe(pcm: np.ndarray) -> str:
    """Groq (fast + accurate) when GROQ_API_KEY is set; else local whisper.cpp. '' if too short/failed."""
    if pcm.size < int(SAMPLE_RATE * MIN_UTTER_S):
        return ""
    path = _write_wav(pcm)
    try:
        if GROQ_API_KEY:
            text = _groq_transcribe(path)
            if text is not None:
                return _heard(text)
        out = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", path, "--no-prints", "-nt", "-l", "en"],
            capture_output=True, text=True, timeout=90,
        )
        return _heard(out.stdout.strip())
    except Exception as e:  # noqa: BLE001
        log(f"transcribe failed: {e}")
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


LED_TOKEN = os.environ.get("LED_TOKEN", "")


def _esp_blink() -> None:
    """Blink the ESP the instant a Mac-captured command is committed (capture-accept), so both
    working indicators start together instead of waiting for the worker's claim ping."""
    try:
        httpx.get(f"{JARED_VOICE_URL}/led-blink",
                  params={"token": LED_TOKEN} if LED_TOKEN else None, timeout=3)
    except Exception:  # noqa: BLE001
        pass


def open_request(cap_start: str, cap_end: str) -> str | None:
    """Open the command's lifecycle row at CAPTURE-ACCEPT (status 'transcribing', no transcript yet) —
    the moment the working indicators turn on (floor lamp amber via pi-audio's realtime sub on this
    insert; ESP blink pinged alongside). Returns the row id, or None (fall back to insert-at-enqueue)."""
    if not SUPABASE_KEY:
        return None
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/voice_requests",
                       headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                "Content-Type": "application/json", "Prefer": "return=representation"},
                       json={"status": "transcribing", "source": "mac", "focus": "hub", "always_on": True,
                             "capture_start": cap_start, "capture_end": cap_end}, timeout=6)
        rows = r.json()
        return rows[0]["id"] if isinstance(rows, list) and rows else None
    except Exception as e:  # noqa: BLE001
        log(f"open-request failed: {e}")
        return None


def update_request_pending(rid: str, text: str) -> None:
    """Fill the lifecycle row in with the transcript and hand it to the workers (status 'pending').
    Checked and retried, and guarded on 'transcribing' so a late write cannot revive a row the worker's
    sweep already closed — unchecked, a failed write left the transcript stranded until that sweep."""
    for pause in (0.5, 1.0, 2.0, None):
        try:
            r = httpx.patch(f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}&status=eq.transcribing",
                            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                     "Content-Type": "application/json", "Prefer": "return=representation"},
                            json={"transcript": text, "status": "pending",
                                  "updated_at": datetime.now(timezone.utc).isoformat()}, timeout=10)
            r.raise_for_status()
            if not r.json():
                log(f"enqueue: row {rid} is no longer transcribing; transcript dropped")
                return
            return
        except Exception as e:  # noqa: BLE001
            if pause is None:
                log(f"enqueue (update) failed: {e}")
                return
            time.sleep(pause)


def close_request_error(rid: str, err: str) -> None:
    """Close a lifecycle row whose capture never became a runnable command — turns the indicators off."""
    try:
        httpx.patch(f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    json={"status": "error", "error": err,
                          "updated_at": datetime.now(timezone.utc).isoformat()}, timeout=6)
    except Exception:  # noqa: BLE001
        pass


def enqueue(text: str, cap_start: str | None = None, cap_end: str | None = None):
    """Fallback path (used when open_request failed at capture-accept): insert the finished transcript
    into voice_requests (source='mac', focus='hub'). Returns the new request_id or None. cap_start/
    cap_end stamp the recording's wall-clock window so the worker can dedup the ESP+Mac pair by span
    overlap."""
    if not (SUPABASE_KEY and text):
        return None
    try:
        payload: dict = {"transcript": text, "source": "mac", "focus": "hub", "always_on": True}
        if cap_start:
            payload["capture_start"] = cap_start
        if cap_end:
            payload["capture_end"] = cap_end
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/voice_requests",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json=payload,
            timeout=10.0,
        )
        rid = None
        try:
            rows = r.json()
            rid = rows[0]["id"] if isinstance(rows, list) and rows else None
        except Exception:  # noqa: BLE001
            pass
        log(f"enqueued (id={rid}): {text[:80]!r}")
        return rid
    except Exception as e:  # noqa: BLE001
        log(f"enqueue failed: {e}")
        return None


def capture_utterance(stream) -> np.ndarray:
    """Read frames until the speaker goes quiet for HANGOVER_S (or MAX_UTTER_S).

    The silence floor is tracked continuously as a running minimum of frame RMS — it snaps down to
    any quieter frame and drifts up only slowly toward sustained noise. That means the loud tail of
    the wake word at the very start of capture can't inflate it, and the floor keeps correcting all
    through the utterance. (The old approach froze a floor from the first 0.35s — which usually
    caught "…asspy" or the command's onset, set the threshold above the speaker's level so the
    command never registered as speech, and ran the full 30s cap. That was the "took forever" bug.)
    Speech = RMS a few × above that floor; once we've heard speech, HANGOVER_S of sub-threshold
    frames ends the utterance.
    """
    global _last_frame
    frames: list[np.ndarray] = []
    noise = None
    spoke = False
    silence_start = None
    t0 = time.monotonic()
    while True:
        buf, _ = stream.read(FRAME)
        _last_frame = time.monotonic()
        i16 = np.frombuffer(bytes(buf), dtype=np.int16)
        frames.append(i16.copy())
        rms = float(np.sqrt(np.mean((i16.astype(np.float32) / 32768.0) ** 2)))
        now = time.monotonic()

        if noise is None:
            noise = rms
        elif rms < noise:
            noise = rms                            # snap down to any quieter frame (true ambient)
        else:
            noise = 0.99 * noise + 0.01 * rms      # rise slowly toward sustained background noise

        threshold = max(noise * 3.0, 0.015)        # speech sits well above the ambient floor
        if rms >= threshold:
            spoke = True
            silence_start = None
        elif spoke:
            if silence_start is None:
                silence_start = now
            elif now - silence_start >= HANGOVER_S:
                break
        if now - t0 >= MAX_UTTER_S:
            break
    return np.concatenate(frames) if frames else np.array([], dtype=np.int16)


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY required (INSERT on voice_requests).", file=sys.stderr)
        return 2
    stt = f"groq:{GROQ_MODEL}" if GROQ_API_KEY else "whisper.cpp (local)"
    if not GROQ_API_KEY and (not os.path.exists(WHISPER_BIN) or not os.path.exists(WHISPER_MODEL)):
        print(f"FATAL: no GROQ_API_KEY and whisper.cpp not found ({WHISPER_BIN}).", file=sys.stderr)
        return 2

    _raise_fd_limit()   # headroom before we open models, sockets, and the wakefree supervisor
    log(f"loading wake models: fast={WAKE_MODEL!r} power={WAKE_MODEL_POWER!r} (threshold {WAKE_THRESHOLD})…")
    oww = Model(wakeword_models=[WAKE_MODEL, WAKE_MODEL_POWER], inference_framework="onnx")
    log(f"listening — 'Hey Rhasspy' or 'Hey Jarvis'  (stt={stt}, hangover={HANGOVER_S}s)")

    feedback = RealtimeFeedback()
    feedback.start()   # one persistent realtime sub → universal speech + HUD state (any source)

    # Push-to-talk takes the mic via signals; wake-free takes it via the hub toggle. Capture iff neither.
    signal.signal(signal.SIGUSR1, _on_ptt_start)
    signal.signal(signal.SIGUSR2, _on_ptt_stop)
    # mac_mic_enabled BEFORE wake-free: set_wakefree consults it, so applying it after made boot spawn
    # wakefree.py (a ~1 GB ECAPA load) only to kill it moments later.
    set_mac_mic(_read_setting("mac_mic_enabled", default=True))
    if _read_setting("wakefree_enabled"):  # apply persisted hub toggle at boot; live flips via RealtimeFeedback
        set_wakefree(True)
    elif _read_setting("wakefree_autooff_at"):   # booted mid-brake → resume passive sensing
        set_sensor(True)
    set_inject_mode(_read_setting("voice_inject_mode"))  # apply persisted inject toggle (forces wake-free on if set)
    set_speak_replies(_read_setting("voice_speak_replies"))  # apply persisted speech toggle (default off)
    # Give back THIS Mac's mute on boot: push-to-talk mutes are Mac-local, so a fresh listener means
    # nothing is dictating here. Only its own holder — another device may be capturing right now, and
    # the hub recomputes `voice_muted` from what is left.
    _write_setting("voice_mute_mac", "")
    # Same self-heal for the HUD: a prior instance that died mid-capture (watchdog os._exit, SIGKILL, or
    # a forced sleep) can leave the 'listening' pill frozen on screen. A fresh listener means the mic is
    # idle, so clear it now — every (re)start un-sticks a stale overlay.
    hud("off")

    global _last_frame
    yielded_logged = False
    with _open_input_stream() as stream:
        _last_frame = time.monotonic()
        threading.Thread(target=_watchdog, daemon=True).start()
        threading.Thread(target=_alive_beacon, daemon=True).start()
        oww.reset()
        while True:
                buf, _ = stream.read(FRAME)
                _last_frame = time.monotonic()
                if not _capture.is_set():
                    # Software mute: the stream stays hot (stopping it is the HAL deadlock); frames are
                    # read and dropped so nothing buffers while push-to-talk / wake-free holds the mic.
                    if not yielded_logged:
                        log("mic yielded (push-to-talk / wake-free) — discarding frames, stream stays open")
                        yielded_logged = True
                    continue
                if yielded_logged:
                    yielded_logged = False
                    oww.reset()          # fresh activation state — don't fire on stale pre-yield scores
                    log("listening (wake word)")
                audio = np.frombuffer(bytes(buf), dtype=np.int16)
                scores = oww.predict(audio)
                fast_score = scores.get(WAKE_MODEL, 0.0)
                power_score = scores.get(WAKE_MODEL_POWER, 0.0)
                if max(fast_score, power_score) >= WAKE_THRESHOLD:   # either wake word — same route
                    log(f"wake! (fast={fast_score:.2f} power={power_score:.2f}) — capturing…")
                    hud("listening")                    # LOCAL: only this Mac knows its own mic is open
                    warm_tts()                          # pre-warm TTS now — load overlaps capture + thinking
                    cap_start = datetime.now(timezone.utc)   # wall-clock start of the recording window
                    pcm = capture_utterance(stream)
                    cap_end = datetime.now(timezone.utc)     # …and its end (before transcription)
                    hud("working")                      # capture done → transcribing / processing
                    # CAPTURE ACCEPTED — the command is committed from this instant: open the lifecycle
                    # row (lamp amber) + blink the ESP NOW, before STT runs.
                    rid = open_request(cap_start.isoformat(), cap_end.isoformat())
                    threading.Thread(target=_esp_blink, daemon=True).start()
                    # Transcription blocks stream reads (Groq / whisper.cpp) — pause the watchdog's expectation.
                    _expect_frames.clear()
                    try:
                        text = strip_wake(transcribe(pcm))
                        if text:
                            log(f"transcript: {text!r}")
                            # working/done HUD and the spoken reply are now driven UNIVERSALLY off
                            # voice_requests status + voice_speech (RealtimeFeedback) — for every source.
                            if rid:
                                update_request_pending(rid, text)
                            else:
                                enqueue(text, cap_start=cap_start.isoformat(), cap_end=cap_end.isoformat())
                        else:
                            if rid:
                                close_request_error(rid, "empty or failed transcription")
                            hud("off")                  # nothing intelligible → clear the local indicator
                            log("…nothing intelligible, ignoring")
                    finally:
                        _last_frame = time.monotonic()
                        _expect_frames.set()
                    oww.reset()  # clear the activation so the next wake is fresh


if __name__ == "__main__":
    sys.exit(main())
