// What a session is doing, the moment it does it.
//
// The reconcile tick (PRESENCE_MS, 30s, both machines) used to be the only thing that noticed a turn
// had finished: Claude Code wrote the reply to the transcript instantly, and it then sat on disk for
// up to half a minute before anything read it. That is not a cost anyone has to pay — the file
// changing IS an event, and both machines can watch it.
//
// So this watches each live session's transcript and pushes what it sees:
//
//   a prompt    → the host writes `active_at` (and `working`) to the row: a turn has started, and
//                 the hub's /api/session-activity gets it as `said` (or `queued` then `said`), with an
//                 id and, when this machine typed it for a command, that command's id (`expectPrompt`)
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
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { config } from './config.js';
import { readTail, turnStart, turnStartBefore, resolvePrompt, gist, flatten, fromHim } from './transcripts.js';

// fs.watch fires several times for one append, so coalesce. Long enough to batch a write, far short
// of anything a person notices.
const DEBOUNCE_MS = 120;

const watched = new Map();   // session id -> { id, path, offset, rest, watcher, timer, onTurnEnd }

// EVERY PROMPT FRAME CARRIES AN IDENTITY, so nothing downstream has to recognise one by its text.
//
// `id` is minted here, once per message: a message queued behind a running turn is one event at two
// moments (`queued` when Claude Code takes it, `said` when the model reads it), and both frames carry
// the same id, so a view updates one entry instead of matching two texts. `cmd` names the hub command
// whose words these are, when this machine typed them: the host registers what it is about to type
// (`expectPrompt`), and the first prompt the transcript then shows with those words takes the id. That
// is the one place text is compared — once, at the source, against the exact string this process just
// typed — and it is what lets the device that sent the words replace its own placeholder by id.
//
// Registered against the session, not the watcher: a resume with words launches a new row that is not
// watched until the next tick, and the words are typed before that. For the same reason the claim is
// made on the PREFIX read too (`loadPrefix`): a resumed session writes its first prompt before the
// watcher attaches, so that prompt is only ever read as prefix, and a claim made only on the live read
// leaves its sender's placeholder on screen under the whole turn. Claiming there is safe because an
// expectation exists only for words this process typed in the last few minutes.
const expecting = new Map();   // session id -> [{ text, cmd, at }]
const EXPECT_MS = 5 * 60 * 1000;
export function expectPrompt(sessionId, text, cmd) {
  const list = expecting.get(sessionId) || [];
  // Matched against the prompt as resolvePrompt shows it, so the same stripping applies to both sides.
  list.push({ text: flatten(resolvePrompt(text).prompt || ''), cmd: String(cmd), at: Date.now() });
  expecting.set(sessionId, list);
}
function takeExpect(sessionId, prompt) {
  const list = expecting.get(sessionId);
  if (!list) return null;
  const now = Date.now();
  const flat = flatten(prompt);
  const i = list.findIndex((e) => e.text === flat && now - e.at < EXPECT_MS);
  const hit = i >= 0 ? list.splice(i, 1)[0].cmd : null;
  const kept = list.filter((e) => now - e.at < EXPECT_MS);
  if (kept.length) expecting.set(sessionId, kept); else expecting.delete(sessionId);
  return hit;
}

