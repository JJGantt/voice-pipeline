#!/usr/bin/env python3
"""Mac synth server — warm Kokoro (PyTorch/Metal) that turns reply text into PCM fast.

The Pi's CPU ONNX Kokoro takes ~2s even for two words; the Mac's PyTorch Kokoro does the same in ~0.5s
(RTF ~0.25 vs ~1.3). So when synth-on-Mac is enabled, the worker (which already produced the reply on the
Mac) POSTs the reply text here, gets back raw PCM, and ships it to the Pi to play — cutting the response→
voice tail to a fraction. The model is loaded ONCE and kept warm; this process just answers /synth.

Output is exactly what the Pi's warm stream wants: raw little-endian int16, MONO, 24000 Hz — no resample
on either end. Runs in ~/voice/mac/.venv-kokoro (which already has `kokoro` + torch). Localhost only.

  POST /synth   body = UTF-8 reply text         → 200, body = raw int16 LE mono 24k PCM
  GET  /health                                  → 200 "ok"
"""
import os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import numpy as np

RATE = 24000                                   # Kokoro's native rate == the Pi warm stream's rate
VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
HOST = os.environ.get("SYNTH_HOST", "127.0.0.1")
PORT = int(os.environ.get("SYNTH_PORT", "8731"))

_pipe = None


def log(m): print(f"{time.strftime('%H:%M:%S')} [mac-synth] {m}", flush=True)


def _to_pcm(text: str) -> bytes:
    """Synthesize text → raw int16 LE mono PCM. Concatenated across Kokoro's internal chunks."""
    chunks = []
    for _gs, _ps, audio in _pipe(text, voice=VOICE):
        a = np.asarray(audio, dtype=np.float32)
        chunks.append((np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2"))
    if not chunks:
        return b""
    return np.concatenate(chunks).tobytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # silence the default per-request stderr spam
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != "/synth":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            text = self.rfile.read(n).decode("utf-8", "replace").strip()
            t = time.monotonic()
            pcm = _to_pcm(text) if text else b""
            log(f"synth {time.monotonic() - t:.2f}s → {len(pcm)} bytes ({len(pcm)/2/RATE:.1f}s audio) — {text[:50]!r}")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(pcm)))
            self.end_headers()
            self.wfile.write(pcm)
        except Exception as e:  # noqa: BLE001
            log(f"synth error: {e}")
            try:
                self.send_response(500); self.end_headers()
            except Exception:  # noqa: BLE001
                pass


def main():
    global _pipe
    log("loading Kokoro (PyTorch)…")
    from kokoro import KPipeline
    _pipe = KPipeline(lang_code="a")           # American English
    try:
        list(_pipe("ready", voice=VOICE))      # warm the model so the first real synth is fast
    except Exception as e:  # noqa: BLE001
        log(f"warmup failed: {e}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"ready on http://{HOST}:{PORT}  (voice={VOICE}, {RATE} Hz mono int16)")
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
