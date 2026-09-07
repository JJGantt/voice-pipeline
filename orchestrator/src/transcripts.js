// Reading a Claude Code transcript: which one belongs to a session we just launched, and what that
// session has actually said.
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
 * The transcript Claude Code just started for a session in `cwd`.
 *
 * Matched by BIRTH TIME rather than by asking Claude, because there is no way to ask: the id is
 * generated inside the process.
 *
 * It used to match on mtime, and that produced two sessions sharing one transcript. mtime bounds
 * nothing while a session is alive: an older session that is still being written keeps a fresh mtime
 * forever, so a second session launched in the same directory adopted the first one's file and both
 * rows recorded the same claude id. Two slots, one conversation. Birth time cannot do that — a
 * transcript created before this launch is not this launch's, however recently it was touched.
 *
 * `taken` excludes ids already claimed by a live session, which closes the same hole from the other
 * side if two launches ever land inside one filesystem timestamp.
 *
 * The window is generous (90s) because Claude Code takes a while to come up on a Pi and writes its
 * transcript only once it does. A 10s window silently produced sessions with no recorded id — which
 * looks like nothing at all going wrong until you try to resume one and find you cannot.
 */
export function findTranscript(cwd, since, { attempts = 90, delayMs = 1000, taken = new Set() } = {}) {
  const dir = projectDir(cwd);
  return new Promise((resolve) => {
    let n = 0;
    const tick = () => {
      try {
        const hit = fs.readdirSync(dir)
          .filter((f) => f.endsWith('.jsonl'))
          .filter((f) => !taken.has(f.replace(/\.jsonl$/, '')))
          .map((f) => ({ f, p: path.join(dir, f), m: fs.statSync(path.join(dir, f)).birthtimeMs }))
          .filter((x) => x.m >= since)
          .sort((a, b) => b.m - a.m)[0];
        if (hit) return resolve({ id: hit.f.replace(/\.jsonl$/, ''), path: hit.p });
      } catch { /* the directory does not exist until the first session in that repo */ }
      if (++n >= attempts) return resolve(null);
      setTimeout(tick, delayMs);
    };
    tick();
  });
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
  const empty = { working: false, lastReply: null, replyAt: null, title: null, customTitle: null, bytes: 0, lastActivity: 0 };
  // Only ever a regular file. A path that is anything else must not be read: `desk_sessions` rows
  // written before the tty got its own column carry a device path here, and readFileSync on
  // /dev/ttys007 BLOCKS THE PROCESS FOR EVER waiting for terminal input — the daemon stops beating,
  // stops draining commands, and steals the keystrokes of whoever is typing in that tab.
  let st;
  try { st = fs.statSync(transcriptPath); } catch { return empty; }
  if (!st.isFile()) return empty;
  // Only the tail is read. These files reach tens of megabytes — one project directory here holds
  // 31MB — and every tick would otherwise read all of it to learn the last few lines. The window is
  // far larger than a turn, and the leading fragment is dropped because the read starts mid-line.
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
      if (!isToolResult) { working = true; turnText = []; }
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
  return { working, lastReply: finalReply ?? anyAssistantText, replyAt,
           title: customTitle || aiTitle, customTitle,
           bytes: st.size, lastActivity: newestTs || st.mtimeMs };
}
