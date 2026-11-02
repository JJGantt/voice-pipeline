#!/usr/bin/env python3
"""Live "is that the enrolled speaker?" detector.

Streams the mic, scores a rolling ~1.2s window against your enrolled voiceprint (reference.npy)
every ~0.5s, and prints a live verdict: ● JARED / ○ not you / · silence, with the cosine similarity
so you can watch the numbers as you talk, then as the TV / someone else talks. Tune THRESHOLD from
what you see. All local.

    python detect.py [threshold]      # default threshold 0.50
"""
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd
import torch

try:
    from speechbrain.inference.speaker import EncoderClassifier
except Exception:  # noqa: BLE001
    from speechbrain.pretrained import EncoderClassifier

HERE = Path(__file__).parent
SR = 16000
WIN_S = 3.0          # scoring window — matches the 3s windows the reference was enrolled from
HOP_S = 0.5          # score this often
ENERGY_GATE = 0.005  # RMS below this = silence (calibrated below the speaker's quietest speech ~0.008)
THRESH = float(sys.argv[1]) if len(sys.argv) > 1 else 0.30
TOPK = 3             # score = mean cosine to the K closest enrollment exemplars (vs a blurred centroid)
REF = HERE / "reference.npy"
EXEMPLARS = HERE / "exemplars.npy"


def score(e: np.ndarray, bank: np.ndarray) -> float:
    """Cosine of window-embedding e against the exemplar bank, averaged over the closest TOPK —
    matching the nearest real enrollment windows gives a sharper, more accurate verdict than a mean."""
    sims = bank @ e                       # both unit-normalized → cosine
    k = min(TOPK, len(sims))
    return float(np.mean(np.sort(sims)[-k:]))


def main() -> int:
    if EXEMPLARS.exists():
        bank = np.load(EXEMPLARS)
    elif REF.exists():
        bank = np.load(REF)[None, :]      # fall back to the single centroid
    else:
        print("No exemplars.npy / reference.npy — run enroll.py first.", file=sys.stderr)
        return 1
    print("Loading ECAPA…", flush=True)
    enc = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(HERE / ".model")
    )
    print(f"Listening (threshold={THRESH}). Talk, then let the TV/others talk. Ctrl-C to stop.\n", flush=True)

    win = int(WIN_S * SR)
    hop = int(HOP_S * SR)
    buf = np.zeros(win, dtype=np.float32)
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=hop) as stream:
        while True:
            chunk, _ = stream.read(hop)
            buf = np.concatenate([buf, chunk.flatten()])[-win:]
            rms = float(np.sqrt(np.mean(buf ** 2)))
            if rms < ENERGY_GATE:
                print(f"\r· silence              rms={rms:.3f}            ", end="", flush=True)
                continue
            e = enc.encode_batch(torch.tensor(buf).unsqueeze(0)).squeeze().detach().cpu().numpy()
            e = e / (np.linalg.norm(e) + 1e-9)
            sim = score(e, bank)
            tag = "● JARED  " if sim >= THRESH else "○ not you"
            print(f"\r{tag}  sim={sim:.2f}  rms={rms:.3f}            ", end="", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
