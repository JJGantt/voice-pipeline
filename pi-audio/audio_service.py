#!/usr/bin/env python3
"""Pi audio service — plays the assistant's spoken REPLY through the TV (HDMI), via PipeWire.

Subscribes to Supabase realtime:
  - voice_speech   INSERT  → the assistant's final reply, synthesized via warm Kokoro (audio_spoken_reply).
  - voice_requests UPDATE  → drives the 'working' floor lamp (amber on 'claimed', off on 'done').

The heard-you POP is NOT here anymore — the UI/browser plays it itself, synced to the moment it paints your
words (status-hub tv.js). Likewise the eventual plan is for the reply audio to move to the browser too, so
the Pi stops being an audio device. For now Kokoro stays resident here and the reply plays out.

Output goes through PipeWire (pw-cat → the HDMI sink), NOT direct-to-hardware, so the Pi shares the speaker
with the browser instead of locking it. A feeder thread writes raw int16 to pw-cat forever — digital
silence when idle, reply samples when speaking — which keeps the PipeWire stream active so the sink never
suspends (no cold-clip). Output-only, so feedback is impossible. The service unit must export
XDG_RUNTIME_DIR=/run/user/1000 to reach the user PipeWire.
Env (see ~/voice/pi/.env-audio): SUPABASE_URL, SUPABASE_KEY, optional PW_SINK, KOKORO_VOICE.
"""
import os, re, asyncio, threading, subprocess, time, collections, socket, json
from datetime import datetime, timezone
import numpy as np
import httpx
from realtime import AsyncRealtimeClient

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PW_SINK   = os.environ.get("PW_SINK", "alsa_output.platform-107c706400.hdmi.hdmi-stereo")  # HDMI via PipeWire
VOICE     = os.environ.get("KOKORO_VOICE", "af_heart")
STREAM_RATE  = 24000     # Kokoro's native rate → no resample for speech; PipeWire upconverts to the HDMI's 48k
STREAM_CHUNK = 600       # frames per pipe write (~25ms) — small so enqueued audio is picked up fast (low lag)

# Toggle cache, mirrored from app_settings (boot fetch + realtime UPDATE). Default on.
_toggles = {"audio_processing_sound": True, "audio_spoken_reply": True}
_hearing = False             # currently hearing Jared? the cue fires on the FALLING edge (he stopped)
_hearing_clear = None        # timer that detects the falling edge once presence goes stale
_kok = None                  # resident Kokoro-ONNX synthesizer
MODEL_ONNX = os.path.expanduser(os.environ.get("KOKORO_MODEL", "~/voice/pi/kokoro-v1.0.onnx"))
VOICES_BIN = os.path.expanduser(os.environ.get("KOKORO_VOICES", "~/voice/pi/voices-v1.0.bin"))
_pending = collections.deque()  # int16 chunks queued for the warm stream (the clip playing right now)
_audio_lock = threading.Lock()  # guards _pending
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


def _to_int16(samples):
    """Float [-1,1] → little-endian int16, the format the warm stream is fed in."""
    s = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (s * 32767.0).astype("<i2")


def _enqueue_audio(i16) -> None:
    """Hand int16 samples to the warm stream in STREAM_CHUNK pieces; played in order after what's queued."""
    with _audio_lock:
        for i in range(0, len(i16), STREAM_CHUNK):
            _pending.append(i16[i:i + STREAM_CHUNK])


def _stop_speech() -> None:
    """Barge-in: drop the audio queued right now (the clip you're talking over). The stream stays open and
    instantly reverts to silence — it is NEVER killed. The reply queue (_speak_queue) is untouched, so the
    rest still plays once you're done; only the one you talked over is dropped."""
    with _audio_lock:
        _pending.clear()


