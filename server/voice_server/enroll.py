"""Voiceprint enrollment from THIS mic (/enroll-start).

The original exemplars were recorded on the Mac mic; matching ESP far-field audio against them is a
cross-device comparison that costs similarity. Enroll mode collects exemplars from the ESP itself:
while armed, each energy-endpointed utterance is embedded (same trim+normalize as verification) and
appended to the shared bank in app_settings.voiceprint — so both devices' channels are represented
and top-3 matching picks whichever exemplars fit the capturing mic. Speaker gating is bypassed while
armed (you can't gate on the voiceprint you're rebuilding). LED: blink = armed, solid flash = phrase
accepted, off = done. IMPORTANT: only arm this with other voices (TV!) silenced — it enrolls whoever talks."""

import asyncio
import base64
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import numpy as np

from . import db, led, voiceid
from .audio import rms

log = logging.getLogger("voice")


@dataclass
class Enrollment:
    left: int = 0                              # phrases still to collect (0 = enroll mode off)
    new: list = field(default_factory=list)    # embeddings collected this run


state = Enrollment()


def _upload_voiceprint(bank) -> bool:
    """Write the merged exemplar bank back to app_settings.voiceprint — the shared source of truth every
    mic loads from. Blocking."""
    if not db.configured():
        return False
    buf = io.BytesIO()
    np.save(buf, bank.astype(np.float32))
    try:
        r = httpx.patch(db.url("/rest/v1/app_settings?key=eq.voiceprint"), headers=db.headers("return=minimal"),
                        json={"value": base64.b64encode(buf.getvalue()).decode(),
                              "updated_at": datetime.now(timezone.utc).isoformat()},
                        timeout=10)
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.exception("voiceprint upload failed: %s", e)
        return False


async def sample(pcm_i16, floor_raw: float, ws) -> None:
    """Handle one energy-endpointed utterance while enroll mode is armed: embed it, add it to the bank
    when the last phrase lands, and flash the LED so the speaker gets per-phrase confirmation at the device."""
    utt_rms = rms(pcm_i16)
    if utt_rms < max(floor_raw * 2.0, 200.0):
        log.info("enroll: sample too quiet (rms=%.0f, floor=%.0f) — speak closer; not counted", utt_rms, floor_raw)
        return
    e = await asyncio.to_thread(voiceid.embed, pcm_i16, 1.0)   # ≥1s of voiced audio for a stable exemplar
    if e is None:
        log.info("enroll: not enough voiced audio — not counted")
        return
    bank = voiceid.state.bank
    if bank is not None:
        sims = bank @ e
        log.info("enroll: phrase accepted (sim vs old bank: %.2f)", float(np.sort(sims)[-min(3, len(sims)):].mean()))
    else:
        log.info("enroll: phrase accepted")
    state.new.append(e)
    state.left -= 1
    if ws is not None:      # solid flash = 'got it', then back to the armed blink
        try:
            await ws.send("LED_ON")
            await asyncio.sleep(0.4)
            await led.render(ws, "enroll phrase accepted")
        except Exception:  # noqa: BLE001
            pass
    if state.left > 0:
        log.info("enroll: %d phrase(s) to go", state.left)
        return
    new = np.vstack(state.new).astype(np.float32)
    bank = voiceid.state.bank
    prev_shape = None if bank is None else bank.shape
    merged = new if bank is None else np.vstack([bank, new]).astype(np.float32)
    voiceid.state.bank = merged
    if _upload_voiceprint(merged):
        log.info("enroll: DONE — bank now %s (was %s; +%d from this mic), uploaded to app_settings",
                 merged.shape, prev_shape, new.shape[0])
    else:
        log.warning("enroll: upload FAILED — new exemplars kept in memory only for this process")
    state.new = []
    if ws is not None:
        await led.render(ws, "enroll done")
