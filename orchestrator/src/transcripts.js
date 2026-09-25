// Reading a Claude Code transcript: what a session has actually said, and when it last did anything.
// Which transcript belongs to which process is not decided here — see discover.js.
//
// Shared by both desk consumers rather than copied. The Pi has always read transcripts (it is the
// only way to tell an idle session from one mid-thought before evicting it); the Mac now needs the
// same answers for a different reason — its sessions live in Terminal tabs nothing else watches, so
// without this a Mac session's reply and its liveness are both invisible.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const PROJECTS = process.env.CLAUDE_PROJECTS_DIR || path.join(os.homedir(), '.claude', 'projects');

/** Claude Code's directory name for a working directory: the path with / and . turned into dashes. */
export function projectDir(cwd) {
  return path.join(PROJECTS, cwd.replace(/[/.]/g, '-'));
}

/**
 * A `user` record as the start of a turn: null when it is not one, else `{ prompt }`, whose prompt is
 * null when the turn was started by something he did not say.
 */
export function turnStart(d) {
  // Tool results are recorded as user records too. They are the model's own work coming back, not
  // Jared saying something, so they must not look like the start of a new turn.
  const blocks = d.message?.content;
  if (Array.isArray(blocks) && blocks.length > 0 && blocks.every((b) => b?.type === 'tool_result')) return null;
  // Claude Code's own injected context (`isMeta`) starts a turn without being a prompt. So does the
  // summary it writes when it compacts a conversation mid-turn (`isCompactSummary`): pages of its
  // own recap, recorded as a user record, which is not something he said.
  if (d.isMeta || d.isCompactSummary) return { prompt: null, meta: true };
  const text = typeof blocks === 'string' ? blocks.trim()
    : Array.isArray(blocks) ? blocks.filter((b) => b?.type === 'text').map((b) => b.text).join('\n').trim()
    : '';
  if (!text || !fromHim(text)) return { prompt: null };
  return resolvePrompt(text);
}

/**
 * Whether text recorded as a user message is something he said. A background task finishing delivers
 * a `<task-notification>` block the same way, as a turn of its own or queued into the one running; it
 * is real, but it is not him, and shown as his words it is a wall of markup in his colour. Both places
 * a user message becomes his words (a turn's prompt here, and a queued message in sessionWatch) ask this.
 */
export function fromHim(text) {
  return !String(text || '').trimStart().startsWith('<task-notification>');
}

// A DICTATED MESSAGE TOO LONG FOR THE TERMINAL IS STILL THE MESSAGE.
//
// A prompt over TTY_INPUT_MAX characters is not typed into the tty (AppleScript cannot hand it over in
// one go), so `sendToTty` parks the words in ~/.claude-desk-prompts and types a one-line stub naming the
// file. Line breaks alone do not do this; they are typed. That stub is what lands in the transcript, so every surface that
// draws "his last message" drew the stub: the session list, the session view, the wall, `desk_turns`.
//
// Resolved HERE because this is the single place a user record becomes prompt text, so one
// substitution covers all of them — the message is displayed the same as one short enough to type.
//
// Only paths inside the stash directory are read. The path arrives inside transcript text, which is
// not a place to take a filename from on trust.
/**
 * What he actually said, from whatever text was recorded.
 *
 * A stub naming a stashed file resolves to the words it stands for; anything else is already them.
 * `stashed` travels with it so a caller can recognise the session's own echo of those words — the stub
 * asks for one, and it is the only place a reply quotes a whole prompt back.
 *
 * Its own function because a prompt reaches the transcript two ways: as a `user` record when a turn
 * consumes it, and as a `queue-operation` when it is typed while one is already running.
 */
export function resolvePrompt(text) {
  const body = unstash(text);
  return body ? { prompt: unwrapPastes(body), stashed: true }
              : { prompt: unwrapPastes(text) || null };
}

