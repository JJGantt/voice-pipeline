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
//   text        → the same, state 'text': what it says between tool calls (never the final reply)
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
import { spawn } from 'node:child_process';
import { config } from './config.js';
import { readTail, turnStart, resolvePrompt, gist, flatten } from './transcripts.js';

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
      // Bounded: one session's pushes go one after another (send), so a request that never returned
      // would hold back every step and reply after it for as long as the process lived.
      signal: AbortSignal.timeout(10000),
    });
  } catch { /* the wall's backstop poll covers a dropped push; never let it break the watcher */ }
}

// One session's pushes, one after another. Fired unawaited they can overtake each other in flight,
// and a thought landing after the tool call it led to, or a step after the turn's `done`, reads wrong.
function send(st, body) {
  st.chain = st.chain.then(() => push(body));
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
    // TYPED WHILE A TURN IS RUNNING. Claude Code QUEUES it and writes `queue-operation` records; there
    // is no `user` record at all for one absorbed into a running turn. Two moments, and they are
    // different facts:
    //
    //   enqueue — he has SENT it; the model has not seen it. `queued`, and a view holds it at the
    //             bottom, because putting it among the steps would claim it was read there.
    //   remove  — `absorbed_mid_turn`, and it carries the same content: THIS is when the model saw
    //             it, so this is where it belongs in the stream.
    //
    // (`dequeue` is the other ending — it started a turn of its own — and carries no content, so the
    // `user` record that follows is what reports it.)
    if (d.type === 'queue-operation') {
      const said = resolvePrompt(d.content);
      if (said.prompt && (d.operation === 'enqueue' || d.operation === 'remove')) {
        send(st, { session: st.id, state: d.operation === 'enqueue' ? 'queued' : 'said', text: said.prompt });
        if (said.stashed) st.echo = gist(said.prompt);
      }
      continue;
    }
    if (d.type === 'user') {
      // A prompt starts a turn. Tool results are recorded as user records too, and are the model's
      // own work coming back — the same distinction readTail draws.
      const blocks = d.message?.content;
      const toolResult = Array.isArray(blocks) && blocks.length > 0 && blocks.every((b) => b?.type === 'tool_result');
      if (!toolResult) st.said = null;
      if (!toolResult && typeof d.timestamp === 'string') {
        try { st.onActivity(st.id, d.timestamp); } catch { /* the tick writes it regardless */ }
      }
      // HIS WORDS, AT THE MOMENT THEY ARRIVE. A prompt sent while a turn is running used to send no
      // frame at all, so a live view learned of it only when the turn ended — and then drew it at the
      // TOP of that turn, above tool calls that had already happened before he typed it. Sent as its
      // own entry, it lands in the stream where it actually happened.
      // `turnStart` is the same rule readTail uses: tool results, injected context and compact
      // summaries are not him speaking, and a message stashed to a file resolves to its real text.
      // A prompt that started its OWN turn: this record is its ingestion. One that was absorbed
      // mid-turn never reaches here — the `remove` above already reported it.
      const said = toolResult ? null : turnStart(d);
      if (said && said.prompt) send(st, { session: st.id, state: 'said', text: said.prompt });
      // A message too long for the tty is typed as a stub that ASKS the session to reply with the
      // words verbatim, so they are visible in the terminal. They are visible in the hub already —
      // `said` above carries them — so the echo is the same paragraph twice, and the whole point of
      // resolving the stub was that there be no trace of the workaround here. Held so the text frame
      // that quotes it can be recognised and dropped.
      if (said && said.stashed) st.echo = gist(said.prompt);
      else if (said && said.prompt) st.echo = null;
      continue;
    }
    if (d.type !== 'assistant') continue;
    const msg = d.message || {};
    // What it says between tool calls. Claude Code writes a message one block per record, in order, so
    // a step's words arrive before its tool call; they are held until that call and sent just ahead of
    // it: the text if Claude Code recorded it, else the summary of its thinking. From 2.1.268 Claude
    // Code records a turn's opening text and its final reply but not text written after a tool result
    // (measured across every transcript on the Mac: 1 in 540 records on 268-270, against hundreds per
    // version on 267 and before), so for most steps the thinking summary is the only account written.
    // The transcript format is internal and undocumented, so this is only as good as what it holds.
    // A message that ends the turn is the reply, which arrives on `done`; what it held is dropped.
    if (st.said?.id !== msg.id) st.said = { id: msg.id, text: [], thought: [] };
    for (const b of Array.isArray(msg.content) ? msg.content : []) {
      if (b?.type === 'text' && String(b.text || '').trim()) st.said.text.push(String(b.text).trim());
      else if (b?.type === 'thinking' && String(b.thinking || '').trim()) st.said.thought.push(String(b.thinking).trim());
      else if (b?.type === 'tool_use') {
        const words = st.said.text.length ? st.said.text : st.said.thought;
        const text = words.join('\n\n');
        // Not the session reading his own stashed message back to him — see `st.echo` above.
        if (words.length && !(st.echo && flatten(text).includes(st.echo))) {
          send(st, { session: st.id, state: 'text', text });
        }
        st.said = { id: msg.id, text: [], thought: [] };
        send(st, { session: st.id, state: 'tool', text: toolLine(b) });
      }
    }
    // Claude Code writes one record per content block, each carrying the message's stop_reason, and
    // a turn ends with `end_turn` — the same marker readTail keys off.
    if (msg.stop_reason === 'end_turn') { ended = true; st.said = null; }
  }
  if (ended) finish(st);
}