def _stream_loop():
    """The single always-open output, now through PipeWire (pw-cat) instead of grabbing the HDMI hardware
    exclusively. PipeWire owns the sink, so the browser/UI can MIX its own sounds in alongside us — the
    whole point: one shared speaker. The continuous-silence write keeps the PipeWire stream active so the
    sink never suspends (no cold-clip), and it's output-only so feedback is impossible. Reopens on error."""
    silence = np.zeros(STREAM_CHUNK, dtype="<i2").tobytes()
    while True:
        try:
            proc = subprocess.Popen(
                ["pw-cat", "-p", "--target", PW_SINK, "--rate", str(STREAM_RATE), "--channels", "1",
                 "--format", "s16", "--raw", "--latency", "80ms", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while True:
                with _audio_lock:
                    chunk = _pending.popleft() if _pending else None
                proc.stdin.write(chunk.tobytes() if chunk is not None else silence)
                proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            log(f"pw-cat stream error: {e}; reopening in 1s")
            time.sleep(1)


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


_lamp_saved = None   # the lamp's OWN state before the working overlay — captured on the idle→working
                     # edge, restored exactly on working→idle. The lamp is a real room light; the amber
                     # is a temporary overlay, never allowed to end in a blind 'off'.


def _lamp_status(timeout: float = 1.0):
    """Query the lamp's live state over LAN (devStatus → {onOff, brightness, color, colorTemInKelvin}),
    or None. Same protocol as govee-mcp's _lan_status_blocking."""
    global _lamp_ip
    if not _lamp_ip or (time.monotonic() - _lamp_scan_at) > 30:
        _lamp_scan()
    if not _lamp_ip:
        return None
    recv = send = None
    try:
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError: pass
        recv.bind(("0.0.0.0", LAMP_RECV_PORT))
        recv.settimeout(timeout)
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.sendto(json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode(), (_lamp_ip, LAMP_CMD_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = recv.recvfrom(2048)
            except socket.timeout:
                return None
            try:
                m = json.loads(data.decode()).get("msg", {})
            except (ValueError, UnicodeDecodeError):
                continue
            if m.get("cmd") == "devStatus":
                return m.get("data", {})
    except Exception as e:  # noqa: BLE001
        log(f"lamp status query failed: {e}")
    finally:
        for s in (recv, send):
            try: s and s.close()
            except OSError: pass
    return None


def _is_work_amber(st) -> bool:
    """True if a devStatus reading is our own working overlay — guards against ever snapshotting the
    amber itself as the 'prior' state (e.g. back-to-back commands racing the restore)."""
    c = (st or {}).get("color") or {}
    return (int(st.get("onOff", 0)) == 1
            and abs(int(c.get("r", -1)) - LAMP_WORK[0]) <= 5
            and abs(int(c.get("g", -1)) - LAMP_WORK[1]) <= 5
            and abs(int(c.get("b", -1)) - LAMP_WORK[2]) <= 5)


def _lamp_apply(working: bool) -> None:
    global _lamp_saved
    if working:
        st = _lamp_status()
        if st is not None and not _is_work_amber(st):
            _lamp_saved = st                     # exact prior state (on/off, brightness, color/kelvin)
        r, g, b = LAMP_WORK
        _lamp_send({"msg": {"cmd": "turn", "data": {"value": 1}}})
        _lamp_send({"msg": {"cmd": "colorwc", "data": {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}}})
    else:
        st = _lamp_saved
        if st is None:
            # Never guess and never go dark: with no known prior state, leaving the amber is the only
            # honest option (a blind 'off' kills a light he may be using).
            log("lamp: no saved prior state — leaving as-is")
            return
        # Restore color/brightness FIRST (so the device's remembered state is right even when the
        # prior state was off), then the power state.
        if st.get("brightness") is not None:
            _lamp_send({"msg": {"cmd": "brightness", "data": {"value": int(st["brightness"])}}})
        color = st.get("color") or {}
        _lamp_send({"msg": {"cmd": "colorwc",
                            "data": {"color": {"r": int(color.get("r", 255)), "g": int(color.get("g", 255)),
                                               "b": int(color.get("b", 255))},
                                     "colorTemInKelvin": int(st.get("colorTemInKelvin") or 0)}}})
        _lamp_send({"msg": {"cmd": "turn", "data": {"value": 1 if int(st.get("onOff", 0)) == 1 else 0}}})


def _lamp_worker():
    """Amber while a command is processing, off otherwise. Sends only on a real change so it's not strobing
    the lamp; each change is a direct LAN UDP, reacting in ~10ms."""
    last = None
    while True:
        _lamp_event.wait()
        _lamp_event.clear()
        if _lamp_working != last:
            last = _lamp_working
            log(f"lamp → {'working (amber overlay)' if _lamp_working else 'restore prior state'}")
            _lamp_apply(_lamp_working)


def _lamp_render():
    if LAMP_ENABLED:
        _lamp_event.set()


def _shorten(text: str) -> str:
    """Strip markdown for speech and apply only a GENEROUS safety cap (a runaway can't synth for minutes).
    Length otherwise follows the reply — barge-in is the real 'cut it short' control, not a hard truncation."""
    return strip_markdown(text)[:600]


def _play_pcm(i16) -> None:
    """Play already-synthesized audio (e.g. from the faster Mac synth) straight into the warm stream, with
    the same drain/barge-in semantics as a Pi-synth reply. Input is int16 LE mono at STREAM_RATE."""
    if _hearing or _muted or i16 is None or len(i16) == 0:
        return
    log(f"play (mac-synth pcm) {len(i16) / STREAM_RATE:.1f}s")
    _enqueue_audio(i16)
    while True:                          # drain (keeps _pending bounded so barge-in drops only this)
        with _audio_lock:
            if not _pending:
                break
        time.sleep(0.05)


def _play_http_server():
    """Accept pre-synthesized PCM from the Mac (the synth-on-Mac path) and play it through the warm stream.
    POST /play body = raw int16 LE mono at STREAM_RATE; routed through the SAME gated queue as Pi-synth
    replies, so it respects barge-in and the don't-start-while-you're-talking hold. Header X-Dry-Run:1
    confirms receipt + parse WITHOUT playing (so the path is testable with no sound). GET /health → ok."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):  # no per-request stderr spam
            pass

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            if self.path != "/play":
                self.send_response(404); self.end_headers(); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                i16 = np.frombuffer(body, dtype="<i2")
                if self.headers.get("X-Dry-Run") == "1":
                    log(f"/play DRY-RUN: {len(body)} bytes = {len(i16) / STREAM_RATE:.2f}s (not played)")
                elif _toggles.get("audio_spoken_reply"):
                    with _speak_lock:
                        _speak_queue.append(("pcm", i16))
                    _speak_event.set()
                    log(f"/play: {len(i16) / STREAM_RATE:.2f}s pcm queued")
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            except Exception as e:  # noqa: BLE001
                log(f"/play error: {e}")
                try:
                    self.send_response(500); self.end_headers()
                except Exception:  # noqa: BLE001
                    pass

    port = int(os.environ.get("PLAY_PORT", "27187"))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def _clauses(text: str):
    """Split a reply into clause-sized pieces on sentence/clause punctuation, so each can be synthesized and
    START PLAYING while the next is still synthesizing. Kokoro's own create_stream only splits on full
    sentences — a one-sentence reply wouldn't stream at all — so this is what gets the first words out fast.
    Split after punctuation+space (so decimals/amounts like '9.5'/'1,000' stay intact), and also before a
    coordinating conjunction so a punctuation-free sentence still streams. Tiny fragments are glued back."""
    parts = re.split(r"(?<=[.!?;:,—–])\s+|\s+(?=(?:and|but|so|then|because|while|plus|which)\b)", text)
    out = []
    for p in (p.strip() for p in parts):
        if not p:
            continue
        if out and len(out[-1]) < 12:    # glue a tiny fragment onto the previous piece (no 1-word synths)
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def speak(text: str) -> None:
    text = _shorten(text)
    if not text:
        return
    t = time.monotonic()
    first = True
    for clause in _clauses(text):
        if _hearing or _muted:                 # barged in / muted between clauses → drop the rest
            break
        samples, sr = _kok.create(clause, voice=VOICE, speed=1.0, lang="en-us")
        if _hearing or _muted:                 # ...or during this clause's synth → drop it, don't enqueue
            break
        if samples is None or len(samples) == 0:
            continue
        _enqueue_audio(_to_int16(samples))      # Kokoro is already STREAM_RATE mono → no resample
        if first:
            log(f"first audio {time.monotonic() - t:.1f}s — {text[:50]!r}")
            first = False
    while True:                                 # drain (keeps _pending bounded so barge-in drops only this)
        with _audio_lock:
            if not _pending:
                break
        time.sleep(0.05)


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
    """Presence went stale → your voice stopped being confirmed. Clear _hearing so a held reply can start and
    barge-in releases. (The heard-you cue no longer fires here — it's on the 'claimed' event now, in lockstep
    with the screen's working indicator.)"""
    global _hearing
    try:
        await asyncio.sleep(delay)
        _hearing = False
    except asyncio.CancelledError:
        pass


def on_presence(payload):
    """Track 'hearing Jared' from voice_presence; each fresh confirm restarts the timer that clears _hearing
    once you stop. _hearing gates 'don't start a reply while you're talking' and drives barge-in. (The
    heard-you cue is NOT here — it's on the 'claimed' event, synced to the screen.)"""
    global _hearing, _hearing_clear
    rec = _rec(payload)
    hj = rec.get("heard_jared_at")
    try:
        fresh = bool(hj) and (datetime.now(timezone.utc) - datetime.fromisoformat(hj)).total_seconds() <= 1.5
    except Exception:
        fresh = False
    if not fresh:
        return
    _hearing = True             # set BEFORE the barge-in clear so a mid-synth reply reliably sees it and stops
    _stop_speech()              # barge-in: you're talking → drop any reply audio queued right now
    if _hearing_clear:
        _hearing_clear.cancel()
    _hearing_clear = asyncio.create_task(_clear_hearing())


def _speaker_loop():
    """Speak queued replies in order. Never START one while you're talking / push-to-talking — hold until
    you're done — so a reply can't begin over your next command. Barge-in cuts off the one that's playing."""
    while True:
        _speak_event.wait()
        with _speak_lock:
            item = _speak_queue.popleft() if _speak_queue else None
            if not _speak_queue:
                _speak_event.clear()
        if not item:
            continue
        kind, payload = item
        t0 = time.monotonic()
        while _hearing or _muted:      # hold until you've stopped talking / released push-to-talk
            time.sleep(0.1)
        held = time.monotonic() - t0   # tail part 2: hold (invisible until now) — reply waiting on you/mute
        if held > 0.15:
            log(f"reply HELD {held:.1f}s (you were talking / push-to-talk) before playing")
        if kind == "pcm":
            _play_pcm(payload)         # already synthesized (on the faster Mac) → just stream it
        else:
            speak(payload)             # synth on the Pi (fallback path); 'first audio Xs' = first clause


def on_speech(payload):
    rec = _rec(payload)
    text = rec.get("text") or ""
    if text and _toggles.get("audio_spoken_reply"):
        lat = ""
        ca = rec.get("created_at")    # tail part 1: realtime delivery (reply written → we got it)
        if ca:
            try:
                lat = f" (delivery {(datetime.now(timezone.utc) - datetime.fromisoformat(ca)).total_seconds():.1f}s)"
            except Exception:  # noqa: BLE001
                pass
        log(f"reply queued{lat} — {text[:40]!r}")
        with _speak_lock:
            _speak_queue.append(("text", text))   # FIFO — play in order, don't drop
        _speak_event.set()


def on_request(payload):
    """Working lamp = "a command of mine is in flight": amber from the moment a capture is ACCEPTED
    (the capturer inserts the row as 'transcribing', before STT even runs) through pending/claimed,
    restored to the lamp's prior state on 'done'/'error'. (The heard-you POP is no longer fired here —
    the UI/browser plays it itself, synced to the moment it paints your words. See status-hub tv.js.)"""
    global _lamp_working
    status = str((_rec(payload)).get("status") or "")
    if status in ("transcribing", "pending", "claimed"):
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
            ch.on_postgres_changes("INSERT", callback=on_request, schema="public", table="voice_requests")
            ch.on_postgres_changes("INSERT", callback=on_speech, schema="public", table="voice_speech")
            ch.on_postgres_changes("UPDATE", callback=on_setting, schema="public", table="app_settings")
            await ch.subscribe()
            log("subscribed (voice_presence → hearing, voice_requests lifecycle → lamp, voice_speech → TTS, app_settings → toggles)")
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
    threading.Thread(target=_stream_loop, daemon=True).start()   # the always-open warm output (no cold clip)
    threading.Thread(target=_play_http_server, daemon=True).start()  # accept pre-synth'd PCM (synth-on-Mac)
    threading.Thread(target=_speaker_loop, daemon=True).start()   # speaks only the latest reply
    if LAMP_ENABLED:
        threading.Thread(target=_lamp_worker, daemon=True).start()   # 'working' floor lamp (amber)
        ip = _lamp_scan()
        log(f"floor lamp {'at ' + ip if ip else 'not found on LAN yet (will rescan on first event)'}")
        _lamp_render()                                              # settle to idle (off) at boot
    log(f"ready — toggles {_toggles}, sink {PW_SINK}")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
