# Open work — voice pipeline

Running list so nothing gets lost between sessions. Companion to [ARCHITECTURE.md](../ARCHITECTURE.md).

---

## Not started

### Dictation leaks into the Mac listener when the always-on brake lifts

Push-to-talk dictation (Hammerspoon) is captured by the Mac's own always-on listener. The ESP is
not the leak: it stays muted for the whole dictation. Diagnosed 2026-09-10 against that day's logs:
3 of 37 dictations leaked (10:04, 16:47, 18:08 local), and all three line up with the brake
releasing partway through the dictation.

**Mechanism:**
- Hammerspoon mutes with a one-shot `pkill -USR1` to whatever `listener.py` / `wakefree.py`
  processes exist at that moment (`~/.hammerspoon/voice.lua:105`).
- The brake releases in `worker/worker.py` (the `wakefree_autooff_at` check) once the cooldown has
  passed and no `voice_presence.heard_jared_at` is newer than `AO_REARM_QUIET_S`.
- Both voice checks are muted during a dictation, so a dictation reads as silence, and the release
  check never looks at `voice_muted`.
- Re-enabling always-on spawns a fresh `wakefree.py`, which starts in the listening state
  (`_resumed.set()`, `voice-id/wakefree.py`). It never received the USR1, so it records the rest of
  the dictation.

**Fix, gated on confirmed state (no timers):**
1. **Dictation is state, not a signal.** Hammerspoon holds a "dictating" state for the whole
   recording, alongside the signals. The listener passes its current mute to every child it spawns
   (wake-free and the sensor), and `wakefree.py` starts muted when a dictation is active. The
   listener's boot reads the real state instead of blindly writing `voice_muted=0`
   (`mac-listener/listener.py`, right after the persisted-toggle reads). `_apply_mute` signals every
   live child, not only while wake-free is on.
2. **The brake never releases while `voice_muted=1`.** Add that to the release check.
   Open question for Jared: should a finished dictation also count as "heard Jared", so the quiet
   window restarts when he stops talking?

**Sibling bugs, same area:**
- A listener self-restart mid-dictation unmutes the ESP through the boot write above.
- The Fly server re-reads always-on on every frame, so a brake landing mid-capture switches that
  capture to plain silence detection and ships it without speaker verification (seen 18:10-18:11
  on 2026-09-10). Latch the mode when a capture starts.
- Leaked dictations are recorded as ignored turns, and those count toward the 3-ignored brake, so
  one leak can help re-trip the brake (it did at 18:10:27).

### Set the confirmed-span accept bar from ESP data — the gate is OFF (row = 0)

The span re-verify at accept (ARCHITECTURE.md, wake-free capture) is deployed on both mics but
`voiceid_accept_threshold` is 0, so it only measures. It went live at 0.22 on 2026-09-10, chosen from
his close-mic recordings and synthetic other voices, and within half an hour dropped a real far-field
ESP command (as a whole clip: 0.20; its confirmed windows: 0.33/0.31/0.27). The scoring was then
changed to the confirmed span, but the bar still has no far-field evidence behind it.

Both mics log `confirmed span sim=…` on every accept. After a few days of ESP-captured commands:
the lowest span score of a REAL command is the floor; any accepted turn that came back `__IGNORE__`
shows what a TV clip scores. Set the row between them (no deploy; the server re-reads it live, the
Mac at its next respawn). If nothing separates them, the lever is enrollment, below.

Second lever, unchanged: enroll far-field exemplars from the ESP itself (`/enroll-start`,
ARCHITECTURE.md) so his voice on that mic scores higher than the TV does.

### The always-on brake kills a confirmed capture mid-flight

2026-09-08 00:04:58 UTC: the brake tripped (three ignored turns) while `wakefree.py` had a confirmed
capture open; `set_wakefree(False)` killed the process, its `voice_requests` row stayed `transcribing`
until the worker's 90s sweep errored it, and the ESP blink + floor lamp stayed on for those 90s. The
row for a capture that was already accepted should be closed by the process itself on the way out, or
the stop should wait for the in-flight capture to finish before the kill.

### `fly logs` for jared-voice holds only about a hundred lines

At the old log rate (three lines per speaker check during TV) that was under a minute of history, so
nothing about the ESP's behaviour during a command a few minutes earlier could be reconstructed. The
per-check lines are now folded into one `sims a/b/c` summary per capture; if history is still too short
in practice, ship the logs somewhere durable.

### Countdown on the red "blocked" state

Jared wants the blocked indicator to show how long until always-on comes back. The release time
exists only inside the worker's check today: `max(brake start + AO_AUTOOFF_COOLDOWN_S, newest
heard_jared_at + AO_REARM_QUIET_S)`, landing up to 60s later because of the check interval.

The worker writes that time to `app_settings` (e.g. `wakefree_rearm_at`) whenever it changes, and
"held" while a dictation holds the brake (fix 2 above). The hub side is in status-hub's
`docs/TODO.md`.
