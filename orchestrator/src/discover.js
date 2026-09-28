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

function readRecord(file) {
  try { return JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, file), 'utf8')); }
  catch { return null; }
}

/**
 * A session sent to the background keeps its terminal process, but the conversation goes on in a
 * separate `bg` process under a NEW id: the terminal's record keeps the old id and names the job as
 * `parkedJobId`, and the job's own record (`kind: "bg"`, the same `jobId`) holds the conversation
 * actually being had, with its own transcript and busy state. Read the terminal's record alone and the
 * row watches a transcript nothing writes to any more: no replies, no activity, sorted by the moment it
 * was backgrounded.
 */
function jobRecord(jobId) {
  let files;
  try { files = fs.readdirSync(SESSIONS_DIR); } catch { return null; }
  for (const f of files) {
    if (!f.endsWith('.json')) continue;
    const d = readRecord(f);
    if (d && d.kind === 'bg' && d.jobId === jobId) return d;
  }
  return null;
}

/** The conversation process `pid` is in, from Claude Code's own record; null when it has none. */
export function sessionOf(pid) {
  // No file: not a Claude Code process, or one that has not written its record yet (it does within
  // a second of starting, so the next look finds it).
  let d = readRecord(`${pid}.json`);
  if (d && typeof d.parkedJobId === 'string') d = jobRecord(d.parkedJobId) || d;
  if (!d || typeof d.sessionId !== 'string' || typeof d.cwd !== 'string') return null;
  return {
    claudeId: d.sessionId, cwd: d.cwd,
    transcriptPath: path.join(projectDir(d.cwd), `${d.sessionId}.jsonl`),
    repo: d.cwd === os.homedir() ? 'root' : path.basename(d.cwd),
    // Whether a turn is in flight, and when that last changed — which is the last turn boundary. From
    // the process itself, so a turn long enough to push its own prompt out of the window readTail
    // reads is still reported as working, and as active from when it began. Null when not recorded.
    busy: d.status === 'busy' ? true : d.status === 'idle' ? false : null,
    statusAt: Number.isFinite(d.statusUpdatedAt) ? new Date(d.statusUpdatedAt).toISOString() : null,
    // What it is blocked on ('permission prompt'), or null. Typing into it then is not a message: the
    // keys go to the dialog, and a return there answers it.
    waiting: d.status === 'waiting' ? (d.waitingFor || 'input') : null,
  };
}

/**
 * `sessionOf(pid)`, waiting up to `timeoutMs` for Claude Code to write the record if it has not yet.
 * Woken by the sessions directory changing; the timeout only bounds a process that never writes one.
 */
export function awaitSession(pid, timeoutMs = 15000) {
  const now = sessionOf(pid);
  if (now) return Promise.resolve(now);
  return new Promise((resolve) => {
    let watcher = null;
    const done = (s) => { clearTimeout(timer); try { watcher?.close(); } catch {} resolve(s); };
    const timer = setTimeout(() => done(sessionOf(pid)), timeoutMs);
    try {
      watcher = fs.watch(SESSIONS_DIR, () => { const s = sessionOf(pid); if (s) done(s); });
    } catch { /* no directory — the timeout's own check is all there is */ }
  });
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
