"""Endpointing for both room mics, in one place.

The ESP (server/server.py, on Fly) and the Mac's wake-free capture (voice-id/wakefree.py) hear the same
sentence, and the worker collapses the two captures into one command by recording-window overlap
(`_duplicate_of_recent`). That only holds while both decide alike where a sentence started and where it
ended. When they disagree the failure is silent and total: the mic that closes early enqueues a fragment,
the fragment is discarded, and the other mic's complete capture is then dropped as its duplicate. So every
value that decides a capture's span lives here, and both import it.

Per-mic values stay in their own files: frame sizes, energy gates, and VAD_FIRST_CHECK_S (the close Mac
mic confirms faster than the far ESP one), which change when a capture is first scored, not its span.

server/assemble.sh copies this file into the jared-voice build context; wakefree.py imports it from here.
"""

# Speaker-gated endpointing: a capture is kept alive on the speaker's VERIFIED presence (rolling ECAPA over the
# trailing window), never a blind energy timeout, so thinking-pauses do not chop a command.
VAD_WINDOW_S = 2.0            # trailing audio scored per check
VAD_CHECK_EVERY_S = 0.7       # re-score cadence while capturing
VAD_NO_JARED_ABORT_S = 3.5    # never confirmed within this much audio: not the speaker, drop

# How many CONSECUTIVE non-confirming checks close a capture: event-based, not a timer that can race the
# check cadence and cut the speaker off between words. It scales with how long the speaker has been talking. A short
# command is one breath, so the first non-confirm ends it; a long dictated one has real thinking-pauses,
# and cutting one in half loses the whole command. Each extra miss buys about VAD_CHECK_EVERY_S of pause.
ABSENT_MISSES_MIN = 1         # short command: close on the first non-confirm (~2s of silence)
ABSENT_MISSES_MAX = 5         # ceiling: past this only the runaway cap stops a capture
ABSENT_RAMP_S = 8.0           # +1 tolerated miss per this much speech (~2.0s of pause at the floor, ~4.8s at
                              # the ceiling: a 50s dictated command survives a 4s thinking-pause)


def absent_misses(audio_s: float) -> int:
    """Consecutive non-confirms needed to close a capture that has run `audio_s` seconds."""
    return min(ABSENT_MISSES_MAX, ABSENT_MISSES_MIN + int(audio_s // ABSENT_RAMP_S))


# Where a sentence STARTED. PREROLL_S is audio kept from before the energy trigger, so a soft first syllable
# that crosses the threshold mid-word is not clipped; it is excluded from the VAD timing above.
PREROLL_S = 1.2
# Back-fill: a capture that never confirms the speaker inside VAD_NO_JARED_ABORT_S is dropped as noise, and the next
# opens on the next frame, mid-sentence. So raw audio from before the trigger is kept (HISTORY_S), and an
# ACCEPTED capture walks backward through it in BACKFILL_STEP_S steps, prepending each VAD_WINDOW_S window
# that still scores as the speaker (BACKFILL_THR, under the confirm bar on purpose: measured on 1,123 windows of the speaker's
# close-mic speech, 0.9% score under 0.22, while TV-only windows top out around 0.18), until silence, another
# voice, or BACKFILL_MAX_S.
HISTORY_S = 8.0
BACKFILL_MAX_S = 5.0          # covers a full noise-abort plus the restart
BACKFILL_STEP_S = 0.5
BACKFILL_THR = 0.22

# Whole-clip re-verify at accept: one more encode on the finished clip, and a clip under the bar is dropped
# before STT. Default for app_settings.voiceid_accept_threshold, which overrides it at boot on both mics.
ACCEPT_THRESHOLD = 0.22
ACCEPT_MAX_S = 15.0           # score at most this much of the clip (the encode is linear in length)
