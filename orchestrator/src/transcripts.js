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
export function readTail(transcriptPath) {
  const empty = { working: false, lastReply: null, replyAt: null, turns: 0, bytes: 0, lastActivity: 0 };
  // Only ever a regular file. A path that is anything else must not be read: `desk_sessions` rows
  // written before the tty got its own column carry a device path here, and readFileSync on
  // /dev/ttys007 BLOCKS THE PROCESS FOR EVER waiting for terminal input — the daemon stops beating,
  // stops draining commands, and steals the keystrokes of whoever is typing in that tab.
  let st;
  try { st = fs.statSync(transcriptPath); } catch { return empty; }
  if (!st.isFile()) return empty;
  let lines;
  try { lines = fs.readFileSync(transcriptPath, 'utf8').split('\n'); }
  catch { return empty; }

  let working = false, turns = 0;
  let turnText = [];            // text blocks seen since the current turn began
  let finalReply = null, replyAt = null;
  let anyAssistantText = null;  // fallback when no turn ever completed

  for (const line of lines) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    const t = d.type;
    if (t !== 'user' && t !== 'assistant') continue;
    turns++;
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

  // mtime is the last time anything was written to this conversation — the recency signal eviction
  // sorts on. Taken from the file rather than from the parsed turns because a transcript's
  // timestamps are not guaranteed to be present on every record type.
  return { working, lastReply: finalReply ?? anyAssistantText, replyAt,
           turns, bytes: st.size, lastActivity: st.mtimeMs };
}