// A PASTE IS STILL HIS WORDS, NOT MARKUP.
//
// Claude Code wraps pasted input in <pasted_content id="N">…</pasted_content id="N"> — the id is
// random per block and the closing tag repeats it. That wrapper is an artifact of how the words
// reached the terminal, so persisting it puts the tags on screen and makes the same message look
// like a second, different one beside the clean copy the live path already showed. Measured in
// desk_turns: a 525-byte prompt stored with the opening tag still on the front of it.
//
// The tags are removed and the content kept, rather than the block being extracted, because a
// message can be part typed and part pasted and both halves are his. An unpaired tag is stripped
// too: a prompt can be stored truncated, and half a wrapper is no more readable than a whole one.
const PASTE_BLOCK = /<pasted_content\b[^>]*>\r?\n?([\s\S]*?)\r?\n?<\/pasted_content\b[^>]*>/g;
const PASTE_ORPHAN = /<\/?pasted_content\b[^>]*>\r?\n?/g;
export function unwrapPastes(text) {
  return String(text || '').replace(PASTE_BLOCK, '$1').replace(PASTE_ORPHAN, '').trim();
}

const STASH_DIR = path.join(os.homedir(), '.claude-desk-prompts');
const STASH_RE = /^\[A message from Jared, too long to type into this terminal\. It is in (\S+) /;
const stashed = new Map();   // path -> text. The file is written once and never rewritten.

/**
 * A distinctive middle slice of some text, whitespace-flattened — for asking "does this contain that".
 *
 * From a fifth of the way in, because the two ends are where a quotation differs: a reply prefixes its
 * own words and a blockquote adds a marker to the first line, while the middle of a dictated paragraph
 * is reproduced exactly.
 */
export function gist(text, len = 60) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  if (flat.length < len) return null;
  return flat.slice(Math.floor(flat.length / 5), Math.floor(flat.length / 5) + len);
}
export const flatten = (t) => String(t || '').replace(/\s+/g, ' ').trim();
function unstash(text) {
  const m = STASH_RE.exec(String(text || ''));
  if (!m) return null;
  const file = path.resolve(m[1]);
  if (path.dirname(file) !== STASH_DIR) return null;
  if (stashed.has(file)) return stashed.get(file);
  let body = null;
  try { body = fs.readFileSync(file, 'utf8').trim() || null; } catch (_) { /* swept, or never written */ }
  if (body) stashed.set(file, body);
  return body;
}

/**
 * The start of the turn that was under way at byte `end`, found by reading BACKWARDS from there.
 *
 * A turn of tool calls is routinely several megabytes — 2.7MB measured, against the 512KB readTail
 * reads — so the prompt that opened it is outside the window exactly when the turn was a long one,
 * which is when the reply most needs its question beside it. Widening the window instead would parse
 * every record of those megabytes to find one; this reads a megabyte at a time, stops at the first
 * turn start it meets, and parses only the lines that could be one.
 *
 * Bytes, not strings, until a line is chosen: a chunk boundary can fall inside a multi-byte character,
 * and a newline byte never does. Returns undefined when no turn start is within `maxBytes`.
 */
function turnStartBefore(transcriptPath, end, { chunk = 1024 * 1024, maxBytes = 64 * 1024 * 1024 } = {}) {
  let fd;
  try { fd = fs.openSync(transcriptPath, 'r'); } catch { return undefined; }
  try {
    let pos = end, carry = Buffer.alloc(0);   // carry: the head of a line whose start is further back
    while (pos > 0 && end - pos < maxBytes) {
      const n = Math.min(chunk, pos);
      pos -= n;
      const buf = Buffer.allocUnsafe(n);
      fs.readSync(fd, buf, 0, n, pos);
      const data = Buffer.concat([buf, carry]);
      // Everything before the first newline belongs to a line that began earlier, unless this is
      // the top of the file.
      const first = pos === 0 ? -1 : data.indexOf(0x0a);
      if (first === -1 && pos > 0) { carry = data; continue; }
      carry = data.subarray(0, first + 1);
      let hi = data.length;
      while (hi > first + 1) {
        const nl = data.lastIndexOf(0x0a, hi - 2);
        const lo = Math.max(nl, first) + 1;
        const line = data.subarray(lo, hi);
        hi = lo;
        if (!line.includes('"type":"user"')) continue;
        let d;
        try { d = JSON.parse(line.toString('utf8')); } catch { continue; }
        const start = d.type === 'user' ? turnStart(d) : null;
        // Injected context is passed over, as readTail passes over it going forwards: the prompt
        // before it is the one that stands.
        if (start && !start.meta) return start;
      }
    }
  } catch { /* unreadable: the same as not found */ } finally { fs.closeSync(fd); }
  return undefined;
}

