#!/usr/bin/env python3
"""Calibration diagnostic: record ~8s of YOU talking, then print per-window RMS + cosine similarity
to the enrolled reference, plus summary stats and the input device. Tells us the real numbers to set
the energy gate, window length, and threshold from — instead of guessing.

    python diag.py
"""
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
SECS = 8.0
WIN = int(2.0 * SR)
HOP = int(0.5 * SR)

ref = np.load(HERE / "reference.npy")
print("input device:", sd.query_devices(kind="input")["name"])
enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(HERE / ".model"))

print(f"\n>>> Recording {SECS:.0f}s — talk continuously NOW.", flush=True)
audio = sd.rec(int(SECS * SR), samplerate=SR, channels=1, dtype="float32")
sd.wait()
wav = audio.flatten()
print(">>> Done. Per 2.0s window (hop 0.5s):\n")

rmss, sims = [], []
for i in range(0, len(wav) - WIN, HOP):
    seg = wav[i:i + WIN]
    rms = float(np.sqrt(np.mean(seg ** 2)))
    e = enc.encode_batch(torch.tensor(seg).unsqueeze(0)).squeeze().detach().cpu().numpy()
    e = e / (np.linalg.norm(e) + 1e-9)
    sim = float(np.dot(e, ref))
    rmss.append(rms); sims.append(sim)
    print(f"  t={i / SR:4.1f}s   rms={rms:.4f}   sim={sim:.3f}")

print(f"\nRMS:  min={min(rmss):.4f}  median={np.median(rmss):.4f}  max={max(rmss):.4f}")
print(f"SIM:  min={min(sims):.3f}  median={np.median(sims):.3f}  max={max(sims):.3f}")
print(f"peak audio sample abs = {np.max(np.abs(wav)):.3f}  (≈1.0 = clipping, <0.05 = very quiet mic)")
