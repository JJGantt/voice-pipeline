// A session started from the hub conversation answers back into it — once, with its first finished
// reply. After that it is a session like any other: the user reads it where it runs and talks to it with
// a follow-up. Nothing later is relayed, so a long job cannot flood the thread.
//
// Which sessions: those whose desk_commands payload carries `relay`. data-layer's start_session sets it
// only when the data MCP was launched by the voice worker, so a session some other agent dispatched
// stays out of the hub thread.
//
// Once: voice_messages.session_id is unique (data-layer 00100) and the insert ignores a duplicate, so
// both paths that notice a finished turn — the transcript watcher and the reconcile tick, on either
// host — can call this freely.
import { config } from './config.js';

// Sessions already relayed, or known not to be hub dispatches. Only a cache of lookups: after a restart
// each running session costs one read again, and the unique column keeps a second insert harmless.
const settled = new Set();

async function rest(path, init = {}) {
  const r = await fetch(`${config.SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: { apikey: config.SUPABASE_KEY, Authorization: `Bearer ${config.SUPABASE_KEY}`,
               'Content-Type': 'application/json', ...init.headers },
  });
  if (!r.ok) throw new Error(`${path.split('?')[0]} ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

/** Post a hub-dispatched session's reply into the hub thread, if it has not been already. */
export async function relayFirstReply(sessionId, tail) {
  if (!tail?.lastReply || settled.has(sessionId)) return;
  const [s] = await rest(`desk_sessions?id=eq.${encodeURIComponent(sessionId)}&select=command_id,label,custom_title`);
  if (!s) return;   // not published yet; the next tick finds it
  const [c] = s.command_id
    ? await rest(`desk_commands?id=eq.${encodeURIComponent(s.command_id)}&select=payload`)
    : [];
  if (!c?.payload?.relay) { settled.add(sessionId); return; }
  const label = s.custom_title || s.label || c.payload.label || null;
  const text = tail.lastReply.slice(0, 20000);
  const rows = await rest('voice_messages?on_conflict=session_id', {
    method: 'POST',
    headers: { Prefer: 'resolution=ignore-duplicates,return=representation' },
    body: JSON.stringify([{ focus: 'hub', mode: 'hub', role: 'assistant', done: true, text,
                            session_id: sessionId, session_label: label }]),
  });
  settled.add(sessionId);
  // No notification here: the hub pushes every session's finished reply, this one included, keyed by
  // turn and carrying the session to open (status-hub /api/session-activity). A second push from here
  // would buzz twice for one reply.
}