// The reply itself is NOT assembled here. A turn's text spans several records and readTail already
// does that correctly, including the fallback for a transcript with no end_turn at all — this only
// has to notice that the turn ended and ask.
function finish(st) {
  const tail = readTail(st.path);
  // The row first, then the news, and both after the steps still in flight. A surface told `done`
  // fetches the reply at once (the watch's open session does), and one that asks before the host
  // has written the row gets the previous reply and keeps showing it.
  // The wait for the write is bounded for the same reason as push: nothing in the host's client times
  // out, and a write that never returned would hold back this reply and every step after it.
  st.chain = st.chain
    .then(() => Promise.race([st.onTurnEnd(st.id, tail), new Promise((r) => setTimeout(r, 10000))]))
    .catch(() => { /* the tick will write it 30s from now regardless */ })
    .then(() => reportTurnEnd(st.id, tail));
}

/**
 * Tell the hub a turn finished: the wall redraws the session and the hub pushes its reply to the
 * phone. The watcher calls this the moment a turn ends, and each host's reconcile tick calls it for a
 * turn end fs.watch dropped. `at` identifies the turn and the hub pushes each one once, so both
 * calling it for the same turn is harmless.
 */
export function reportTurnEnd(id, tail) {
  if (!tail?.lastReply) return Promise.resolve();
  // `done` goes out AT ONCE — it is what makes a live view stop showing the caret and fetch the reply.
  // The notification does not ride on it: `summary_coming` tells the hub to hold it for the frame
  // below, which arrives seconds later carrying a sentence worth putting on a lock screen. A host that
  // does not send one (the Pi, today) simply omits the flag and the hub notifies from `done` as before.
  const p = push({ session: id, state: 'done', text: tail.lastReply.slice(0, 400),
                   at: tail.replyAt || null, summary_coming: true });
  summarise(tail.lastReply).then((summary) => push({
    session: id, state: 'summary', text: tail.lastReply.slice(0, 400), at: tail.replyAt || null, summary,
  })).catch(() => {});
  return p;
}

// ---- the sentence that lands on a lock screen -------------------------------
//
// A finished answer is written for a screen; an iOS banner shows about two lines of it, which is
// usually the preamble. So this asks for ONE sentence saying what happened — "Reverted the polling
// timer and deployed the Hammerspoon alternative" — and the hub uses it as the notification body.
//
// Run through the `claude` CLI on this machine rather than an API: it is the Claude that is already
// set up here, so this adds no key anywhere. Haiku because it is the cheapest thing that can read a
// page and say what it was, and this runs on every finished turn.
//
// Never required. Any failure — no CLI, a timeout, empty output — resolves to null, and the hub falls
// back to the reply itself, which is exactly what it sends today.
const SUMMARY_SYSTEM =
  'Summarise this finished reply as ONE short sentence for a phone notification banner, which shows '
  + 'about two lines. Say what was DONE and the outcome. Past tense, terse, addressed to nobody: no '
  + '"you", no "I", never "the assistant". No line breaks, no markdown, no bullets, no preamble, no '
  + 'quotes. Aim for 100 characters and never exceed 160. If the reply asks a question or needs a '
  + 'decision, the sentence is that question. Output only the sentence.';
const SUMMARY_MAX = 160;
function summarise(reply) {
  const body = String(reply || '').trim();
  if (body.length < 200) return Promise.resolve(null);   // short answers are already their own summary
  return new Promise((resolve) => {
    let out = '', done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    let child;
    try {
      child = spawn('claude', ['-p', '--model', 'haiku'], { stdio: ['pipe', 'pipe', 'ignore'] });
    } catch (_) { return finish(null); }
    const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch (_) {} finish(null); }, 45000);
    child.stdout.on('data', (c) => { out += c; if (out.length > 4000) out = out.slice(0, 4000); });
    child.on('error', () => { clearTimeout(timer); finish(null); });
    child.on('close', () => {
      clearTimeout(timer);
      const line = out.replace(/\s+/g, ' ').trim().replace(/^["']|["']$/g, '');
      finish(line ? line.slice(0, SUMMARY_MAX) : null);
    });
    try { child.stdin.end(`${SUMMARY_SYSTEM}\n\n---\n\n${body.slice(0, 12000)}`); }
    catch (_) { clearTimeout(timer); finish(null); }
  });
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
    const st = { id: r.id, path: r.transcript_path, offset, rest: '', watcher: null, timer: null, onTurnEnd, onActivity,
                 said: null, chain: Promise.resolve() };
    watched.set(r.id, st);
    attach(st);
  }
  for (const id of [...watched.keys()]) if (!live.has(id)) detach(id);
}
