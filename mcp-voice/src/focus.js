// Resolves a fuzzy focus target against recent sessions and writes voice_focus.

import { listRecentSessions } from './sessions.js';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_KEY;

const HUB_WORDS = ['hub', 'wall', 'home', 'dashboard', 'tv', 'default', 'main', 'main view', 'default view', 'hub view'];
const RECENT_WORDS = ['most recent', 'latest', 'last', 'newest', 'current'];

function norm(s) {
  return (s || '').toLowerCase().trim();
}
// "the hub", "switch to the hub", "go to hub" → "hub". Strips leading filler so a bare
// hub word resolves to the default view even if a SESSION happens to be named e.g. "Hub UI"
// (that fuller name still routes to the session via keyword matching below).
function stripFiller(q) {
  return q.replace(/^(switch to |go to |show |open |focus on |focus |bring up |the )+/g, '').trim();
}

// Resolve `target` to either 'hub' or one of the recent sessions.
// Returns { kind: 'hub' } | { kind: 'session', session, note } | { kind: 'none', message }.
export async function resolveTarget(target) {
  const q = norm(target);
  if (!q) return { kind: 'none', message: 'Empty target.' };

  // Bare "hub"/"default"/"the hub"/... → the default dashboard view (not a session).
  const bare = stripFiller(q);
  if (HUB_WORDS.includes(q) || HUB_WORDS.includes(bare)) {
    return { kind: 'hub' };
  }

  const sessions = await listRecentSessions(5);
  if (!sessions.length) {
    if (q === 'hub') return { kind: 'hub' };
    return { kind: 'none', message: 'No recent sessions found to focus.' };
  }

  // "most recent" / "latest" / "last" → newest session (already sorted newest-first).
  if (RECENT_WORDS.some((w) => q === w || q.includes(w))) {
    return { kind: 'session', session: sessions[0], note: 'most recent session' };
  }

  // Exact session id match.
  const byId = sessions.find((s) => norm(s.id) === q || norm(s.id).startsWith(q));
  if (byId) return { kind: 'session', session: byId };

  // Keyword scoring against title and project.
  const terms = q.split(/\s+/).filter(Boolean);
  const scored = sessions.map((s) => {
    const hay = `${norm(s.title)} ${norm(s.project)}`;
    let score = 0;
    if (hay.includes(q)) score += 10; // full-phrase substring
    for (const t of terms) if (hay.includes(t)) score += 1;
    return { s, score };
  });
  scored.sort((a, b) => b.score - a.score);

  if (scored[0].score === 0) {
    const list = sessions.map((s) => `"${s.title}"`).join(', ');
    return { kind: 'none', message: `No session matched "${target}". Recent: ${list}.` };
  }

  const best = scored[0];
  const tied = scored.filter((x) => x.score === best.score);
  const note = tied.length > 1
    ? `best of ${tied.length} matches`
    : undefined;
  return { kind: 'session', session: best.s, note };
}

// PATCH the voice_focus singleton (id=1). Leaves terminal_url untouched.
export async function writeFocus({ focus, label, session_cwd }) {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    throw new Error('SUPABASE_URL / SUPABASE_KEY not set in environment.');
  }
  const body = {
    focus,
    label,
    session_cwd: session_cwd ?? null,
    updated_by: 'mcp',
    updated_at: new Date().toISOString(),
  };
  const res = await fetch(`${SUPABASE_URL}/rest/v1/voice_focus?id=eq.1`, {
    method: 'PATCH',
    headers: {
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${SUPABASE_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`Supabase PATCH failed (${res.status}): ${txt}`);
  }
  return res.json();
}

export async function setFocus(target) {
  const resolved = await resolveTarget(target);
  if (resolved.kind === 'none') {
    return resolved.message;
  }
  if (resolved.kind === 'hub') {
    await writeFocus({ focus: 'hub', label: 'Hub', session_cwd: null });
    return 'Focus set to the Hub (Pi/TV wall home view).';
  }
  const s = resolved.session;
  await writeFocus({ focus: s.id, label: s.title, session_cwd: s.cwd });
  const noteStr = resolved.note ? ` (${resolved.note})` : '';
  const cwdStr = s.cwd ? ` — ${s.cwd}` : '';
  return `Focus set to session "${s.title}"${noteStr}${cwdStr} [${s.id}].`;
}
