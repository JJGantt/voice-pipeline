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

// ---- desk control (hub repo picker) ---------------------------------------

// Upsert the scanned repo rows (merge-duplicates on the path PK).
export async function upsertRepos(rows) {
  if (!rows.length) return;
  const res = await fetch(`${base()}/repos?on_conflict=path`, {
    method: 'POST',
    headers: headers({ Prefer: 'resolution=merge-duplicates,return=minimal' }),
    body: JSON.stringify(rows),
  });
  if (!res.ok) throw new Error(`upsertRepos ${res.status}: ${await res.text()}`);
}

// Delete repo rows for anything that has left the scanned roots.
export async function pruneRepos(keepPaths) {
  const list = keepPaths.map((p) => `"${p}"`).join(',');
  const filter = keepPaths.length ? `path=not.in.(${list})` : 'path=neq.__none__';
  const res = await fetch(`${base()}/repos?${filter}`, {
    method: 'DELETE',
    headers: headers({ Prefer: 'return=minimal' }),
  });
  if (!res.ok) throw new Error(`pruneRepos ${res.status}: ${await res.text()}`);
}

// Oldest-first pending commands.
export async function pendingCommands() {
  const res = await fetch(`${base()}/desk_commands?status=eq.pending&select=*&order=created.asc`, {
    headers: headers(),
  });
  if (!res.ok) throw new Error(`pendingCommands ${res.status}: ${await res.text()}`);
  return res.json();
}

// Claim a command by flipping pending -> running. The status filter makes this a compare-and-swap:
// an empty result means someone else got there first, so the caller must skip it.
export async function claimCommand(id) {
  const res = await fetch(`${base()}/desk_commands?id=eq.${encodeURIComponent(id)}&status=eq.pending`, {
    method: 'PATCH',
    headers: headers({ Prefer: 'return=representation' }),
    body: JSON.stringify({ status: 'running', updated: new Date().toISOString() }),
  });
  if (!res.ok) throw new Error(`claimCommand ${res.status}: ${await res.text()}`);
  const rows = await res.json();
  return rows.length > 0;
}

// Terminal state for a command: done | error | expired (+ a human-readable detail).
export async function finishCommand(id, status, detail) {
  const res = await fetch(`${base()}/desk_commands?id=eq.${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: headers({ Prefer: 'return=minimal' }),
    body: JSON.stringify({ status, detail: detail ? String(detail).slice(0, 500) : null, updated: new Date().toISOString() }),
  });
  if (!res.ok) throw new Error(`finishCommand ${res.status}: ${await res.text()}`);
}

// Heartbeat so the hub can tell whether the Mac is awake and able to launch anything.
export async function heartbeatDesk(worker, info) {
  const res = await fetch(`${base()}/worker_presence?on_conflict=worker`, {
    method: 'POST',
    headers: headers({ Prefer: 'resolution=merge-duplicates,return=minimal' }),
    body: JSON.stringify([{ worker, last_seen: new Date().toISOString(), info: info || {} }]),
  });
  if (!res.ok) throw new Error(`heartbeatDesk ${res.status}: ${await res.text()}`);
}
