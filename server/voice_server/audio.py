"""PCM helpers: the stream format, WAV packing for the transcriber, and level/quality measurements."""

import io
import wave

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1280          # 80 ms @ 16 kHz — openWakeWord's expected chunk


def pcm_to_wav(pcm: bytes) -> bytes:
    # Peak-normalize to -1 dBFS headroom: utterances are stored un-gained (the mic runs quiet, ~-14 dBFS
    # peaks), so bring the level up for Whisper WITHOUT the clipping the old stored-gain caused. Skip
    # near-silence (peak ≤ ~1% FS) — normalizing an empty capture would just amplify room noise.
    arr = np.frombuffer(pcm, dtype=np.int16)
    peak = int(np.abs(arr).max()) if arr.size else 0
    if peak > 327:
        arr = (arr.astype(np.float32) * (0.9 * 32767.0 / peak)).astype(np.int16)
        pcm = arr.tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def audio_quality(a: np.ndarray, wall_s: float, seed_s: float) -> str:
    """Cheap per-utterance quality scalars for the endpoint line.

    rms+peak alone cannot separate clean speech from a DC-offset mic, a dead high end (bad clock /
    wrong I2S bit alignment), hum, or a stream full of dropouts — all of which still transcribe
    "well enough" while quietly degrading the ECAPA match. These four numbers do separate them:
      clip   samples at/near the int16 rail — >0 means acoustic overload, the >>12 shift is too hot
      dc     mean sample value; a healthy AC-coupled mic sits at ~0, a misconfigured one does not
      hf     fraction of energy above 4 kHz; speech carries a few %, ~0 = muffled/dead high end,
             very high = hiss or whine rather than voice
      stream received audio vs wall-clock; <100% means frames never arrived (WiFi dropouts), which
             is invisible in an averaged RMS
    """
    if a.size == 0:
        return "clip=0 dc=0 hf=0.000 stream=—"
    f = a.astype(np.float32)
    clipped = int(np.count_nonzero(np.abs(a) >= 32700))
    dc = float(f.mean())
    spec = np.abs(np.fft.rfft(f * np.hanning(f.size))) ** 2
    freqs = np.fft.rfftfreq(f.size, 1.0 / SAMPLE_RATE)
    total = float(spec.sum()) or 1.0
    hf = float(spec[freqs >= 4000].sum() / total)
    # Pre-roll was buffered before the trigger, so it isn't part of the wall-clock window.
    got = a.size / SAMPLE_RATE - seed_s
    stream = f"{100.0 * got / wall_s:.0f}%" if wall_s > 0.05 else "—"
    return f"clip={clipped} dc={dc:.0f} hf={hf:.3f} stream={stream}"


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
