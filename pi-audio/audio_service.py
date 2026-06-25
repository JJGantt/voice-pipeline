#!/usr/bin/env python3
"""Pi audio service — owns the TV speaker (HDMI) and is the single audio owner for the voice system.

Subscribes to Supabase realtime and plays, serialized through one lock so nothing overlaps:
  - voice_requests UPDATE → status 'claimed'  → the "heard you / processing" cue (audio_processing_sound).
    Gated on 'claimed' (a single, post-dedup event the worker emits once per command) rather than the raw
    INSERT, so two mics on one utterance don't double-ding.
  - voice_speech   INSERT                      → the assistant's final reply, synthesized via warm Kokoro
    (audio_spoken_reply). The reply is already a past-tense one-liner, so we just speak it.

Kokoro is loaded ONCE at startup and kept resident (warm) — the Pi has the RAM and stays idle between
commands, so synthesis is hot. Output goes through ALSA card 1 (the connected TV HDMI; card 0 errors).
NOTE: the HDMI sink suspends when idle, so a cold ALSA open clips the first ~150ms of a reply. Fix: every
clip is prefixed with LEAD_SILENCE_S of silence in the SAME stream, so the HDMI link does its cold-lock
during the silence and the first real syllable plays clean. (Keeping the sink 'warm' with pw-loopback was
tried and must NEVER be repeated — it bridges the sink to itself and howls. A silence prefix routes and
captures nothing, so feedback is impossible.)
Env (see ~/voice/pi/.env-audio): SUPABASE_URL, SUPABASE_KEY, optional ALSA_DEV, HEARD_WAV, KOKORO_VOICE.
"""
import os, re, asyncio, threading, subprocess, tempfile, time, collections, socket, json
from datetime import datetime, timezone
import numpy as np
import soundfile as sf
import httpx
from realtime import AsyncRealtimeClient

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ALSA_DEV  = os.environ.get("ALSA_DEV", "plughw:1,0")
HEARD_WAV = os.path.expanduser(os.environ.get("HEARD_WAV", "~/voice/sfx-candidates/02_initial_message-pop.wav"))
VOICE     = os.environ.get("KOKORO_VOICE", "af_heart")
LEAD_SILENCE_S = float(os.environ.get("LEAD_SILENCE_S", "0.4"))  # silence prefix that absorbs the HDMI cold-lock

# Toggle cache, mirrored from app_settings (boot fetch + realtime UPDATE). Default on.
_toggles = {"audio_processing_sound": True, "audio_spoken_reply": True}
_hearing = False             # currently hearing Jared? the cue fires on the FALLING edge (he stopped)
_hearing_clear = None        # timer that detects the falling edge once presence goes stale
_kok = None                  # resident Kokoro-ONNX synthesizer
MODEL_ONNX = os.path.expanduser(os.environ.get("KOKORO_MODEL", "~/voice/pi/kokoro-v1.0.onnx"))
VOICES_BIN = os.path.expanduser(os.environ.get("KOKORO_VOICES", "~/voice/pi/voices-v1.0.bin"))
_play_lock = threading.Lock()  # one speaker → serialize every playback
_cur_play = None               # in-flight aplay process (for barge-in / cut-off)
_heard_padded = None           # heard-you cue with the silence prefix baked in (built once at startup)
_speak_queue = collections.deque()   # replies waiting to be spoken — FIFO, in order; succinct so a short
_speak_lock = threading.Lock()       # queue is fine. Never dropped except the one you talk over.
_speak_event = threading.Event()
_muted = False                 # push-to-talk / FAB active → cut off + hold replies until you're done