async function push(body) {
  if (!config.HUB_URL || !config.HUB_TOKEN) return null;
  try {
    const r = await fetch(`${config.HUB_URL}/api/session-activity?token=${encodeURIComponent(config.HUB_TOKEN)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      // Bounded: one session's pushes go one after another (send), so a request that never returned
      // would hold back every step and reply after it for as long as the process lived.
      signal: AbortSignal.timeout(10000),
    });
    // The hub answers an event with what it holds of that turn (`emit`); everything else is empty.
    return r.status === 200 ? await r.json().catch(() => null) : null;
  } catch { return null; /* the wall's backstop poll covers a dropped push; never let it break the watcher */ }
}

/**
 * One line describing a tool call.
 *
 * Deliberately the same shape the voice worker pushes to the activity bar (`worker.py` `_emit`):
 * the short tool name, then its arguments compacted. Two surfaces showing tool calls should read the
 * same way, or the wall teaches two vocabularies for one thing.
 */
function readsStash(block) {
  try { return JSON.stringify(block.input || {}).includes('.claude-desk-prompts/'); } catch { return false; }
}

function toolLine(block) {
  const name = String(block.name || '');
  const short = name.includes('__') ? name.split('__').pop() : name;
  let args = '';
  try { args = block.input ? JSON.stringify(block.input) : ''; } catch { args = ''; }
  if (args.length > 240) args = `${args.slice(0, 239)}…`;
  return `${short}${args ? ` ${args}` : ''}`.trim().slice(0, 260);
}

// ---- A TURN IS A NUMBERED LIST, AND THIS MACHINE HOLDS ALL OF IT ------------------------------
//
// The hub keeps a conversation's events in memory, so a hub restart (every deploy is one) loses the
// turn in flight: its prompt, and every step so far. The transcript is the record, and it is here.
// So each event of a turn carries the turn's `turn` key and its position `n` in it, and this keeps
// the turn's whole list (`st.turn.events`). A hub that is sent n = 57 while holding nothing of that
// turn answers `{ replay, have }`, and gets everything it is missing before the next frame goes out.
// `done` carries the list too, which is what the hub stores as the finished turn.
//
// The key is the uuid of the record that opened the turn, so it is the same after this process
// restarts and reads the turn's beginning back from the transcript (`loadPrefix`).
const newTurn = (d) => ({ key: String((d && (d.uuid || d.timestamp)) || randomUUID()), at: (d && d.timestamp) || null, n: 0, events: [] });

/** Number an event into its turn. Returns the event as sent. */
function note(st, body) {
  if (!st.turn) st.turn = newTurn();
  const t = st.turn;
  const ev = { ...body, turn: t.key, n: ++t.n };
  t.events.push(ev);
  return ev;
}

function emit(st, body) {
  const ev = note(st, body), t = st.turn;
  st.chain = st.chain.then(async () => {
    const r = await push(ev);
    if (r && r.replay) {
      const have = Number(r.have) || 0;
      await push({ session: st.id, state: 'replay', turn: t.key,
                   events: t.events.filter((e) => e.n > have && e.n <= ev.n) });
    }
  });
}

/**
 * One transcript record, as the events it means. `out` receives each one.
 *
 * Shared by the live read and by `loadPrefix`, which runs it over the part of a turn written before
 * this process was watching: the same records have to number the same way both times, or a replay
 * would not line up with what the hub already holds. `live` is false there, and the only thing it
 * turns off is the one that acts on the world: the row's activity stamp. A typed command's id IS
 * claimed there (`takeExpect`, see `expectPrompt`).
 * Returns true when the record ends the turn.
 */
export function step(st, d, out, live) {
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
    if (!fromHim(d.content)) return false;
    const said = resolvePrompt(d.content);
    if (said.prompt && (d.operation === 'enqueue' || d.operation === 'remove')) {
      // The same message at its two moments carries the same id: minted at `enqueue` and held on
      // `st.queue` until its `remove` (matched by the exact text both records carry) takes it. A
      // `remove` with nothing queued (the enqueue is beyond what this process has read) mints its own.
      const fresh = () => ({ id: randomUUID(), cmd: takeExpect(st.id, said.prompt) });
      let ev;
      if (d.operation === 'enqueue') {
        ev = fresh();
        st.queue.push({ ...ev, text: flatten(said.prompt) });
      } else {
        const flat = flatten(said.prompt);
        const i = st.queue.findIndex((q) => q.text === flat);
        ev = i >= 0 ? st.queue.splice(i, 1)[0] : fresh();
      }
      out({ session: st.id, state: d.operation === 'enqueue' ? 'queued' : 'said', text: said.prompt,
            id: ev.id, ...(ev.cmd ? { cmd: ev.cmd } : {}) });
      if (said.stashed) st.echo = gist(said.prompt);
    }
    return false;
  }
  if (d.type === 'user') {
    // A prompt starts a turn. Tool results are recorded as user records too, and are the model's
    // own work coming back — the same distinction readTail draws.
    // turnStart is null for both, and for a terminal-local command and its output.
    const said = turnStart(d);
    if (said) st.said = null;
    // Injected context starts nothing of its own, as in readTail: the turn before it stands.
    if (said && !said.meta) st.turn = newTurn(d);
    if (live && said && typeof d.timestamp === 'string') {
      try { st.onActivity(st.id, d.timestamp); } catch { /* the tick writes it regardless */ }
    }
    // HIS WORDS, AT THE MOMENT THEY ARRIVE, as their own event in the list.
    // `turnStart` is the same rule readTail uses: tool results, injected context and compact
    // summaries are not him speaking, and a message stashed to a file resolves to its real text.
    // A prompt that started its OWN turn: this record is its ingestion. One that was absorbed
    // mid-turn never reaches here — the `remove` above already reported it.
    // One that waited in the queue for the turn before to end is the same message it was when queued,
    // so it keeps that id and a view moves the one entry instead of showing it twice.
    if (said && said.prompt) {
      const flat = flatten(said.prompt);
      const i = st.queue.findIndex((q) => q.text === flat);
      const ev = i >= 0 ? st.queue.splice(i, 1)[0] : { id: randomUUID(), cmd: takeExpect(st.id, said.prompt) };
      out({ session: st.id, state: 'said', text: said.prompt, id: ev.id, ...(ev.cmd ? { cmd: ev.cmd } : {}) });
    }
    // A message too long for the tty is typed as a stub that ASKS the session to quote the words
    // back, so they are visible in the terminal. They are visible in the hub already — `said` above
    // carries them — so the echo is the same paragraph twice, and the whole point of resolving the
    // stub was that there be no trace of the workaround here. Held so the text frame that quotes it
    // can be recognised and dropped.
    if (said && said.stashed) st.echo = gist(said.prompt);
    else if (said && said.prompt) st.echo = null;
    return false;
  }
  if (d.type !== 'assistant') return false;
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
        out({ session: st.id, state: 'text', text });
      }
      st.said = { id: msg.id, text: [], thought: [] };
      // Nor the session opening the stash file itself: a path into ~/.claude-desk-prompts on the wrist
      // is the workaround showing through, and his words already arrived as `said`.
      if (readsStash(b)) continue;
      out({ session: st.id, state: 'tool', text: toolLine(b) });
    }
  }
  // Claude Code writes one record per content block, each carrying the message's stop_reason, and
  // a turn ends with `end_turn` — the same marker readTail keys off.
  if (msg.stop_reason === 'end_turn') { st.said = null; return true; }
  return false;
}

/**
 * The part of the turn in flight that was written before this process started watching.
 *
 * The watcher attaches at the END of a transcript, so a turn already under way then is known only
 * from there on — and its events would number from 1 again, against a hub holding the real count.
 * Read once, on the first new record: from the record that opened the turn up to where the watcher
 * attached, through the same `step` the live read uses.
 */
function loadPrefix(st) {
  st.partial = false;
  const start = turnStartBefore(st.path, st.attachOffset);
  if (!start || typeof start.offset !== 'number' || start.offset >= st.attachOffset) return;
  let text;
  try {
    const fd = fs.openSync(st.path, 'r');
    try {
      const buf = Buffer.allocUnsafe(st.attachOffset - start.offset);
      fs.readSync(fd, buf, 0, buf.length, start.offset);
      text = buf.toString('utf8');
    } finally { fs.closeSync(fd); }
  } catch { return; }
  const was = { id: st.id, said: null, queue: [], echo: null, turn: null };
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    // A turn that had ended by the attach leaves nothing in flight.
    if (step(was, d, (body) => note(was, body), false)) was.turn = null;
  }
  st.turn = was.turn; st.said = was.said; st.queue = was.queue; st.echo = was.echo;
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
  if (st.partial) loadPrefix(st);

  const lines = (st.rest + chunk).split('\n');
  st.rest = lines.pop();          // incomplete until the next append finishes it
  let ended = false;
  for (const line of lines) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    if (step(st, d, (body) => emit(st, body), true)) {
      // A message is one record per content block, so a turn's last message ends it once per block.
      // The turn is set aside at the first and stays `st.last` for the rest: taking it again at the
      // second would hand `finish` an empty list in place of the turn.
      if (st.turn) { st.last = st.turn; st.turn = null; }
      ended = true;
    }
  }
  if (ended) finish(st);
}

// The reply itself is NOT assembled here. A turn's text spans several records and readTail already
// does that correctly, including the fallback for a transcript with no end_turn at all — this only
// has to notice that the turn ended and ask.
function finish(st) {
  const tail = readTail(st.path);
  // The turn's list goes with the reply it led to. A turn that ended saying nothing, or whose reply
  // is not written yet (the block before it also carries `end_turn`), reports the reply before it —
  // readTail's `lastReply` is the last COMPLETED answer — which is not this turn's.
  const turn = st.last;
  const mine = turn && (!turn.at || !tail.replyAt || tail.replyAt >= turn.at) ? turn : null;
  // The row first, then the news, and both after the steps still in flight. A surface told `done`
  // fetches the reply at once (the watch's open session does), and one that asks before the host
  // has written the row gets the previous reply and keeps showing it.
  // The wait for the write is bounded for the same reason as push: nothing in the host's client times
  // out, and a write that never returned would hold back this reply and every step after it.
  st.chain = st.chain
    .then(() => Promise.race([st.onTurnEnd(st.id, tail), new Promise((r) => setTimeout(r, 10000))]))
    .catch(() => { /* the tick will write it 30s from now regardless */ })
    .then(() => reportTurnEnd(st.id, tail, mine))
    .then(() => st.onReported(st.id, tail))
    .catch((e) => console.error('[sessionWatch] after turn end:', e.message));
}

/**
 * Tell the hub a turn finished: the wall redraws the session and the hub pushes its reply to the
 * phone. The watcher calls this the moment a turn ends, and each host's reconcile tick calls it for a
 * turn end fs.watch dropped. `at` identifies the turn and the hub pushes each one once, so both
 * calling it for the same turn is harmless.
 */
export function reportTurnEnd(id, tail, turn = null) {
  if (!tail?.lastReply) return Promise.resolve();
  // `done` goes out AT ONCE — it is what makes a live view stop showing the caret and fetch the reply.
  // The notification does not ride on it: `summary_coming` tells the hub to hold it for the frame
  // below, which arrives seconds later carrying a sentence worth putting on a lock screen. A host that
  // does not send one (the Pi, today) simply omits the flag and the hub notifies from `done` as before.
  // `text` is the 400-character line a wall draws; `reply` is the whole answer, and `prompt` what
  // began the turn, so the hub's event log holds the finished turn in full without a second read.
  const p = push({ session: id, state: 'done', text: tail.lastReply.slice(0, 400),
                   reply: tail.lastReply.slice(0, 20000),
                   prompt: tail.lastPrompt ? tail.lastPrompt.slice(0, 20000) : null,
                   at: tail.replyAt || null, summary_coming: true,
                   // The turn in order, as this machine read it: what the hub stores, and what fills
                   // in a hub that restarted and heard none of it. Absent from the tick's report of a
                   // turn end the watcher missed, which has no list.
                   ...(turn ? { turn: turn.key, events: turn.events } : {}) });
  summarise(tail.lastReply).then((summary) => push({
    session: id, state: 'summary', text: tail.lastReply.slice(0, 400), at: tail.replyAt || null, summary,
  })).catch((e) => console.error('[sessionWatch] summary push:', e.message));
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
 * `onReported(id, tail)` runs once the hub has been told the turn ended — the moment a session that
 * asked to close itself (`close_after_turn`) can go without losing its reply.
 */
export function syncSessionWatch(rows, onTurnEnd, onActivity = () => {}, onReported = () => {}) {
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
    const st = { id: r.id, path: r.transcript_path, offset, rest: '', watcher: null, timer: null, onTurnEnd, onActivity, onReported,
                 said: null, queue: [], chain: Promise.resolve(),
                 turn: null, last: null, partial: true, attachOffset: offset };
    watched.set(r.id, st);
    attach(st);
  }
  for (const id of [...watched.keys()]) if (!live.has(id)) detach(id);
}
