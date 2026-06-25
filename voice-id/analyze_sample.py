#!/usr/bin/env python3
"""Replay a recorded voice sample through the EXACT wake-free speaker-gated scoring and print a
per-check timeline — so we can SEE where/why it would drop Jared mid-speech. Same voiceprint
(exemplars.npy), same ECAPA, same 2.0s rolling window, 0.7s cadence, threshold + keep-alive
hysteresis, and the consecutive-miss close rule as wakefree.py.

Usage:  ./.venv/bin/python analyze_sample.py [~/voice/sample.wav]
"""
import sys
from pathlib import Path
import numpy as np
import soundfile as sf
import torch
from speechbrain.inference.speaker import EncoderClassifier

HERE = Path(__file__).resolve().parent
SR = 16000
FRAME_S = 0.1
ENERGY_GATE = 0.006
# --- the exact wakefree params under test ---
THRESHOLD = 0.30
KEEPALIVE_MARGIN = 0.16
TOPK = 3
VAD_WINDOW_S = 2.0
VAD_CHECK_EVERY_S = 0.7
VAD_FIRST_CHECK_S = 1.4
ABSENT_MISSES = 2

wav_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("~/voice/sample.wav").expanduser()
audio, sr = sf.read(str(wav_path), dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
if sr != SR:
    import math
    idx = (np.arange(int(len(audio) * SR / sr)) * sr / SR).astype(int)
    audio = audio[np.clip(idx, 0, len(audio) - 1)]
dur = len(audio) / SR
rms_all = float(np.sqrt(np.mean(audio ** 2)))
print(f"sample: {wav_path.name}  {dur:.1f}s  overall_rms={rms_all:.4f}  gate={ENERGY_GATE}")


def trim_voiced(pcm, gate=ENERGY_GATE, margin_s=0.15):
    step = int(0.05 * SR)
    v = [i for i in range(0, len(pcm) - step, step) if float(np.sqrt(np.mean(pcm[i:i + step] ** 2))) >= gate]
    if not v:
        return np.array([], dtype=np.float32)
    m = int(margin_s * SR)
    return pcm[max(0, v[0] - m): min(len(pcm), v[-1] + step + m)]


print("loading ECAPA…")
enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(HERE / ".model"))
bank = np.load(HERE / "exemplars.npy")
print(f"voiceprint bank: {bank.shape}")


def score(seg):
    e = enc.encode_batch(torch.tensor(seg).unsqueeze(0)).squeeze().detach().cpu().numpy()
    e = e / (np.linalg.norm(e) + 1e-9)
    sims = bank @ e
    k = min(TOPK, len(sims))
    return float(np.mean(np.sort(sims)[-k:]))


# Simulate ONE continuous capture starting at the first voiced frame. Mark every point the live
# system WOULD close (consecutive misses), but keep scoring so we see the whole curve.
fr = int(FRAME_S * SR)
energies = np.array([np.sqrt(np.mean(audio[i:i + fr] ** 2)) for i in range(0, len(audio) - fr, fr)])
voiced_frames = np.where(energies >= ENERGY_GATE)[0]
if len(voiced_frames) == 0:
    print("no voiced audio above gate — mic level too low?"); sys.exit(0)
start = voiced_frames[0] * fr
print(f"capture starts at {start/SR:.1f}s\n")
print(f"{'t(s)':>6} {'win_voiced':>10} {'sim':>6} {'thr':>5}  verdict")

miss = 0
jared_seen = False
last_check = -99.0
closes = []
t = VAD_FIRST_CHECK_S
end_s = (len(audio) - start) / SR
while t <= end_s:
    now_sample = start + int(t * SR)
    win_raw = audio[start:now_sample][-int(VAD_WINDOW_S * SR):]
    win = trim_voiced(win_raw)
    vlen = len(win) / SR
    if win.size >= int(0.4 * SR):
        sim = score(win)
        thr = THRESHOLD - (KEEPALIVE_MARGIN if jared_seen else 0.0)
        if sim >= thr:
            miss = 0
            tag = "CONFIRM" + ("  (first lock)" if not jared_seen else "")
            jared_seen = True
        else:
            if jared_seen:
                miss += 1
            tag = f"miss -> {miss}"
        print(f"{t:6.1f} {vlen:9.2f}s {sim:6.2f} {thr:5.2f}  {tag}")
    else:
        if jared_seen:
            miss += 1
        print(f"{t:6.1f} {vlen:9.2f}s {'--':>6} {'':>5}  silent-window -> miss {miss}")
    if jared_seen and miss >= ABSENT_MISSES:
        print(f"       >>> WOULD CLOSE HERE at t={t:.1f}s (consecutive misses={miss}) — cut <<<")
        closes.append(t)
        miss = 0
        jared_seen = False        # simulate restart of the next capture
    t += VAD_CHECK_EVERY_S

print(f"\nclose points: {[f'{c:.1f}s' for c in closes] or 'none — rode the whole sample'}")
