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

const exec = promisify(execFile);

// Reading transcripts is not Pi-specific — the Mac's consumer needs the same idle signal and the
// same final reply — so it lives in one place and is re-exported here for this module's callers.
export { readTail } from '../orchestrator/src/transcripts.js';

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
    let alive = false, pid = null;
    try {
      const { stdout: panes } = await exec('tmux', ['list-panes', '-t', name, '-F', '#{pane_current_command}\t#{pane_pid}'], { timeout: 10000 });
      // tmux keeps the shell alive after claude exits, so "the session exists" is a different
      // question from "claude is in it". Only the latter counts as live. launch.sh `exec`s claude, so
      // the pane's own process IS claude — the pid its session record is filed under.
      const hit = panes.split('\n').map((l) => l.trim().split('\t')).find(([c]) => c === 'claude');
      if (hit) { alive = true; pid = Number(hit[1]) || null; }
    } catch { alive = false; }
    out.push({ name, alive, created, pid });
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
