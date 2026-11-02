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
    // claimed_by is what makes an abandoned claim recoverable: a command left 'running' by a machine
    // that died can only be swept by the machine that took it, and this is how it knows which.
    body: JSON.stringify({ status: 'running', claimed_by: 'desk', updated: new Date().toISOString() }),
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

// ---- desk sessions -------------------------------------------------------
// The Mac publishes what it has open for the same two reasons the Pi does: the sessions panel needs
// to show it, and a follow-up resolves its target through command_id. Two handles, two columns: the
// tab's `tty` is how a follow-up reaches the session, `transcript_path` is how anything reads what
// it said. They were the same column once, which is why a Mac session's replies were unreadable.
export async function upsertDeskSession(row) {
  const res = await fetch(`${base()}/desk_sessions?on_conflict=id`, {
    method: 'POST',
    headers: headers({ Prefer: 'resolution=merge-duplicates,return=minimal' }),
    body: JSON.stringify([{ ...row, last_seen: new Date().toISOString() }]),
  });
  if (!res.ok) throw new Error(`upsertDeskSession ${res.status}: ${await res.text()}`);
}

export async function deskSession(id) {
  const res = await fetch(`${base()}/desk_sessions?id=eq.${encodeURIComponent(id)}&select=*`, { headers: headers() });
  if (!res.ok) return null;
  const rows = await res.json();
  return rows[0] || null;
}

export async function patchDeskSession(id, patch) {
  const res = await fetch(`${base()}/desk_sessions?id=eq.${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: headers({ Prefer: 'return=minimal' }),
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`patchDeskSession ${res.status}: ${await res.text()}`);
}

/** Sessions this machine has published, optionally filtered by status. */
export async function deskSessionsFor(worker, status) {
  const q = `worker=eq.${encodeURIComponent(worker)}${status ? `&status=eq.${status}` : ''}`;
  const res = await fetch(`${base()}/desk_sessions?${q}&select=*`, { headers: headers() });
  if (!res.ok) throw new Error(`deskSessionsFor ${res.status}: ${await res.text()}`);
  return res.json();
}

/**
 * Keep a command that could not run.
 *
 * The prompt inside is usually minutes of dictated speech that cannot be repeated, so it is never
 * simply logged and lost. Not a retry queue — the command is finished, and the user decides later
 * whether the user still wants it.
 */
export async function deadLetterCommand(cmd, reason, source = 'desk', machine = 'mac') {
  const res = await fetch(`${base()}/desk_dead_letters`, {
    method: 'POST',
    headers: headers({ Prefer: 'return=minimal' }),
    body: JSON.stringify([{ id: crypto.randomUUID(), command_id: cmd.id, kind: cmd.kind,
                            payload: cmd.payload || {}, machine,
                            reason: String(reason || '').slice(0, 500), source }]),
  });
  if (!res.ok) throw new Error(`deadLetterCommand ${res.status}: ${await res.text()}`);
}

/** Commands this machine claimed and never finished — see the sweep in commands.js. */
export async function abandonedClaims(worker, olderThanIso) {
  const res = await fetch(
    `${base()}/desk_commands?status=eq.running&claimed_by=eq.${encodeURIComponent(worker)}&updated=lt.${olderThanIso}&select=*`,
    { headers: headers() });
  if (!res.ok) throw new Error(`abandonedClaims ${res.status}: ${await res.text()}`);
  return res.json();
}

/** Enqueue a command for the other machine — how a transfer asks the destination to resume. */
export async function insertCommand(kind, payload) {
  const id = crypto.randomUUID();
  const res = await fetch(`${base()}/desk_commands`, {
    method: 'POST',
    headers: headers({ Prefer: 'return=minimal' }),
    body: JSON.stringify({ id, kind, payload }),
  });
  if (!res.ok) throw new Error(`insertCommand ${res.status}: ${await res.text()}`);
  return id;
}
