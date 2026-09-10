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
  try {
    const fd = fs.openSync(transcriptPath, 'r');
    try {
      const buf = Buffer.allocUnsafe(st.size - from);
      fs.readSync(fd, buf, 0, buf.length, from);
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
      // Tool results are recorded as user records too. They are the model's own work coming back,
      // not Jared saying something, so they must not look like the start of a new turn.
      const blocks = d.message?.content;
      const isToolResult = Array.isArray(blocks) && blocks.length > 0
        && blocks.every((b) => b?.type === 'tool_result');
      if (!isToolResult) { working = true; turnText = []; promptAt = d.timestamp || promptAt; }
      continue;
    }
    const msg = d.message || {};
    const blocks = msg.content;
    if (Array.isArray(blocks)) {
      const txt = blocks.filter((b) => b?.type === 'text').map((b) => b.text).join('').trim();
      if (txt) { turnText.push(txt); anyAssistantText = txt; }
    }
    if (msg.stop_reason === 'end_turn') {
      working = false;
      const joined = turnText.join('\n\n').trim();
      if (joined) { finalReply = joined; replyAt = d.timestamp || null; }
    }
  }

  if (finalReply === null && from > 0) return readTail(transcriptPath, { maxBytes: maxBytes * 4 });

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
  return { working, lastReply: finalReply ?? anyAssistantText, replyAt, turnAt,
           title: customTitle || aiTitle, customTitle,
           bytes: st.size, lastActivity: newestTs || st.mtimeMs };
}
