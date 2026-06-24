# voice-id — speaker identification + wake-free voice

Local speaker verification (is this Jared's voice?) and the **wake-free** capture mode it gates.
Everything runs on-device; no audio leaves the machine. Model: **ECAPA-TDNN**
(`speechbrain/spkrec-ecapa-voxceleb`), CPU, ~20 MB.

## Files

| file | what it does |
|---|---|
| `enroll.py` | Build/extend your voiceprint from **separate command-shaped phrases** (each its own exemplar). `--append` adds more (e.g. a far-away pass) without replacing. |
| `detect.py [thresh]` | Live readout — `● JARED / ○ not you / · silence` with the cosine score. Use it to probe your voice vs. the TV/others and tune the threshold. |
| `wakefree.py` | The wake-free capture loop (run via `run-wakefree.sh`). Started/stopped by the Mac listener on the hub toggle — not launched by hand in normal use. |
| `exemplars.npy` | The voiceprint: an `(N, 192)` bank of per-phrase ECAPA embeddings. `reference.npy` is the averaged centroid (fallback). |
| `.venv/`, `.model/` | Python env (torch + speechbrain + sounddevice) and the cached ECAPA weights. |

## Enrollment

```
.venv/bin/python enroll.py            # fresh — walks you through ~12 phrases (Enter, say each)
.venv/bin/python enroll.py --append   # ADD a pass (e.g. standing across the room) to cover more conditions
```

Why separate phrases beat slicing one long recording: the exemplars are genuinely diverse, and the
bank holds **short** voiceprints, so short runtime commands verify like-against-like. Coverage > count
— ~20–40 samples spanning the positions/distances you actually use, not 100 identical ones. Bump the
macOS mic input volume first; quiet audio scores lower.

## How verification works

A captured utterance is trimmed to its voiced span, embedded, and scored as the **mean cosine to the
top-3 closest exemplars**. `≥ voiceid_threshold` (hub setting, default 0.30) → Jared. Short/distant
utterances carry less speaker signal, so they score lower — the fix is bank coverage (short + far
exemplars) and a permissive threshold (the TV tops out ~0.20, so 0.30 still clears it; the worker's
`__IGNORE__` filter is the backstop for anything ambient that slips through).

## Wake-free flow

`wakefree.py`: energy-segment → ~2s pause (`wakefree_pause_s`) → trim → **verify speaker** → Groq
transcribe → enqueue `voice_requests {source: mac, tier: fast}`. The TV/other people are dropped before
transcription. Toggled by `app_settings.wakefree_enabled`; the Mac listener owns the mic handoff
(push-to-talk > wake-free > wake-word). See `../ARCHITECTURE.md` → "Wake-free mode".

## Settings (hub-controlled, `app_settings`)

- `wakefree_enabled` — on/off (the listener reacts live via realtime).
- `voiceid_threshold` — match threshold (lower = more permissive).
- `wakefree_pause_s` — end-of-command silence.

`wakefree.py` reads the threshold + pause at startup (a toggle off→on re-reads them).
