// Keeping a session's transcript on the other machine, so it can be picked up there.
//
// A Claude session is not process state — it is an append-only file plus a working directory — so
// handing one over is copying that file and resuming it. What makes it work is a single rewrite:
// every absolute path in the transcript carries the source machine's home, and the destination's is
// different (/Users/jaredgantt vs /home/jaredgantt). Rewritten, the session resumes with its full
// conversation and the same id; unrewritten, it points at a directory that does not exist.
//
// ONLY THE TAIL IS UPLOADED. Transcripts here reach 16MB and a turn adds tens of KB, so each mirror
// writes `<claude_session_id>/<offset>.part` and the destination concatenates the parts in offset
// order. `desk_sessions.mirror_offset` is how much is already up.
//
// Both desk consumers use this — it is shared, not copied, because the two halves of a transfer have
// to agree byte for byte on what a mirrored transcript is.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { projectDir } from './transcripts.js';

const SUPABASE_URL = process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co';
const BUCKET = 'transcripts';
const key = () => process.env.SUPABASE_KEY || '';
const auth = (extra = {}) => ({ apikey: key(), Authorization: `Bearer ${key()}`, ...extra });
// Offsets are the sort key, and lexical order has to match numeric order.
const partName = (offset) => `${String(offset).padStart(12, '0')}.part`;

/** The homes a transcript can have been written under. Rewriting is what makes it portable. */
const HOMES = ['/Users/jaredgantt', '/home/jaredgantt'];

/** Rewrite every path in a transcript to this machine's home. */
export function localize(text) {
  const here = os.homedir();
  let out = text;
  for (const h of HOMES) if (h !== here) out = out.split(h).join(here);
  return out;
}

// What Claude Code writes when it is sent C-c mid-turn — which is how pi-desk closes a session it moves
// for doing heavy work (sessions.js evict). Left in place, these tell the resumed session that THE USER
// rejected the step and to "STOP and wait", which outweighs any system note and halts the work the move
// exists to let it finish.
const REJECTED = "The user doesn't want to proceed with this tool use.";
const INTERRUPTED = '[Request interrupted by user';
const MOVED_RESULT = 'Not finished: the session was moved from the Pi to the Mac while this was running, and '
  + 'everything it had started on the Pi was stopped. Jared did not reject or interrupt it. Run it again here.';
const MOVED_INTERRUPT = '[Cut off by the move from the Pi, not by Jared]';

/**
 * Rewrite the interrupt records the move left at the end of a transcript into what actually happened.
 * Only the trailing run after the last assistant record is touched — that is the turn the move cut off;
 * an interrupt anywhere earlier was the user's.
 */
export function unmarkMoveInterrupt(text) {
  const lines = text.split('\n');
  let changed = false;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].trim()) continue;
    let rec;
    try { rec = JSON.parse(lines[i]); } catch { continue; }
    if (rec.type === 'assistant') break;
    const c = rec.type === 'user' ? rec.message?.content : null;
    if (!Array.isArray(c) && typeof c !== 'string') continue;
    let hit = false;
    if (typeof c === 'string') {
      if (c.startsWith(INTERRUPTED)) { rec.message.content = MOVED_INTERRUPT; hit = true; }
    } else {
      for (const b of c) {
        if (b.type === 'text' && b.text?.startsWith(INTERRUPTED)) { b.text = MOVED_INTERRUPT; hit = true; }
        if (b.type !== 'tool_result') continue;
        if (typeof b.content === 'string' && b.content.startsWith(REJECTED)) { b.content = MOVED_RESULT; hit = true; }
        if (Array.isArray(b.content) && b.content.some((x) => x.text?.startsWith(REJECTED))) {
          b.content = [{ type: 'text', text: MOVED_RESULT }]; hit = true;
        }
      }
    }
    if (!hit) continue;
    if (typeof rec.toolUseResult === 'string') rec.toolUseResult = MOVED_RESULT;   // "User rejected tool use"
    lines[i] = JSON.stringify(rec);
    changed = true;
  }
  return changed ? lines.join('\n') : text;
}

/**
 * Upload everything written since `offset`. Returns the new offset, or null if there was nothing.
 *
 * A file that has SHRUNK was rewritten under us (a compaction), so the parts already up describe a
 * transcript that no longer exists — they are dropped and the whole file goes again.
 */
export async function mirrorUp(claudeId, transcriptPath, offset = 0) {
  let st;
  try { st = fs.statSync(transcriptPath); } catch { return null; }
  if (!st.isFile()) return null;
  let from = Number(offset) || 0;
  if (st.size < from) { await mirrorReset(claudeId); from = 0; }
  if (st.size === from) return null;

  const fd = fs.openSync(transcriptPath, 'r');
  let buf;
  try {
    buf = Buffer.allocUnsafe(st.size - from);
    fs.readSync(fd, buf, 0, buf.length, from);
  } finally { fs.closeSync(fd); }

  const res = await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}/${claudeId}/${partName(from)}`, {
    method: 'POST',
    headers: auth({ 'Content-Type': 'application/x-ndjson', 'x-upsert': 'true' }),
    body: buf,
  });
  if (!res.ok) throw new Error(`mirrorUp ${res.status}: ${(await res.text()).slice(0, 160)}`);
  return st.size;
}

/** Every part uploaded for a session, in offset order. */
async function parts(claudeId) {
  const res = await fetch(`${SUPABASE_URL}/storage/v1/object/list/${BUCKET}`, {
    method: 'POST',
    headers: auth({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ prefix: claudeId, limit: 1000, sortBy: { column: 'name', order: 'asc' } }),
  });
  if (!res.ok) throw new Error(`mirror list ${res.status}: ${(await res.text()).slice(0, 160)}`);
  return (await res.json()).map((o) => o.name).filter((n) => n.endsWith('.part')).sort();
}

/** Throw the mirror away — used when a transcript is rewritten under us. */
export async function mirrorReset(claudeId) {
  let names;
  try { names = await parts(claudeId); } catch { return; }
  if (!names.length) return;
  await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}`, {
    method: 'DELETE',
    headers: auth({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ prefixes: names.map((n) => `${claudeId}/${n}`) }),
  });
}

/**
 * Assemble a mirrored transcript into this machine's project directory for `cwd`, ready to resume.
 *
 * Returns the file's path, or null if nothing has been mirrored — which is the honest answer when a
 * session was never idle long enough to be uploaded, and is far better than resuming an empty one.
 */
export async function mirrorDown(claudeId, cwd, { movedMidTurn = false } = {}) {
  const names = await parts(claudeId);
  if (!names.length) return null;
  const chunks = [];
  for (const n of names) {
    const res = await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}/${claudeId}/${n}`, { headers: auth() });
    if (!res.ok) throw new Error(`mirrorDown ${n} ${res.status}`);
    chunks.push(Buffer.from(await res.arrayBuffer()));
  }
  const dir = projectDir(cwd);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${claudeId}.jsonl`);
  const text = localize(Buffer.concat(chunks).toString('utf8'));
  fs.writeFileSync(file, movedMidTurn ? unmarkMoveInterrupt(text) : text, { mode: 0o600 });
  return file;
}
