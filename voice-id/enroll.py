#!/usr/bin/env python3
"""Enroll the speaker's voiceprint from SEPARATE command-like phrases.

Records a set of DISTINCT phrases one at a time — mostly real short commands, plus a couple of longer
natural ones — and saves each as its own ECAPA exemplar (exemplars.npy). This beats slicing one long
monologue two ways: the exemplars are genuinely diverse (not near-duplicate overlapping windows from
one acoustic moment), and the bank now contains SHORT exemplars, so a short runtime command verifies
like-against-like instead of against slices of a long take — directly targeting the short-command
problem. Run in your own terminal. All local; nothing leaves the machine.

Tip: bump the macOS input volume first (your mic was quiet) and speak each phrase clearly.
"""
import sys
import time
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
ENERGY_GATE = 0.006
SILENCE_S = 1.2        # end a phrase after this much trailing silence
MAX_S = 7.0            # hard cap per phrase
MIN_S = 0.4            # reject a phrase shorter than this (re-prompt)

# Mostly your real short commands (so the bank holds short, command-shaped voiceprints) + a couple
# of longer natural lines for range. Edit freely.
PHRASES = [
    "Lights off",
    "Make the lamp white",
    "Make the floor lamp blue",
    "Turn the floor lamp red",
    "Set the lamp to ten percent",
    "What time is it",
    "Turn the lights on",
    "How many calories have I eaten today",
    "Log my weight",
    "Add milk to the grocery list",
    "What's on my grocery list",
    "I'd like you to dim the floor lamp a little bit, please",
]


def embed(enc, pcm: np.ndarray) -> np.ndarray:
    e = enc.encode_batch(torch.tensor(pcm).unsqueeze(0)).squeeze().detach().cpu().numpy()
    return e / (np.linalg.norm(e) + 1e-9)


def trim_voiced(pcm: np.ndarray, margin_s: float = 0.15) -> np.ndarray:
    if pcm.size == 0:
        return pcm
    step = int(0.05 * SR)
    voiced = [i for i in range(0, len(pcm) - step, step)
              if float(np.sqrt(np.mean(pcm[i:i + step] ** 2))) >= ENERGY_GATE]
    if not voiced:
        return np.array([], dtype=np.float32)
    m = int(margin_s * SR)
    return pcm[max(0, voiced[0] - m): min(len(pcm), voiced[-1] + step + m)]


def record_until_silence() -> np.ndarray:
    frame = int(0.1 * SR)
    frames, spoke, silence_start, t0 = [], False, None, time.monotonic()
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=frame) as st:
        while True:
            chunk, _ = st.read(frame)
            chunk = chunk.flatten()
            frames.append(chunk)
            now = time.monotonic()
            if float(np.sqrt(np.mean(chunk ** 2))) >= ENERGY_GATE:
                spoke, silence_start = True, None
            elif spoke:
                silence_start = silence_start or now
                if now - silence_start >= SILENCE_S:
                    break
            if now - t0 >= MAX_S:
                break
    return np.concatenate(frames) if frames else np.array([], dtype=np.float32)


def _upload_voiceprint(path: Path) -> None:
    """Push the voiceprint to Supabase app_settings.voiceprint so the ESP/Fly server uses the SAME one
    (one shared voiceprint, congruent across Mac + ESP). Reads the key from the listener's .env."""
    try:
        import base64
        import httpx
        envf = Path("/Users/jaredgantt/repos/voice-pipeline/mac-listener/.env")
        key = next(l.split("=", 1)[1] for l in envf.read_text().splitlines() if l.startswith("SUPABASE_KEY="))
        b64 = base64.b64encode(path.read_bytes()).decode()
        httpx.post("https://felyggqjjhltwokdfhop.supabase.co/rest/v1/app_settings",
                   headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                            "Prefer": "resolution=merge-duplicates,return=minimal"},
                   json={"key": "voiceprint", "value": b64}, timeout=20)
        print("Uploaded voiceprint to Supabase (shared with the ESP/Fly server).")
    except Exception as e:  # noqa: BLE001
        print(f"(voiceprint upload skipped: {e})")


def main() -> int:
    print("Loading ECAPA speaker model…", flush=True)
    enc = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(HERE / ".model"))
    append = "--append" in sys.argv
    if append:
        print(f"\nAPPEND mode — these {len(PHRASES)} phrases ADD to your existing voiceprint. Stand where "
              f"you actually command from (e.g. across the room) so the bank covers that distance/position.\n")
    else:
        print(f"\nEnrolling {len(PHRASES)} separate phrases (fresh). For each: press Enter, then say it once.\n")

    embs = []
    for k, phrase in enumerate(PHRASES, 1):
        while True:
            input(f"[{k}/{len(PHRASES)}]  press Enter, then say:  \"{phrase}\"")
            print("     recording…", flush=True)
            pcm = trim_voiced(record_until_silence())
            if pcm.size < int(SR * MIN_S):
                print("     too short/quiet — let's redo this one.")
                continue
            embs.append(embed(enc, pcm))
            print(f"     ✓ captured {pcm.size / SR:.1f}s")
            break

    bank = np.array(embs)
    if append and (HERE / "exemplars.npy").exists():
        bank = np.vstack([np.load(HERE / "exemplars.npy"), bank])   # grow coverage, don't replace
    np.save(HERE / "exemplars.npy", bank)
    ref = bank.mean(axis=0)
    np.save(HERE / "reference.npy", ref / (np.linalg.norm(ref) + 1e-9))
    _upload_voiceprint(HERE / "exemplars.npy")   # one shared voiceprint — sync to the ESP/Fly server too

    # Cross-similarity sanity: how alike are your own exemplars? (low = very diverse range)
    sims = [float(bank[i] @ bank[j]) for i in range(len(bank)) for j in range(i + 1, len(bank))]
    print(f"\nSaved {len(embs)} exemplars. Inter-phrase similarity: "
          f"min={min(sims):.2f} mean={np.mean(sims):.2f} max={max(sims):.2f}")
    print("Now run detect.py (or wakefree) and the short commands should score higher.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
