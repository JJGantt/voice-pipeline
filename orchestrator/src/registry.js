// Session registry publisher. Every REGISTRY_POLL_MS it scans the projects dir,
// keeps the 5 most-recent sessions, upserts them into voice_sessions, and prunes
// any rows that are no longer in the top-5 (table stays == current set).
import { config } from './config.js';
import { scanRegistry } from './sessions.js';
import { upsertSessions, pruneSessions } from './supabase.js';

let timer = null;
let running = false;

async function tick() {
  if (running) return;
  running = true;
  try {
    const rows = await scanRegistry(config.REGISTRY_LIMIT);
    await upsertSessions(rows);
    await pruneSessions(rows.map((r) => r.id));
  } catch (e) {
    console.error('[registry] error:', e.message);
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
