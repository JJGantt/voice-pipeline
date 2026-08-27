// Claude sessions running on this Mac that nobody dispatched — the ones Jared opened himself.
//
// A dispatched session records its own identity at launch. One started by typing `claude` in a tab
// announces nothing, so it has to be recognised from outside: a `claude` process with a controlling
// tty (which excludes the headless `claude -p` the worker runs, and the MCP children, neither of
// which is a session anyone is sitting in), its working directory, and the transcript it created.
//
// MATCHING A PROCESS TO ITS TRANSCRIPT is the whole difficulty, because nothing links them: there is
// no session id in the environment, and Claude Code appends-and-closes the file, so it is not held
// open either. What is reliable is BIRTH ORDER — a session writes its transcript within seconds of
// starting. So processes are paired oldest-first with the EARLIEST unclaimed transcript in their
// project directory born inside a window around their start.
//
// "Newest transcript created since this process started" — the rule the launch path can safely use,
// because it looks the instant it launches — is wrong here: an older session still running would
// adopt a newer session's file. Measured on two live sessions in one directory, both claimed the
// same transcript that way, and each was then reported as saying the other's words.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { projectDir } from './transcripts.js';

const exec = promisify(execFile);

// A transcript belongs to the process that started just before it. Generous, because a cold start on
// a big repo takes a while; bounded, because a match outside it is a guess, and reporting no session
// is better than reporting one with someone else's conversation in it.
const MATCH_WINDOW_MS = 180000;
const MATCH_SLACK_MS = 5000;   // the file can be stamped a moment before the process clock

/** Every `claude` sitting in a terminal, oldest first. */
export async function liveClaudeProcesses() {
  let stdout;
  try { ({ stdout } = await exec('ps', ['-Ao', 'pid=,tty=,comm=,lstart='], { timeout: 10000 })); }
  catch { return []; }
  return stdout.split('\n').map((l) => l.trim()).filter(Boolean)
    .map((l) => {
      const m = l.match(/^(\d+)\s+(\S+)\s+(\S+)\s+(.+)$/);
      if (!m) return null;
      const [, pid, tty, comm, lstart] = m;
      // No controlling tty means nobody is sitting in it: the worker's `claude -p`, an MCP child,
      // the orchestrator's own pty. Those are not sessions to manage.
      if (tty === '??' || tty === '?') return null;
      if (comm.split('/').pop() !== 'claude') return null;
      const startedAt = new Date(lstart).getTime();
      return Number.isFinite(startedAt) ? { pid, tty: `/dev/${tty}`, startedAt } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.startedAt - b.startedAt);
}

async function cwdOf(pid) {
  try {
    const { stdout } = await exec('lsof', ['-a', '-p', pid, '-d', 'cwd', '-Fn'], { timeout: 10000 });
    const line = stdout.split('\n').find((l) => l.startsWith('n'));
    return line ? line.slice(1) : null;
  } catch { return null; }
}

/** The earliest transcript in `cwd`'s project directory born just after `startedAt`. */
function transcriptFor(cwd, startedAt, taken) {
  const dir = projectDir(cwd);
  let names;
  try { names = fs.readdirSync(dir); } catch { return null; }
  return names.filter((f) => f.endsWith('.jsonl'))
    .map((f) => ({ id: f.slice(0, -6), p: path.join(dir, f) }))
    .filter((x) => !taken.has(x.id))
    .map((x) => { try { return { ...x, birth: fs.statSync(x.p).birthtimeMs }; } catch { return null; } })
    .filter((x) => x && x.birth >= startedAt - MATCH_SLACK_MS && x.birth <= startedAt + MATCH_WINDOW_MS)
    .sort((a, b) => a.birth - b.birth)[0] || null;
}

/**
 * Sessions running here that are not already accounted for.
 *
 * `knownTtys` are tabs already tracked (a dispatched session is a live claude process too, and must
 * not be adopted a second time under another identity). `takenIds` are transcripts already spoken
 * for, so a tracked session's file cannot be handed to the process next to it.
 */
export async function discoverSessions({ knownTtys = new Set(), takenIds = new Set() } = {}) {
  const claimed = new Set(takenIds);
  const found = [];
  for (const p of await liveClaudeProcesses()) {
    if (knownTtys.has(p.tty)) continue;
    const cwd = await cwdOf(p.pid);
    if (!cwd) continue;
    const hit = transcriptFor(cwd, p.startedAt, claimed);
    // No confident match: leave it alone. It will be picked up on a later tick if its transcript
    // appears, and until then reporting nothing beats reporting the wrong conversation.
    if (!hit) continue;
    claimed.add(hit.id);
    found.push({ ...p, cwd, repo: cwd === os.homedir() ? 'root' : path.basename(cwd),
                 claudeId: hit.id, transcriptPath: hit.p });
  }
  return found;
}
