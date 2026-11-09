// Session registry publisher. Every REGISTRY_POLL_MS it scans the projects dir,
// keeps the 5 most-recent sessions, upserts them into voice_sessions, and prunes
// any rows that are no longer in the top-5 (table stays == current set).
// Writes are change-gated: the scan is local and free, but the upsert+prune round-trip isn't —
// unconditional ticks were ~4k Supabase writes/day for a table that rarely changes. A write happens
// only when the set/order/title/cwd changed, or when a session's last_active drifted >60s (an ACTIVE
// session's file mtime moves every few seconds; minute-level freshness is plenty for the picker).
import { config } from './config.js';
import { scanRegistry } from './sessions.js';
import { upsertSessions, pruneSessions } from './supabase.js';
import { streak } from './streak.js';

let timer = null;
let running = false;
let lastWritten = null; // rows as last successfully pushed
// A failed write is re-made by the next tick (lastWritten stays behind), so a blip is a warning.
const writeStreak = streak('[registry] write');

function needsWrite(rows) {
  if (!lastWritten || rows.length !== lastWritten.length) return true;
  for (let i = 0; i < rows.length; i++) {
    const a = rows[i], b = lastWritten[i];
    if (a.id !== b.id || a.title !== b.title || a.cwd !== b.cwd || a.project !== b.project) return true;
    if (Math.abs(Date.parse(a.last_active) - Date.parse(b.last_active)) > 60000) return true;
  }
  return false;
}

async function tick() {
  if (running) return;
  running = true;
  try {
    const rows = await scanRegistry(config.REGISTRY_LIMIT);
    if (needsWrite(rows)) {
      await upsertSessions(rows);
      await pruneSessions(rows.map((r) => r.id));
      lastWritten = rows;
    }
    writeStreak.ok();
  } catch (e) {
    writeStreak.fail(e);
  } finally {
    running = false;
  }
}

export function startRegistry() {
  tick();
  timer = setInterval(tick, config.REGISTRY_POLL_MS);
  return () => {
    if (timer) clearInterval(timer);
  };
}
