// Thin Supabase REST helpers using the service_role key. No SDK; plain fetch
// (Node 20+ has global fetch). All calls go through PostgREST.
import { config } from './config.js';

function headers(extra = {}) {
  return {
    apikey: config.SUPABASE_KEY,
    Authorization: `Bearer ${config.SUPABASE_KEY}`,
    'Content-Type': 'application/json',
    ...extra,
  };
}

const base = () => `${config.SUPABASE_URL}/rest/v1`;

// Fetch the single focus row (id=1). Returns the row object or null.
export async function getFocus() {
  const res = await fetch(`${base()}/voice_focus?id=eq.1`, { headers: headers() });
  if (!res.ok) throw new Error(`getFocus ${res.status}: ${await res.text()}`);
  const rows = await res.json();
  return rows[0] || null;
}

// Patch the focus row (id=1) with a partial update, e.g. { terminal_url }.
export async function patchFocus(patch) {
  const res = await fetch(`${base()}/voice_focus?id=eq.1`, {
    method: 'PATCH',
    headers: headers({ Prefer: 'return=minimal' }),
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`patchFocus ${res.status}: ${await res.text()}`);
}

// Upsert session rows (merge-duplicates on id PK).
export async function upsertSessions(rows) {
  if (!rows.length) return;
  const res = await fetch(`${base()}/voice_sessions?on_conflict=id`, {
    method: 'POST',
    headers: headers({ Prefer: 'resolution=merge-duplicates,return=minimal' }),
    body: JSON.stringify(rows),
  });
  if (!res.ok) throw new Error(`upsertSessions ${res.status}: ${await res.text()}`);
}

// Delete any session rows whose id is not in keepIds.
export async function pruneSessions(keepIds) {
  // PostgREST: id=not.in.(a,b,c). Quote ids defensively.
  const list = keepIds.map((id) => `"${id}"`).join(',');
  const filter = keepIds.length ? `id=not.in.(${list})` : 'id=neq.__none__';
  const res = await fetch(`${base()}/voice_sessions?${filter}`, {
    method: 'DELETE',
    headers: headers({ Prefer: 'return=minimal' }),
  });
  if (!res.ok) throw new Error(`pruneSessions ${res.status}: ${await res.text()}`);
}
