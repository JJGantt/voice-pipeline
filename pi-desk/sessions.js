// Session lifecycle for the Pi: residency, eviction, resume, and the queue behind them.
//
// THE TWO LIMITS ARE DIFFERENT, and conflating them is the mistake this module exists to avoid.
// An idle session costs no CPU at all — measured at 00:00:00 of CPU time across 17 minutes of wall
// clock — but holds ~334MB permanently on a 4GB machine. So:
//
//   RESIDENT_MAX  is a MEMORY bound: how many sessions may exist at once.
//   WORKING_MAX   is a CPU bound:    how many may be mid-turn at once.
//
// Holding a finished session resident is nearly free and is usually what you want, because evicting
// it and resuming later costs tokens (a resume re-sends the conversation). Eviction is therefore a
// last resort, taken only when a new session needs a slot.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const exec = promisify(execFile);
const CLAUDE_PROJECTS = path.join(os.homedir(), '.claude', 'projects');

/** Claude Code's directory name for a working directory: the path with / and . turned into dashes. */
function projectDir(cwd) {
  return path.join(CLAUDE_PROJECTS, cwd.replace(/[/.]/g, '-'));
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
 * Read a transcript's tail: whether the session is mid-turn, and its last assistant reply.
 *
 * THIS IS THE IDLE SIGNAL, and it has to come from here rather than from CPU. A session waiting on the
 * API and a session that finished an hour ago are both at 0% CPU and indistinguishable from the
 * outside; evicting on CPU would kill work mid-thought. The transcript knows: a user turn with no
 * assistant reply after it means a turn is in flight.
 */
export function readTail(transcriptPath) {
  let lines;
  try { lines = fs.readFileSync(transcriptPath, 'utf8').split('\n'); }
  catch { return { working: false, lastReply: null, turns: 0, bytes: 0, lastActivity: 0 }; }

  let lastRole = null, lastReply = null, turns = 0;
  for (const line of lines) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    const t = d.type;
    if (t !== 'user' && t !== 'assistant') continue;
    turns++;
    lastRole = t;
    if (t === 'assistant') {
      const blocks = d.message?.content;
      if (Array.isArray(blocks)) {
        const txt = blocks.filter((b) => b?.type === 'text').map((b) => b.text).join('').trim();
        if (txt) lastReply = txt;
      }
    }
  }
  // mtime is the last time anything was written to this conversation — the recency signal eviction
  // sorts on. Read from the file rather than from the parsed turns because a transcript's timestamps
  // are not guaranteed to be present on every record type.
  let bytes = 0, lastActivity = 0;
  try { const st = fs.statSync(transcriptPath); bytes = st.size; lastActivity = st.mtimeMs; } catch {}
  // A trailing user turn means Claude has been asked something and has not answered yet.
  return { working: lastRole === 'user', lastReply, turns, bytes, lastActivity };
}

/** tmux sessions this daemon owns, and whether a claude process is actually inside each. */
export async function liveSessions() {
  let names = [];
  try {
    // session_created is the ONE authoritative start time. The database row's `started` is not: a row
    // can be deleted and recreated, which resets it to now and then wrongly excludes the session's own
    // (older) transcript from adoption. tmux knows when the session actually began.
    const { stdout } = await exec('tmux', ['list-sessions', '-F', '#{session_name}\t#{session_created}'], { timeout: 10000 });
    names = stdout.split('\n').map((l) => l.trim()).filter(Boolean)
      .map((l) => { const [n, c] = l.split('\t'); return { n, created: Number(c) * 1000 || 0 }; })
      .filter((x) => x.n.startsWith('hub-'));
  } catch { return []; }   // no server running is a valid state, not an error

  const out = [];
  for (const { n: name, created } of names) {
    let alive = false;
    try {
      const { stdout: panes } = await exec('tmux', ['list-panes', '-t', name, '-F', '#{pane_current_command}'], { timeout: 10000 });
      // tmux keeps the shell alive after claude exits, so "the session exists" is a different
      // question from "claude is in it". Only the latter counts as live.
      alive = panes.split('\n').some((c) => c.trim() === 'claude');
    } catch { alive = false; }
    out.push({ name, alive, created });
  }
  return out;
}

/**
 * Close a session so it can be resumed later.
 *
 * Sends the shell's own exit path rather than `tmux kill-session`, which is a SIGKILL: a hard kill
 * during a tool call can leave a repo half-edited and may lose whatever the transcript had not yet
 * flushed. C-c interrupts anything in flight, then the session is asked to end; the kill is only a
 * backstop for a process that ignores both.
 */
export async function evict(name) {
  try {
    await exec('tmux', ['send-keys', '-t', name, 'C-c'], { timeout: 10000 });
    await new Promise((r) => setTimeout(r, 800));
    await exec('tmux', ['send-keys', '-t', name, '/exit', 'Enter'], { timeout: 10000 });
    await new Promise((r) => setTimeout(r, 2500));
  } catch { /* fall through to the backstop */ }
  try { await exec('tmux', ['kill-session', '-t', name], { timeout: 10000 }); } catch {}
}

/**
 * Which resident session to evict to make room, or null if none may be.
 *
 * RECENCY, and nothing else. The least recently active session goes first.
 *
 * This used to sort by transcript size — cheapest to resume, since resuming re-sends the
 * conversation. That optimised for the wrong thing and cost a live session: a short transcript is
 * usually the conversation just started, and a long one is usually an old finished job, so
 * "cheapest to restore" and "least likely to be wanted" point in opposite directions. A ten-hour-old
 * session that had said its work was deployed was protected, and the one Jared was mid-conversation
 * with was killed, because it was 66KB smaller.
 *
 * Never a working one either — that check remains, but it is weak on its own: `working` means a turn
 * is in flight, and between a reply and the next message a live conversation is indistinguishable
 * from an abandoned one. Recency is what actually separates them.
 */
export function pickEviction(residents) {
  const idle = residents.filter((r) => !r.working);
  if (!idle.length) return null;
  return idle.slice().sort((a, b) => (a.lastActivity || 0) - (b.lastActivity || 0))[0];
}
