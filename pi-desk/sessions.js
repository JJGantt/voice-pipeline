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
 * Matched by mtime rather than by asking Claude, because there is no way to ask: the id is generated
 * inside the process. `since` bounds it to files created after the launch, so a long-lived transcript
 * in the same directory can never be mistaken for the new one.
 */
export function findTranscript(cwd, since, { attempts = 20, delayMs = 500 } = {}) {
  const dir = projectDir(cwd);
  return new Promise((resolve) => {
    let n = 0;
    const tick = () => {
      try {
        const hit = fs.readdirSync(dir)
          .filter((f) => f.endsWith('.jsonl'))
          .map((f) => ({ f, p: path.join(dir, f), m: fs.statSync(path.join(dir, f)).mtimeMs }))
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
  catch { return { working: false, lastReply: null, turns: 0, bytes: 0 }; }

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
  let bytes = 0;
  try { bytes = fs.statSync(transcriptPath).size; } catch {}
  // A trailing user turn means Claude has been asked something and has not answered yet.
  return { working: lastRole === 'user', lastReply, turns, bytes };
}

/** tmux sessions this daemon owns, and whether a claude process is actually inside each. */
export async function liveSessions() {
  let names = [];
  try {
    const { stdout } = await exec('tmux', ['list-sessions', '-F', '#{session_name}'], { timeout: 10000 });
    names = stdout.split('\n').map((n) => n.trim()).filter((n) => n.startsWith('hub-'));
  } catch { return []; }   // no server running is a valid state, not an error

  const out = [];
  for (const name of names) {
    let alive = false;
    try {
      const { stdout: panes } = await exec('tmux', ['list-panes', '-t', name, '-F', '#{pane_current_command}'], { timeout: 10000 });
      // tmux keeps the shell alive after claude exits, so "the session exists" is a different
      // question from "claude is in it". Only the latter counts as live.
      alive = panes.split('\n').some((c) => c.trim() === 'claude');
    } catch { alive = false; }
    out.push({ name, alive });
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
 * Never a working one — that is the whole point of reading transcripts. Among idle candidates, the
 * SMALLEST transcript goes first: resuming re-sends the conversation, so evicting a long session and
 * bringing it back costs materially more than evicting a short one. Cheapest to restore, first out.
 */
export function pickEviction(residents) {
  const idle = residents.filter((r) => !r.working);
  if (!idle.length) return null;
  return idle.slice().sort((a, b) => (a.bytes || 0) - (b.bytes || 0))[0];
}
