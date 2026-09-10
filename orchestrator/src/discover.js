// Which conversation each Claude Code process on this machine is in — read from Claude Code's own
// record, never inferred.
//
// Every running Claude Code process writes ~/.claude/sessions/<pid>.json: the id of the conversation
// it is having and its working directory. It rewrites the id when `/clear` starts a new conversation
// (verified 2026-09-10: same pid, the id went c5b343ee → 67e5a855) and deletes the file when the
// process exits. The conversation id IS the transcript's filename, so a process's transcript is a
// lookup.
//
// Inferring it from outside — transcript birth times, the newest transcript in a project directory —
// is what this file used to do, and it cannot be made safe: every session opened in the home directory
// shares one project directory, so a new session there looked exactly like a `/clear` in each older
// one. The Mac logged 336 rebinds against 42 adoptions before that was found, and rows were reported
// saying each other's words.
//
// Shared by both machines. The Mac adopts every `claude` in a terminal; the Pi reads the record of
// each process it launched in tmux.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { projectDir } from './transcripts.js';

const exec = promisify(execFile);
const SESSIONS_DIR = process.env.CLAUDE_SESSIONS_DIR || path.join(os.homedir(), '.claude', 'sessions');

/** The conversation process `pid` is in, from Claude Code's own record; null when it has none. */
export function sessionOf(pid) {
  let d;
  // No file: not a Claude Code process, or one that has not written its record yet (it does within
  // a second of starting, so the next look finds it).
  try { d = JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, `${pid}.json`), 'utf8')); }
  catch { return null; }
  if (!d || typeof d.sessionId !== 'string' || typeof d.cwd !== 'string') return null;
  return {
    claudeId: d.sessionId, cwd: d.cwd,
    transcriptPath: path.join(projectDir(d.cwd), `${d.sessionId}.jsonl`),
    repo: d.cwd === os.homedir() ? 'root' : path.basename(d.cwd),
    // Whether a turn is in flight, from the process itself — so a turn long enough to push its own
    // prompt out of the window readTail reads is still reported as working. And, while it is idle,
    // when its last turn FINISHED: statusUpdatedAt is when the status last changed, so while it is busy
    // that is the start of the current turn, which earns no recency. Null when not recorded.
    busy: d.status === 'busy' ? true : d.status === 'idle' ? false : null,
    finishedAt: d.status === 'idle' && Number.isFinite(d.statusUpdatedAt) ? new Date(d.statusUpdatedAt).toISOString() : null,
  };
}

/**
 * Every `claude` sitting in a terminal on this machine, with the conversation it is in. Oldest first.
 *
 * A process with no controlling tty is nobody's session: the voice worker's SDK client writes the
 * same record, and so would anything else headless, but nobody is sitting in it.
 */
export async function liveClaudeSessions() {
  let stdout;
  // `comm` LAST: macOS truncates it to 16 characters when another column follows, which turned a
  // `claude` launched by full path (/Users/jaredgantt/.local/bin/claude) into "/Users/jaredgant" and
  // made that session invisible.
  try { ({ stdout } = await exec('ps', ['-Ao', 'pid=,tty=,lstart=,comm='], { timeout: 10000 })); }
  catch { return []; }
  return stdout.split('\n').map((l) => l.trim()).filter(Boolean)
    .map((l) => {
      // lstart is five fields ("Wed Sep  9 16:02:54 2026"); comm is the rest of the line.
      const m = l.match(/^(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.+)$/);
      if (!m) return null;
      const [, pid, tty, lstart, comm] = m;
      if (tty === '??' || tty === '?') return null;
      if (comm.split('/').pop() !== 'claude') return null;
      const startedAt = new Date(lstart).getTime();
      if (!Number.isFinite(startedAt)) return null;
      const s = sessionOf(pid);
      return s ? { pid: Number(pid), tty: `/dev/${tty}`, startedAt, ...s } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.startedAt - b.startedAt);
}

/**
 * The session a launch just started on `tty`: the first `claude` there that began after `since`.
 * Polled, because the tab opens before claude does. `ps` reports start times to the whole second, so
 * `since` carries a second of slack from the caller.
 */
export async function awaitSessionOn(tty, since, { attempts = 90, delayMs = 1000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const hit = (await liveClaudeSessions()).find((s) => s.tty === tty && s.startedAt >= since);
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}