# ---- Floor lamp = the 'Working' indicator (Govee H6072, LAN, ~10ms send) -----------------------------
# Deliberately NOT a hearing light: blue is Jared's ambient color, and the hearing edge lagged. The lamp
# only signals processing — amber the moment a command is claimed, off when it's done. LAN-direct UDP off
# the cached IP, no cloud. (Hearing still shows on the ESP LED + Mac HUD, just not here.)
FLOOR_DEV  = os.environ.get("FLOOR_DEV", "")
FLOOR_SKU  = os.environ.get("FLOOR_SKU", "H6072")
LAMP_MCAST = ("239.255.255.250", 4001)
LAMP_RECV_PORT, LAMP_CMD_PORT = 4002, 4003
LAMP_WORK  = (255, 120, 0)     # amber — a command is processing (Working)
LAMP_ENABLED = os.environ.get("LAMP_ENABLED", "1") not in ("0", "false", "off", "no", "")
_lamp_ip = None
_lamp_scan_at = 0.0
_lamp_working = False           # a voice_request is 'claimed' (processing) — set by on_request
_lamp_event = threading.Event()


def log(m): print(f"{time.strftime('%H:%M:%S')} [pi-audio] {m}", flush=True)


def strip_markdown(md: str) -> str:
    md = re.sub(r"```.*?```", " ", md, flags=re.DOTALL)
    md = re.sub(r"`([^`]*)`", r"\1", md)
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"https?://\S+", " ", md)
    md = re.sub(r"[*_~#>|]", " ", md)
    return re.sub(r"\s+", " ", md).strip()


