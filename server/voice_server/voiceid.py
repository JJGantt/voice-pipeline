"""Speaker ID: the ECAPA model and the shared voiceprint (app_settings.voiceprint), loaded lazily when
wake-free, the governor brake's sensor, enrollment or /verify first needs them. Every function that
encodes is blocking: call it through asyncio.to_thread from the event loop."""

import asyncio
import base64
import io
import logging
import time
from dataclasses import dataclass

import numpy as np

from . import db
from .audio import SAMPLE_RATE
from endpointing import (  # copied into the build context by assemble.sh (shared/endpointing.py)
    VAD_WINDOW_S, BACKFILL_MAX_S, BACKFILL_STEP_S, BACKFILL_THR, ACCEPT_THRESHOLD as ACCEPT_THR_DEFAULT,
)

log = logging.getLogger("voice")


@dataclass
class VoiceId:
    enc: object = None        # ECAPA model (lazy)
    bank: np.ndarray | None = None  # voiceprint: (N, 192) exemplar embeddings from app_settings.voiceprint
    thresh: float = 0.30      # confirm bar (app_settings.voiceid_threshold)
    accept: float = 0.22      # whole-clip bar at capture-accept (app_settings.voiceid_accept_threshold)
    pause: float = 2.0        # wake-free energy-endpoint hangover (app_settings.wakefree_pause_s)


state = VoiceId()


def ready() -> bool:
    return state.enc is not None and state.bank is not None


def load():
    """Load ECAPA + the shared voiceprint + thresholds. Blocking (model load and a settings read)."""
    import torch
    # On this shared 1-vCPU machine, torch's default thread pool oversubscribes the single core it
    # actually gets scheduled on, so a single ECAPA encode thrashes and balloons from ~0.2s to 7-10s.
    # That blows past the 3.5s no-speaker abort window, so real commands drop as noise before ECAPA can
    # confirm you. Pin to one thread — a single small inference is FASTER single-threaded here (no
    # coordination overhead), and the latency stays stable inside the confirm window.
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)   # can only be set once, before any inter-op work starts
    except RuntimeError:
        pass
    from speechbrain.inference.speaker import EncoderClassifier
    if state.enc is None:
        state.enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir="/app/.ecapa")
    s = db.get_settings(["voiceprint", "voiceid_threshold", "voiceid_accept_threshold", "wakefree_pause_s"])
    state.bank = np.load(io.BytesIO(base64.b64decode(s["voiceprint"]))) if s.get("voiceprint") else None
    state.thresh = float(s.get("voiceid_threshold") or 0.30)
    state.accept = float(s.get("voiceid_accept_threshold") or ACCEPT_THR_DEFAULT)
    state.pause = float(s.get("wakefree_pause_s") or 2.0)
    log.info("wake-free loaded: voiceprint=%s thresh=%.2f accept=%.2f pause=%.1fs",
             None if state.bank is None else state.bank.shape, state.thresh, state.accept, state.pause)


def load_bg() -> None:
    """Load ECAPA + the voiceprint OFF the event loop, so it never blocks the ESP websocket."""
    try:
        asyncio.get_running_loop().run_in_executor(None, load)
    except RuntimeError:
        load()


def _trim_voiced(wav, gate=0.006, margin_s=0.15):
    step = int(0.05 * SAMPLE_RATE)
    v = [i for i in range(0, len(wav) - step, step) if float(np.sqrt(np.mean(wav[i:i + step] ** 2))) >= gate]
    if not v:
        return np.array([], dtype=np.float32)
    m = int(margin_s * SAMPLE_RATE)
    return wav[max(0, v[0] - m): min(len(wav), v[-1] + step + m)]


def embed(pcm_i16, min_voiced_s: float = 0.4):
    """Trim → peak-normalize → ECAPA-embed an utterance. Returns a unit-norm (192,) embedding, or None
    if the model isn't loaded / not enough voiced audio. Shared by verification AND enrollment so both
    sides of every comparison go through the identical pipeline."""
    if state.enc is None:
        return None
    import torch
    wav = _trim_voiced(pcm_i16.astype(np.float32) / 32768.0)
    if wav.size < int(min_voiced_s * SAMPLE_RATE):
        return None
    # Peak-normalize before encoding: the embedding then doesn't depend on mic distance / device gain,
    # so ESP far-field checks land on the same scale as the enrolled exemplars. Skip near-silence
    # (peak ≤ 0.01 ≈ -40 dBFS) — boosting pure room noise 90x would just embed the noise.
    peak = float(np.abs(wav).max())
    if peak > 0.01:
        wav = wav * (0.9 / peak)
    _t = time.monotonic()
    e = state.enc.encode_batch(torch.tensor(wav).unsqueeze(0)).squeeze().detach().cpu().numpy()
    ms = (time.monotonic() - _t) * 1000
    if ms > 1000:   # normal is ~0.2s; seconds means the shared vCPU is thrashing and checks will miss the abort window
        log.warning("ECAPA encode SLOW: %.0fms for %.1fs voiced audio", ms, wav.size / SAMPLE_RATE)
    return e / (np.linalg.norm(e) + 1e-9)


def verify(pcm_i16):
    """Top-3 cosine of the trimmed utterance vs the voiceprint. 0 if no model/voiceprint loaded."""
    bank = state.bank
    if bank is None:
        return 0.0
    e = embed(pcm_i16)
    if e is None:
        return 0.0
    sims = bank @ e
    k = min(3, len(sims))
    return float(np.mean(np.sort(sims)[-k:]))


def backfill(history) -> int:
    """How many samples off the END of `history` (the audio right before a capture's trigger) are still
    the speaker talking. Walks back one BACKFILL_STEP_S at a time, scoring the VAD_WINDOW_S window that ends at
    each step, and stops at the first window that is silence (too little voiced audio → verify
    returns 0) or not the speaker. One ECAPA encode per step, only at capture-accept. Twin of wakefree.py's
    backfill()."""
    step, win, cap = int(BACKFILL_STEP_S * SAMPLE_RATE), int(VAD_WINDOW_S * SAMPLE_RATE), int(BACKFILL_MAX_S * SAMPLE_RATE)
    n, inc = len(history), 0
    while inc + step <= min(n, cap):
        end = n - inc
        if verify(history[max(0, end - win):end]) < BACKFILL_THR:
            break
        inc += step
    return inc


async def check_speaker(window_i16, win_end):
    """Run the (blocking) ECAPA verify off the event loop so frame capture keeps flowing while it runs.
    Returns (similarity, win_end); win_end is the capture time the window closed at, so the caller can
    timestamp 'last confirmed the speaker' precisely rather than at result-collection time."""
    sim = await asyncio.to_thread(verify, window_i16)
    return sim, win_end