// What turnStartBefore found, per transcript, for the turn that ended at `replyAt` — so a long turn is
// searched for once when it ends, not on every tick for as long as the session then sits idle. A miss
// is kept too: a turn start beyond the search's reach would otherwise be searched for every 30s.
const promptCache = new Map();   // transcript path -> { replyAt, prompt }
const PROMPT_CACHE_MAX = 200;

/**
 * Read a transcript's tail: whether a turn is in flight, and the FINAL message of the last one.
 *
 * THIS IS THE IDLE SIGNAL, and it has to come from here rather than from CPU. A session waiting on
 * the API and a session that finished an hour ago are both at 0% CPU and indistinguishable from the
 * outside; evicting on CPU would kill work mid-thought.
 *
 * "Final" is not inferred. Claude Code writes one transcript record per content block, each carrying
 * the message's `stop_reason`, and a turn ends with `end_turn` — anything else (`tool_use`) is the
 * model still working. So the reply reported here is the text of the last COMPLETED turn, never a
 * half-formed one, and `replyAt` is when it landed.
 *
 * The last-assistant-text fallback is for transcripts with no end_turn record at all: one compacted,
 * or written by an older version. Reporting the last thing said beats reporting nothing.
 */
export function readTail(transcriptPath, { maxBytes = 512 * 1024 } = {}) {
  const empty = { working: false, lastReply: null, replyAt: null, turnAt: null, title: null, customTitle: null, bytes: 0, lastActivity: 0 };
  // Only ever a regular file. A path that is anything else must not be read: `desk_sessions` rows
  // written before the tty got its own column carry a device path here, and readFileSync on
  // /dev/ttys007 BLOCKS THE PROCESS FOR EVER waiting for terminal input — the daemon stops beating,
  // stops draining commands, and steals the keystrokes of whoever is typing in that tab.
  let st;
  try { st = fs.statSync(transcriptPath); } catch { return empty; }
  if (!st.isFile()) return empty;
  // Only the tail is read. These files reach tens of megabytes — one project directory here holds
  // 31MB — and every tick would otherwise read all of it to learn the last few lines. The leading
  // fragment is dropped because the read starts mid-line. A window with no completed turn in it is
  // widened (below): one long turn of tool calls fills 512KB on its own — measured, 137 records and no
  // text — and the reply reported is the last COMPLETED turn's, which is then further back.
  let lines;
  const from = Math.max(0, st.size - maxBytes);
  let windowStart = from;
  try {
    const fd = fs.openSync(transcriptPath, 'r');
    try {
      const buf = Buffer.allocUnsafe(st.size - from);
      fs.readSync(fd, buf, 0, buf.length, from);
      // Where the first WHOLE line starts, which is where a backwards search for a turn's opening
      // has to begin: the fragment dropped below may itself be that record.
      if (from > 0) windowStart = from + buf.indexOf(0x0a) + 1;
      lines = buf.toString('utf8').split('\n');
    } finally { fs.closeSync(fd); }
  } catch { return empty; }
  if (from > 0) lines.shift();

  let working = false, aiTitle = null, customTitle = null;
  let newestTs = 0;             // newest record timestamp — see the lastActivity note below
  let turnText = [];            // text blocks seen since the current turn began
  let finalReply = null, replyAt = null;
  let anyAssistantText = null;  // fallback when no turn ever completed
  let promptAt = null;          // when a turn was last started — see turnAt below
  let turnPrompt = null;        // what started the current turn
  let finalPrompt = null;       // what started the turn finalReply ended, so the two always pair
  let startSeen = false;        // a turn has started inside the window
  let finalStartSeen = false;   // ...before the end of the turn finalReply ended

  for (const line of lines) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    // Every record type, not just the conversational ones: a turn ends with a `system`
    // (turn_duration) record, and taking only user/assistant would report a turn as older than it is.
    if (typeof d.timestamp === 'string') {
      const ms = Date.parse(d.timestamp);
      if (ms > newestTs) newestTs = ms;
    }
    // Claude Code's own name for the conversation — a `/rename` beats the generated one. Free to
    // pick up here, and it is the only description a session nobody dispatched ever has.
    if (d.type === 'custom-title' && typeof d.customTitle === 'string') customTitle = d.customTitle;
    else if (typeof d.aiTitle === 'string') aiTitle = d.aiTitle;
    const t = d.type;
    if (t !== 'user' && t !== 'assistant') continue;
    if (t === 'user') {
      const start = turnStart(d);
      if (start) {
        working = true; turnText = []; promptAt = d.timestamp || promptAt;
        startSeen = true;
        // Injected context starts a turn without saying anything: the prompt before it stands.
        if (!start.meta) turnPrompt = start.prompt;
      }
      continue;
    }
    const msg = d.message || {};
    const blocks = msg.content;
    if (Array.isArray(blocks)) {
      // The reply is what the model says after its LAST tool call. Text before one is narration: it
      // streams as progress (sessionWatch), and joined onto the reply it made the answer open with
      // "Checking the logs first..." on the watch and in the notification.
      if (blocks.some((b) => b?.type === 'tool_use')) turnText = [];
      const txt = blocks.filter((b) => b?.type === 'text').map((b) => b.text).join('').trim();
      if (txt) { turnText.push(txt); anyAssistantText = txt; }
    }
    if (msg.stop_reason === 'end_turn') {
      working = false;
      const joined = turnText.join('\n\n').trim();
      if (joined) { finalReply = joined; replyAt = d.timestamp || null; finalPrompt = turnPrompt; finalStartSeen = startSeen; }
    }
  }

  if (finalReply === null && from > 0) return readTail(transcriptPath, { maxBytes: maxBytes * 4 });

  // The reply is in the window and the turn's opening is not: look further back for it, once.
  if (finalReply !== null && !finalStartSeen && from > 0) {
    const hit = promptCache.get(transcriptPath);
    if (replyAt && hit && hit.replyAt === replyAt) finalPrompt = hit.prompt;
    else {
      finalPrompt = turnStartBefore(transcriptPath, windowStart)?.prompt ?? null;
      promptCache.delete(transcriptPath);
      promptCache.set(transcriptPath, { replyAt, prompt: finalPrompt });
      if (promptCache.size > PROMPT_CACHE_MAX) promptCache.delete(promptCache.keys().next().value);
    }
  }

  // lastActivity is when this conversation last did something — the signal the idle reaper and
  // pickEviction both run on. It is the newest record TIMESTAMP, and it must not be the file's mtime.
  //
  // mtime was the obvious choice and it silently disabled the reaper for as long as it was in place.
  // Claude Code keeps appending untimestamped bookkeeping records to a transcript long after the
  // conversation stops — `last-prompt`, `ai-title`, `mode`, `permission-mode`, `atis-latch`, and the
  // `bridge-session` record that Remote Control rewrites. Each one moves mtime, so an idle session
  // looks freshly active for ever. Measured on 2026-09-07: two Pi sessions idle for 8h and 11h were
  // checked every 30s and judged active every time, until the kernel OOM-killed them instead. Across
  // 203 transcripts on the Mac, 59 had mtimes 1h to 145h past their last real record.
  //
  // Falling back to mtime when nothing in the window carries a timestamp is deliberate: a compacted
  // or very old transcript is then judged exactly as it was before, rather than reporting 0 and
  // being treated as the least valuable session on the machine.
  // Two names, kept apart on purpose: `customTitle` is Jared renaming the session by hand, `aiTitle`
  // is Claude Code's guess from the content. A hand-written name outranks anything derived — see the
  // label rule in deskSessions.js.
  //
  // turnAt is the last TURN BOUNDARY — a prompt sent or a turn finished — and it is what every surface
  // orders, numbers and focuses sessions by. Deliberately not lastActivity: that moves with every tool
  // call, so two working sessions would trade places mid-turn and "session two" would name a different
  // session by the time the sentence saying it was resolved.
  const turnAt = [promptAt, replyAt].filter(Boolean).sort().pop() || null;
  // lastPrompt is the prompt of the turn lastReply ended, or null when the reply is only the fallback
  // text of a turn that never finished (its prompt would not be the one it answers).
  return { working, lastReply: finalReply ?? anyAssistantText, lastPrompt: finalReply !== null ? finalPrompt : null,
           replyAt, turnAt,
           title: customTitle || aiTitle, customTitle,
           bytes: st.size, lastActivity: newestTs || st.mtimeMs };
}