def _play(path: str) -> None:
    """Play a WAV straight to the HDMI ALSA device. Direct-to-hw, no PipeWire routing — boring and safe."""
    global _cur_play
    with _play_lock:
        p = subprocess.Popen(["aplay", "-D", ALSA_DEV, path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _cur_play = p
        try:
            p.wait()
        finally:
            _cur_play = None


def _stop_speech() -> None:
    """Barge-in: cut off the reply PLAYING right now (you're talking over it). The QUEUE is kept — remaining
    succinct replies still play once you're done; only the one you talked over is dropped."""
    p = _cur_play
    if p is not None:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass


def _lead(samples, sr):
    """Prefix a clip with LEAD_SILENCE_S of silence so the HDMI cold-lock lands on silence, not the audio."""
    samples = np.asarray(samples)
    pad_len = int(sr * LEAD_SILENCE_S)
    pad = np.zeros((pad_len,) + samples.shape[1:], dtype=samples.dtype)
    return np.concatenate([pad, samples])


def _build_padded_cue() -> None:
    """Bake the silence prefix into the heard-you cue once, so its own first sound isn't cold-clipped."""
    global _heard_padded
    if not os.path.exists(HEARD_WAV):
        return
    try:
        data, sr = sf.read(HEARD_WAV)
        out = os.path.join(tempfile.gettempdir(), "heard_padded.wav")
        sf.write(out, _lead(data, sr), sr)
        _heard_padded = out
    except Exception as e:  # noqa: BLE001
        log(f"cue pad failed: {e}")


def play_heard() -> None:
    path = _heard_padded or HEARD_WAV
    if os.path.exists(path):
        _play(path)
    else:
        log(f"heard-you wav missing: {HEARD_WAV}")


def _lamp_scan(timeout: float = 1.2):
    """One multicast scan for the floor lamp; cache its IP. Same protocol as govee_speed.py."""
    global _lamp_ip, _lamp_scan_at
    recv = send = None
    try:
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError: pass
        recv.bind(("0.0.0.0", LAMP_RECV_PORT))
        recv.settimeout(0.3)
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        send.sendto(json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode(), LAMP_MCAST)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                data, _ = recv.recvfrom(2048)
                d = json.loads(data).get("msg", {}).get("data", {})
                if d.get("ip") and (d.get("device") == FLOOR_DEV or d.get("sku") == FLOOR_SKU):
                    _lamp_ip = d["ip"]; _lamp_scan_at = time.monotonic()
                    return _lamp_ip
            except (socket.timeout, json.JSONDecodeError):
                continue
    except Exception as e:  # noqa: BLE001
        log(f"lamp scan failed: {e}")
    finally:
        for s in (recv, send):
            try: s and s.close()
            except OSError: pass
    return None


def _lamp_send(msg: dict) -> None:
    """Fire one LAN command to the lamp; rescan the IP if stale (>30s) or unknown."""
    global _lamp_ip
    if not _lamp_ip or (time.monotonic() - _lamp_scan_at) > 30:
        _lamp_scan()
    if not _lamp_ip:
        return
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(json.dumps(msg).encode(), (_lamp_ip, LAMP_CMD_PORT))
        s.close()
    except Exception as e:  # noqa: BLE001
        log(f"lamp send failed: {e}")


def _lamp_apply(working: bool) -> None:
    if working:
        r, g, b = LAMP_WORK
        _lamp_send({"msg": {"cmd": "turn", "data": {"value": 1}}})
        _lamp_send({"msg": {"cmd": "colorwc", "data": {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}}})
    else:
        _lamp_send({"msg": {"cmd": "turn", "data": {"value": 0}}})


def _lamp_worker():
    """Amber while a command is processing, off otherwise. Sends only on a real change so it's not strobing
    the lamp; each change is a direct LAN UDP, reacting in ~10ms."""
    last = None
    while True:
        _lamp_event.wait()
        _lamp_event.clear()
        if _lamp_working != last:
            last = _lamp_working
            _lamp_apply(_lamp_working)


def _lamp_render():
    if LAMP_ENABLED:
        _lamp_event.set()


def _shorten(text: str) -> str:
    """Strip markdown for speech and apply only a GENEROUS safety cap (a runaway can't synth for minutes).
    Length otherwise follows the reply — barge-in is the real 'cut it short' control, not a hard truncation."""
    return strip_markdown(text)[:600]


def speak(text: str) -> None:
    text = _shorten(text)
    if not text:
        return
    t = time.monotonic()
    samples, sr = _kok.create(text, voice=VOICE, speed=1.0, lang="en-us")
    if samples is None or len(samples) == 0:
        return
    wav = tempfile.mktemp(suffix=".wav")
    sf.write(wav, _lead(samples, sr), sr)
    log(f"synth {time.monotonic() - t:.1f}s — {text[:60]!r}")
    _play(wav)
    try:
        os.remove(wav)
    except OSError:
        pass


def fetch_toggles() -> None:
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "in.(audio_processing_sound,audio_spoken_reply)", "select": "key,value"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10)
        for row in r.json():
            _toggles[row["key"]] = str(row["value"]) not in ("0", "false", "off", "no", "", "None")
    except Exception as e:
        log(f"toggle fetch failed: {e}")


def _rec(payload):
    return (payload.get("data") or {}).get("record") or {}


async def _clear_hearing(delay: float = 1.3):
    """Presence went stale → your voice stopped being confirmed → the whole command is captured and about
    to transcribe. THIS falling edge is when the 'heard you' cue fires (not the start, not after STT)."""
    global _hearing
    try:
        await asyncio.sleep(delay)
        if _hearing and _toggles.get("audio_processing_sound"):
            threading.Thread(target=play_heard, daemon=True).start()
        _hearing = False
    except asyncio.CancelledError:
        pass


def on_presence(payload):
    """Track 'hearing Jared' from voice_presence; each fresh confirm restarts the falling-edge timer. The
    cue itself fires in _clear_hearing when confirms stop — i.e. the command is done and about to transcribe."""
    global _hearing, _hearing_clear
    rec = _rec(payload)
    hj = rec.get("heard_jared_at")
    try:
        fresh = bool(hj) and (datetime.now(timezone.utc) - datetime.fromisoformat(hj)).total_seconds() <= 1.5
    except Exception:
        fresh = False
    if not fresh:
        return
    _stop_speech()              # barge-in: you're talking → shut up any reply that's playing or queued
    _hearing = True
    if _hearing_clear:
        _hearing_clear.cancel()
    _hearing_clear = asyncio.create_task(_clear_hearing())


def _speaker_loop():
    """Speak queued replies in order. Never START one while you're talking / push-to-talking — hold until
    you're done — so a reply can't begin over your next command. Barge-in cuts off the one that's playing."""
    while True:
        _speak_event.wait()
        with _speak_lock:
            text = _speak_queue.popleft() if _speak_queue else None
            if not _speak_queue:
                _speak_event.clear()
        if not text:
            continue
        while _hearing or _muted:      # hold until you've stopped talking / released push-to-talk
            time.sleep(0.1)
        speak(text)


def on_speech(payload):
    rec = _rec(payload)
    text = rec.get("text") or ""
    if text and _toggles.get("audio_spoken_reply"):
        with _speak_lock:
            _speak_queue.append(text)   # FIFO — play in order, don't drop
        _speak_event.set()


def on_request(payload):
    """Drive the lamp's Working state off any command's status (universal, same as the Mac HUD):
    'claimed' → processing (amber, unless you're still talking → blue wins); 'done'/'error' → clear."""
    global _lamp_working
    status = str(_rec(payload).get("status") or "")
    if status == "claimed":
        _lamp_working = True
        _lamp_render()
    elif status in ("done", "error"):
        _lamp_working = False
        _lamp_render()


def on_setting(payload):
    rec = _rec(payload)
    k = rec.get("key")
    if k == "voice_muted":
        global _muted
        _muted = str(rec.get("value")) not in ("0", "false", "off", "no", "", "None")
        if _muted:   # push-to-talk / FAB = "I'm recording" → cut off what's playing + hold the queue
            _stop_speech()
            log("voice_muted → barge-in + hold (push-to-talk / FAB)")
    elif k in _toggles:
        _toggles[k] = str(rec.get("value")) not in ("0", "false", "off", "no", "", "None")
        log(f"toggle {k} -> {_toggles[k]}")


async def serve():
    while True:
        try:
            sock = AsyncRealtimeClient(f"{SUPABASE_URL}/realtime/v1", SUPABASE_KEY, auto_reconnect=True)
            await sock.connect()
            ch = sock.channel("pi-audio")
            ch.on_postgres_changes("UPDATE", callback=on_presence, schema="public", table="voice_presence")
            ch.on_postgres_changes("UPDATE", callback=on_request, schema="public", table="voice_requests")
            ch.on_postgres_changes("INSERT", callback=on_speech, schema="public", table="voice_speech")
            ch.on_postgres_changes("UPDATE", callback=on_setting, schema="public", table="app_settings")
            await ch.subscribe()
            log("subscribed (voice_presence → hearing/lamp, voice_requests → working/lamp, voice_speech → TTS, app_settings → toggles)")
            while sock.is_connected:
                await asyncio.sleep(1.0)
        except Exception as e:
            log(f"realtime error: {e}; retry 5s")
        await asyncio.sleep(5)


def main():
    global _kok
    log("loading Kokoro-ONNX (resident)…")
    from kokoro_onnx import Kokoro
    _kok = Kokoro(MODEL_ONNX, VOICES_BIN)
    try:                                   # warm the graph so the first real synth is fast
        _kok.create("ready", voice=VOICE, speed=1.0, lang="en-us")
    except Exception as e:
        log(f"warmup failed: {e}")
    fetch_toggles()
    _build_padded_cue()                                          # silence-prefixed heard-you cue (no cold clip)
    threading.Thread(target=_speaker_loop, daemon=True).start()   # speaks only the latest reply
    if LAMP_ENABLED:
        threading.Thread(target=_lamp_worker, daemon=True).start()   # 'working' floor lamp (amber)
        ip = _lamp_scan()
        log(f"floor lamp {'at ' + ip if ip else 'not found on LAN yet (will rescan on first event)'}")
        _lamp_render()                                              # settle to idle (off) at boot
    log(f"ready — toggles {_toggles}, ALSA {ALSA_DEV}")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
