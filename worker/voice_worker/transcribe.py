"""The last transcriber: the Mac's local Whisper for clips every cloud provider failed on. Not a copy of the
capturers' transcribers: it re-runs a stored clip from the voice-clips bucket after they (or the hub) gave up."""

import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta

import httpx

from .config import SUPABASE_KEY, SUPABASE_URL, UNTRANSCRIBED_MAX_S, WORKER_ID, _NOTHING_SAID
from .logs import alert_failure, log, log_error, now_iso
from .db import report_failure


# THE LAST TRANSCRIBER. When Groq and OpenAI both fail, the hub keeps the clip and queues the row
# 'untranscribed' (status-hub /api/voice-command) — but only while this Mac's worker is up, since it is
# the only host with a local Whisper. Taken as its realtime UPDATE lands (the one that attaches `audio`),
# and swept once when the socket subscribes, so a row queued while the socket was down is not missed.
WHISPER_CLI = os.path.expanduser(os.environ.get("WHISPER_CLI", "~/whisper.cpp/build/bin/whisper-cli"))


WHISPER_MODEL = os.path.expanduser(os.environ.get("WHISPER_MODEL", "~/whisper.cpp/models/ggml-small.en.bin"))


def local_whisper_ready() -> bool:
    return WORKER_ID == "mac" and os.access(WHISPER_CLI, os.X_OK) and os.path.exists(WHISPER_MODEL)


def transcribe_local(rec: dict) -> None:
    """Take one 'untranscribed' row: download its clip, run it through local Whisper, and hand it to the
    workers as 'pending'. The take is an atomic untranscribed→transcribing PATCH, so it runs once."""
    rid, audio = rec.get("id"), rec.get("audio") or {}
    if not (rid and audio.get("path")) or not local_whisper_ready():
        return
    hdr = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    url = f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}"
    try:
        got = httpx.patch(url + "&status=eq.untranscribed", json={"status": "transcribing", "updated_at": now_iso()},
                          headers={**hdr, "Content-Type": "application/json", "Prefer": "return=representation"},
                          timeout=10).json()
        if not got:
            return
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as d:
            src, wav = os.path.join(d, "clip"), os.path.join(d, "clip.wav")
            r = httpx.get(f"{SUPABASE_URL}/storage/v1/object/voice-clips/{audio['path']}", headers=hdr, timeout=30)
            r.raise_for_status()
            open(src, "wb").write(r.content)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", wav],
                           check=True, timeout=60)
            out = subprocess.run([WHISPER_CLI, "-m", WHISPER_MODEL, "-f", wav, "-l", "en", "--no-prints", "-nt"],
                                 check=True, timeout=120, capture_output=True, text=True).stdout
        text = " ".join(out.split()).strip()
        if not text or _NOTHING_SAID.match(text):
            body = {"status": "error", "error": "nothing heard (local Whisper)", "updated_at": now_iso()}
        else:
            body = {"transcript": text, "status": "pending", "transcribed_at": now_iso(), "updated_at": now_iso()}
        httpx.patch(url + "&status=eq.transcribing", json=body,
                    headers={**hdr, "Content-Type": "application/json"}, timeout=10).raise_for_status()
        log(f"local Whisper transcribed {rid} in {time.monotonic() - t0:.1f}s: {text[:80]!r}")
    except Exception as e:  # noqa: BLE001
        log_error(f"local Whisper failed for {rid}: {e}")
        try:
            httpx.patch(url + "&status=eq.transcribing", json={"status": "error", "error": f"local Whisper failed: {e}"[:300],
                        "updated_at": now_iso()}, headers={**hdr, "Content-Type": "application/json"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        alert_failure("transcription", f"local Whisper failed: {e}",
                      f"Request {rid}: Groq and OpenAI had both failed, and the Mac's local Whisper failed too.")


def sweep_untranscribed() -> None:
    """On subscribe (Mac only): take any queued clip the socket missed while it was down."""
    if not local_whisper_ready():
        return
    try:
        since = (datetime.now(timezone.utc) - timedelta(seconds=UNTRANSCRIBED_MAX_S)).isoformat()
        rows = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_requests",
                         params={"status": "eq.untranscribed", "audio": "not.is.null", "created_at": f"gte.{since}",
                                 "select": "id,audio"},
                         headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10).json()
        for r in rows or []:
            threading.Thread(target=transcribe_local, args=(r,), daemon=True).start()
    except Exception as e:  # noqa: BLE001
        report_failure(f"untranscribed sweep failed: {e}", e)
