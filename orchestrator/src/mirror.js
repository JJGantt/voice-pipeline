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
const MIRROR_FETCHES = 16;   // parallel part downloads in mirrorDown
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

// How far this process has uploaded each conversation, and the upload in flight for it. Both
// consumers mirror one conversation from more than one place (the transcript watcher as it is written,
// the reconcile tick when it is idle, a close or a move), and parts are keyed by the offset they start
// at, so two uploads from the same stale offset would leave overlapping parts. One upload at a time per
// conversation, each starting where the last one ended, keeps the parts contiguous.
const uploadedTo = new Map();
const inFlight = new Map();

/**
 * Upload every whole line written since `offset`. Returns the offset the mirror now reaches, or null
 * when there was nothing to upload.
 *
 * Only whole lines: a transcript is appended one JSON record per line, and a read can land in the
 * middle of one being written. The tail is cut at the last newline and the returned offset is the byte
 * after it, so the half-written record goes up whole with the next upload instead of as two halves.
 *
 * A file that has SHRUNK was rewritten under us (a compaction), so the parts already up describe a
 * transcript that no longer exists — they are dropped and the whole file goes again.
 */
export function mirrorUp(claudeId, transcriptPath, offset = 0) {
  const next = (inFlight.get(claudeId) || Promise.resolve()).catch(() => {})
    .then(() => uploadTail(claudeId, transcriptPath, offset));
  inFlight.set(claudeId, next);
  next.then(() => {}, () => {}).then(() => { if (inFlight.get(claudeId) === next) inFlight.delete(claudeId); });
  return next;
}

async function uploadTail(claudeId, transcriptPath, offset) {
  let st;
  try { st = fs.statSync(transcriptPath); } catch { return null; }
  if (!st.isFile()) return null;
  const asked = Number(offset) || 0;
  // The caller's offset comes from a row that may not have been written since this process last uploaded.
  let from = Math.max(asked, uploadedTo.get(claudeId) || 0);
  if (st.size < from) { await mirrorReset(claudeId); from = 0; }
  const reached = () => (from > asked ? from : null);
  if (st.size === from) return reached();

  const fd = fs.openSync(transcriptPath, 'r');
  let buf;
  try {
    buf = Buffer.allocUnsafe(st.size - from);
    fs.readSync(fd, buf, 0, buf.length, from);
  } finally { fs.closeSync(fd); }
  const cut = buf.lastIndexOf(0x0a);
  if (cut < 0) return reached();   // nothing but a line still being written
  const body = buf.subarray(0, cut + 1);

  const res = await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}/${claudeId}/${partName(from)}`, {
    method: 'POST',
    headers: auth({ 'Content-Type': 'application/x-ndjson', 'x-upsert': 'true' }),
    body,
  });
  if (!res.ok) throw new Error(`mirrorUp ${res.status}: ${(await res.text()).slice(0, 160)}`);
  const to = from + body.length;
  uploadedTo.set(claudeId, to);
  return to;
}

/** Whether anything has been mirrored for a conversation — whether it can be resumed elsewhere. */
export async function mirrorHas(claudeId) {
  return (await parts(claudeId)).length > 0;
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
  uploadedTo.delete(claudeId);
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
 * The transcript the parts describe, `[{ at, buf }]` in offset order. A part that starts before the end
 * of the ones before it repeats bytes they already hold (an upload made from an offset a row had not
 * caught up to), so only what it adds past that end is kept.
 */
export function assemble(chunks) {
  const out = [];
  let end = 0;
  for (const { at, buf } of chunks) {
    const skip = Number.isFinite(at) ? Math.max(0, end - at) : 0;
    if (skip >= buf.length) continue;
    out.push(skip ? buf.subarray(skip) : buf);
    end = (Number.isFinite(at) ? Math.max(at, end) : end) + buf.length - skip;
  }
  return Buffer.concat(out);
}

/**
 * Assemble a mirrored transcript into this machine's project directory for `cwd`, ready to resume.
 *
 * Returns the file's path, or null if nothing has been mirrored — the honest answer for a session that
 * never wrote a whole line, and far better than resuming an empty one.
 */
export async function mirrorDown(claudeId, cwd, { movedMidTurn = false } = {}) {
  const names = await parts(claudeId);
  if (!names.length) return null;
  // In parallel: a long session is hundreds of parts at ~0.4s a request, so fetched one at a time a
  // 300-part transcript takes over two minutes, during which the session runs on no machine and this
  // machine's command queue (drained serially) takes nothing else. Each part keeps its slot, because
  // `assemble` relies on offset order.
  const chunks = new Array(names.length);
  let next = 0;
  const fetchSome = async () => {
    while (next < names.length) {
      const i = next++;
      const res = await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}/${claudeId}/${names[i]}`, { headers: auth() });
      if (!res.ok) throw new Error(`mirrorDown ${names[i]} ${res.status}`);
      chunks[i] = { at: Number.parseInt(names[i], 10), buf: Buffer.from(await res.arrayBuffer()) };
    }
  };
  await Promise.all(Array.from({ length: Math.min(MIRROR_FETCHES, names.length) }, fetchSome));
  const dir = projectDir(cwd);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${claudeId}.jsonl`);
  const text = localize(assemble(chunks).toString('utf8'));
  fs.writeFileSync(file, movedMidTurn ? unmarkMoveInterrupt(text) : text, { mode: 0o600 });
  return file;
}
