// What a session is doing, the moment it does it.
//
// The reconcile tick (PRESENCE_MS, 30s, both machines) used to be the only thing that noticed a turn
// had finished: Claude Code wrote the reply to the transcript instantly, and it then sat on disk for
// up to half a minute before anything read it. That is not a cost anyone has to pay — the file
// changing IS an event, and both machines can watch it.
//
// So this watches each live session's transcript and pushes what it sees:
//
//   a prompt    → the host writes `active_at` (and `working`) to the row: a turn has started
//   tool_use    → the hub's /api/session-activity, transient, straight out to the wall over SSE
//   end_turn    → the same, carrying the reply, AND the host writes `last_reply` to the row
//
// The tick still runs and still does both of those. It is the NET now, not the mechanism, and it
// keeps that job for two reasons that are not going away: liveness cannot be event-driven, because
// nothing fires when a process stops existing; and fs.watch genuinely drops events — FSEvents
// coalesces on macOS, inotify can miss under load — so something slow has to come along behind.
//
// Shared by both hosts. The Mac's deskSessions.js and the Pi's pi-desk each call syncSessionWatch
// with whatever they currently believe is running, and hand in their own way of writing the row.
import fs from 'node:fs';
import { config } from './config.js';
import { readTail } from './transcripts.js';

// fs.watch fires several times for one append, so coalesce. Long enough to batch a write, far short
// of anything a person notices.
const DEBOUNCE_MS = 120;

const watched = new Map();   // session id -> { id, path, offset, rest, watcher, timer, onTurnEnd }

async function push(body) {
  if (!config.HUB_URL || !config.HUB_TOKEN) return;
  try {
    await fetch(`${config.HUB_URL}/api/session-activity?token=${encodeURIComponent(config.HUB_TOKEN)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch { /* the wall's backstop poll covers a dropped push; never let it break the watcher */ }
}

/**
 * One line describing a tool call.
 *
 * Deliberately the same shape the voice worker pushes to the activity bar (`worker.py` `_emit`):
 * the short tool name, then its arguments compacted. Two surfaces showing tool calls should read the
 * same way, or the wall teaches two vocabularies for one thing.
 */
function toolLine(block) {
  const name = String(block.name || '');
  const short = name.includes('__') ? name.split('__').pop() : name;
  let args = '';
  try { args = block.input ? JSON.stringify(block.input) : ''; } catch { args = ''; }
  if (args.length > 240) args = `${args.slice(0, 239)}…`;
  return `${short}${args ? ` ${args}` : ''}`.trim().slice(0, 260);
}

// Read only what is NEW. These transcripts reach tens of megabytes, so the offset is the whole point
// — and `rest` holds the trailing partial line, because an append can land mid-record.
function drain(st) {
  let size;
  try { size = fs.statSync(st.path).size; } catch { return; }
  // Shrunk means compacted or replaced. Starting over would replay the whole conversation as live
  // activity, so take the new end and carry on from there.
  if (size < st.offset) { st.offset = size; st.rest = ''; return; }
  if (size === st.offset) return;

  let chunk;
  try {
    const fd = fs.openSync(st.path, 'r');
    try {
      const buf = Buffer.allocUnsafe(size - st.offset);
      fs.readSync(fd, buf, 0, buf.length, st.offset);
      chunk = buf.toString('utf8');
    } finally { fs.closeSync(fd); }
  } catch { return; }
  st.offset = size;

  const lines = (st.rest + chunk).split('\n');
  st.rest = lines.pop();          // incomplete until the next append finishes it
  let ended = false;
  for (const line of lines) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    if (d.type === 'user') {
      // A prompt starts a turn. Tool results are recorded as user records too, and are the model's
      // own work coming back — the same distinction readTail draws.
      const blocks = d.message?.content;
      const toolResult = Array.isArray(blocks) && blocks.length > 0 && blocks.every((b) => b?.type === 'tool_result');
      if (!toolResult && typeof d.timestamp === 'string') {
        try { st.onActivity(st.id, d.timestamp); } catch { /* the tick writes it regardless */ }
      }
      continue;
    }
    if (d.type !== 'assistant') continue;
    const msg = d.message || {};
    for (const b of Array.isArray(msg.content) ? msg.content : []) {
      if (b?.type === 'tool_use') push({ session: st.id, state: 'tool', text: toolLine(b) });
    }
    // Claude Code writes one record per content block, each carrying the message's stop_reason, and
    // a turn ends with `end_turn` — the same marker readTail keys off.
    if (msg.stop_reason === 'end_turn') ended = true;
  }
  if (ended) finish(st);
}

// The reply itself is NOT assembled here. A turn's text spans several records and readTail already
// does that correctly, including the fallback for a transcript with no end_turn at all — this only
// has to notice that the turn ended and ask.
function finish(st) {
  const tail = readTail(st.path);
  reportTurnEnd(st.id, tail);
  try { st.onTurnEnd(st.id, tail); } catch { /* the tick will write it 30s from now regardless */ }
}

/**
 * Tell the hub a turn finished: the wall redraws the session and the hub pushes its reply to the
 * phone. The watcher calls this the moment a turn ends, and each host's reconcile tick calls it for a
 * turn end fs.watch dropped. `at` identifies the turn and the hub pushes each one once, so both
 * calling it for the same turn is harmless.
 */
export function reportTurnEnd(id, tail) {
  if (tail?.lastReply) push({ session: id, state: 'done', text: tail.lastReply.slice(0, 400), at: tail.replyAt || null });
}

function attach(st) {
  try {
    st.watcher = fs.watch(st.path, () => {
      clearTimeout(st.timer);
      st.timer = setTimeout(() => drain(st), DEBOUNCE_MS);
    });
    st.watcher.on('error', () => detach(st.id));
  } catch {
    // The transcript does not exist yet — a session opened a moment ago. The next sync retries.
    watched.delete(st.id);
  }
}

function detach(id) {
  const st = watched.get(id);
  if (!st) return;
  clearTimeout(st.timer);
  try { st.watcher?.close(); } catch { /* already gone */ }
  watched.delete(id);
}

/**
 * Watch exactly the sessions that are running, and nothing else.
 *
 * Called from each host's reconcile tick, so a session opened between ticks starts being watched
 * within one tick — the delay is in NOTICING a session, never in what it then does.
 *
 * `onTurnEnd(id, tail)` and `onActivity(id, isoTimestamp)` are the host writing its own row: the Mac
 * and the Pi patch `desk_sessions` through different clients, and this module should not know which.
 */
export function syncSessionWatch(rows, onTurnEnd, onActivity = () => {}) {
  const live = new Set();
  for (const r of rows || []) {
    if (r.status !== 'resident' || !r.transcript_path) continue;
    live.add(r.id);
    const cur = watched.get(r.id);
    if (cur && cur.path === r.transcript_path) continue;
    // A session that has moved to a new conversation (`/clear`) is watched in its new transcript;
    // staying on the old file would miss everything it does from here on.
    if (cur) detach(r.id);
    // Start at the CURRENT end of the file. Reading from zero would replay a whole conversation's
    // tool calls onto the wall as though they were happening now.
    let offset = 0;
    try { offset = fs.statSync(r.transcript_path).size; } catch { continue; }
    const st = { id: r.id, path: r.transcript_path, offset, rest: '', watcher: null, timer: null, onTurnEnd, onActivity };
    watched.set(r.id, st);
    attach(st);
  }
  for (const id of [...watched.keys()]) if (!live.has(id)) detach(id);
}
