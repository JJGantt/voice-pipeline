// Focus controller. Watches voice_focus id=1 and reconciles the world:
//   focus = <session id>  -> close that session's desk tab, spawn a pty
//                            running `claude --resume <id>`, advertise terminal_url.
//   focus = 'hub'         -> kill the pty, re-open the desk tab for whatever
//                            was last focused, clear terminal_url.
// A realtime subscription (watchTable, which handles the resubscribe dance) reconciles the instant
// the row changes; polling survives only as a missed-event backstop — 60s while the subscription is
// healthy, FOCUS_POLL_MS when it isn't. The always-2s poll was ~43k Supabase reads/day of unchanged rows.
import { watchTable } from './realtime.js';
import { config, terminalUrl } from './config.js';
import { getFocus, patchFocus } from './supabase.js';
import { cwdFromSessionFile } from './sessions.js';
import { ttyForSession, closeTabForTty, reopenDeskTab } from './applescript.js';
import { startSession, killActive } from './pty.js';

let currentFocus = null; // last focus value we acted on
let lastFocused = null; // { id, cwd } — the session most recently shown on the wall
let timer = null;
let reconciling = false;

export function getLastFocused() {
  return lastFocused;
}

// Apply a transition to `targetFocus`. Idempotent against currentFocus.
async function reconcile(targetFocus) {
  if (targetFocus === currentFocus) return;

  if (targetFocus === 'hub') {
    // Go back to the lightweight hub: tear down the pty, restore the desk tab.
    killActive();
    if (lastFocused) {
      try {
        await reopenDeskTab(lastFocused.cwd, lastFocused.id);
      } catch (e) {
        console.error('[focus] reopenDeskTab failed:', e.message);
      }
    }
    try {
      await patchFocus({ terminal_url: null });
    } catch (e) {
      console.error('[focus] clear terminal_url failed:', e.message);
    }
    currentFocus = 'hub';
    return;
  }

  // targetFocus is a session id -> bring it to the wall.
  const id = targetFocus;

  // Resolve cwd: prefer voice_focus.session_cwd, else read from the file.
  let row;
  try {
    row = await getFocus();
  } catch (e) {
    console.error('[focus] getFocus during reconcile failed:', e.message);
    return; // try again next tick
  }
  let cwd = row && row.session_cwd ? row.session_cwd : null;
  if (!cwd) cwd = await cwdFromSessionFile(id);

  // 1. Close the desk tab holding this session, if we can find a live one.
  try {
    const tty = await ttyForSession(id, cwd);
    if (tty) {
      const closed = await closeTabForTty(tty);
      console.log(`[focus] desk tab for ${id} tty=${tty} closed=${closed}`);
    } else {
      console.log(`[focus] no live desk tab for ${id}; skipping close`);
    }
  } catch (e) {
    console.error('[focus] close desk tab failed:', e.message);
  }

  // 2. Spawn the pty (becomes the active pty, replacing any previous).
  try {
    startSession(id, cwd);
    console.log(`[focus] spawned pty: claude --resume ${id} (cwd=${cwd})`);
  } catch (e) {
    console.error('[focus] startSession failed:', e.message);
    return;
  }

  // 3. Advertise where the TV should connect.
  try {
    await patchFocus({ terminal_url: terminalUrl() });
  } catch (e) {
    console.error('[focus] set terminal_url failed:', e.message);
  }

  lastFocused = { id, cwd };
  currentFocus = id;
}

async function tick() {
  if (reconciling) return;
  reconciling = true;
  try {
    const row = await getFocus();
    const target = row && row.focus ? row.focus : 'hub';
    await reconcile(target);
  } catch (e) {
    console.error('[focus] poll error:', e.message);
  } finally {
    reconciling = false;
  }
}

export function startFocusWatcher() {
  // Initialize currentFocus to whatever's there now WITHOUT acting, so a
  // daemon restart while a session is focused doesn't immediately re-spawn.
  // We simply let the first tick reconcile from a clean slate.
  tick();

  // Realtime: any change to the focus row reconciles immediately. Our own patchFocus writes echo
  // back as events too — harmless, reconcile() is idempotent against currentFocus.
  const rt = watchTable({ channel: 'orch-focus', table: 'voice_focus', onChange: tick, tag: 'focus' });

  // Adaptive backstop: a setTimeout chain (not setInterval) so the cadence follows realtime health.
  const loop = () => {
    tick();
    timer = setTimeout(loop, rt.isHealthy() ? 60000 : config.FOCUS_POLL_MS);
  };
  timer = setTimeout(loop, config.FOCUS_POLL_MS);
  return () => {
    if (timer) clearTimeout(timer);
    rt.stop();
  };
}
