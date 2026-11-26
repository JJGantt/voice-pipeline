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
  try { files = fs.readdirSync(SESSIONS_DIR); }
  catch (e) { if (e.code !== 'ENOENT') console.warn('[discover] reading %s:', SESSIONS_DIR, e.message); return null; }
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
    // A background shell still running while no turn is (`run_in_background`, or a command moved to the
    // background on its timeout). Claude Code writes `shell` in place of `idle` for exactly that and for
    // nothing else; a background AGENT leaves it `idle`, which is what backgroundAgents() is for.
    shell: d.status === 'shell',
    // When this process started: anything a background agent left behind before it cannot be running in it.
    recordStartedAt: Number.isFinite(d.startedAt) ? d.startedAt : null,
    // The process actually holding the conversation (a backgrounded job's, when it has one): its children
    // are the shells.
    recordPid: Number.isFinite(d.pid) ? d.pid : Number(pid),
  };
}

/**
 * The background agents still running in a conversation — each one's description, as its Agent call
 * named it. Claude Code keeps a record beside each subagent's transcript
 * (`<conversation>/subagents/agent-<id>.meta.json`), written at launch and rewritten with `finished`
 * when the agent ends. A record without it is running — unless it predates this process (`sinceMs`),
 * which is a process that was killed with the agent in flight and never wrote the end.
 */
export function backgroundAgents(transcriptPath, sinceMs) {
  if (!transcriptPath || !sinceMs) return [];
  const dir = path.join(transcriptPath.replace(/\.jsonl$/, ''), 'subagents');
  let files;
  try { files = fs.readdirSync(dir); }
  catch (e) { if (e.code !== 'ENOENT') console.warn('[discover] reading %s:', dir, e.message); return []; }
  const out = [];
  for (const f of files) {
    if (!f.endsWith('.meta.json')) continue;
    const file = path.join(dir, f);
    let m, st;
    try { st = fs.statSync(file); m = JSON.parse(fs.readFileSync(file, 'utf8')); } catch { continue; }
    if (m.requestShape !== 'background' || m.finished || m.stoppedByUser || m.sweptByStop) continue;
    if (st.mtimeMs < sinceMs) continue;
    out.push({ at: st.mtimeMs, label: String(m.description || m.agentType || 'agent').slice(0, 120) });
  }
  return out.sort((a, b) => a.at - b.at).map((a) => a.label);
}

/** The command a Bash-tool shell runs: Claude Code wraps it as `… && eval '<command>' < /dev/null && …`. */
export function shellCommand(args) {
  const i = args.indexOf(" eval '");
  if (i < 0) return null;
  const end = args.lastIndexOf("' < /dev/null");
  if (end <= i) return null;
  return args.slice(i + 7, end).replace(/'"'"'/g, "'").replace(/'\\''/g, "'");
}

/** The commands of the Bash-tool shells running under process `pid`, from the process table. */
async function shellsUnder(pid) {
  let stdout;
  try { ({ stdout } = await exec('ps', ['-Ao', 'pid=,ppid=,args='], { timeout: 10000, maxBuffer: 16 * 1024 * 1024 })); }
  catch (e) { console.warn('[discover] ps for the shells under %s failed:', pid, e.message); return []; }
  const kids = new Map(), args = new Map();
  for (const l of stdout.split('\n')) {
    const m = l.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/);
    if (!m) continue;
    args.set(+m[1], m[3]);
    if (!kids.has(+m[2])) kids.set(+m[2], []);
    kids.get(+m[2]).push(+m[1]);
  }
  // The topmost wrapper in each branch: a shell's own children are its command's, not another shell.
  // A wrapper between claude and the shell (the Pi's CLAUDE_CODE_SHELL_PREFIX) is walked through.
  const out = [];
  const walk = (p) => {
    for (const c of kids.get(p) || []) {
      const cmd = shellCommand(args.get(c) || '');
      if (cmd) out.push(cmd); else walk(c);
    }
  };
  walk(pid);
  return out;
}

// The description each command was launched with, read once per command from the transcripts that
// could hold its Bash call — the conversation's own and its subagents' (an agent can leave a shell
// running after it ends).
const described = new Map();
function describe(cmd, transcriptPath) {
  const key = `${transcriptPath}\0${cmd}`;
  if (described.has(key)) return described.get(key);
  const needle = `"command":${JSON.stringify(cmd)}`;
  let label = null;
  const dir = path.join(transcriptPath.replace(/\.jsonl$/, ''), 'subagents');
  let files = [transcriptPath];
  try { files = files.concat(fs.readdirSync(dir).filter((f) => f.endsWith('.jsonl')).map((f) => path.join(dir, f))); }
  catch (e) { if (e.code !== 'ENOENT') console.warn('[discover] reading %s:', dir, e.message); }
  for (const f of files) {
    let text;
    try { text = fs.readFileSync(f, 'utf8'); }
    catch (e) { if (e.code !== 'ENOENT') console.warn('[discover] reading %s:', f, e.message); continue; }
    // Newest first: other records quote the command too (a task's notice, a progress line).
    for (let at = text.lastIndexOf(needle); at >= 0 && !label; at = text.lastIndexOf(needle, at - 1)) {
      const from = text.lastIndexOf('\n', at) + 1, to = text.indexOf('\n', at);
      try {
        const rec = JSON.parse(text.slice(from, to < 0 ? undefined : to));
        const use = (rec.message?.content || []).find((x) => x.type === 'tool_use' && x.input?.command === cmd);
        if (use?.input?.description) label = String(use.input.description);
      } catch { /* a partial line */ }
      if (at === 0) break;
    }
    if (label) break;
  }
  if (described.size > 500) described.clear();
  described.set(key, label);
  return label;
}

/**
 * The background work a live process `proc` (sessionOf) has going — what every host publishes as
 * `desk_sessions.bg_agents`, `bg_shell` and `bg_shells`. A shell is named by the description its Bash
 * call gave it, else by its command's first line. Nothing for a Codex session, whose rollout records none.
 */
export async function backgroundOf(proc, transcriptPath) {
  if (!proc || proc.shell == null) return { bg_agents: null, bg_shell: false, bg_shells: null };
  const agents = backgroundAgents(transcriptPath, proc.recordStartedAt);
  let shells = null;
  if (proc.shell === true && (proc.recordPid || proc.pid)) {
    const cmds = await shellsUnder(proc.recordPid || proc.pid);
    shells = cmds.map((c) => (transcriptPath && describe(c, transcriptPath))
      || c.trim().split('\n')[0].slice(0, 120));
    if (!shells.length) shells = null;
  }
  return { bg_agents: agents.length ? agents : null, bg_shell: proc.shell === true, bg_shells: shells };
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
    } catch (e) {
      // No directory: the timeout's own check is all there is.
      if (e.code !== 'ENOENT') console.warn('[discover] watching %s:', SESSIONS_DIR, e.message);
    }
  });
}

/**
 * Resolve once process `pid`'s session is no longer blocked on a dialog: the session, or null if it
 * has gone. Woken by Claude Code rewriting its record; the timeout only bounds a dialog nobody answers
 * and Claude Code never times out.
 */
export function awaitNotWaiting(pid, timeoutMs) {
  const clear = () => { const s = sessionOf(pid); return !s ? { s: null } : !s.waiting ? { s } : null; };
  const now = clear();
  if (now) return Promise.resolve(now.s);
  return new Promise((resolve, reject) => {
    let watcher = null;
    const finish = (fn) => { clearTimeout(timer); try { watcher?.close(); } catch {} fn(); };
    const timer = setTimeout(() => {
      const r = clear();
      finish(() => r ? resolve(r.s) : reject(new Error(`still on a ${sessionOf(pid)?.waiting || 'dialog'} after ${Math.round(timeoutMs / 1000)}s`)));
    }, timeoutMs);
    try {
      watcher = fs.watch(SESSIONS_DIR, () => { const r = clear(); if (r) finish(() => resolve(r.s)); });
    } catch (e) {
      // No directory: the timeout's own check is all there is.
      if (e.code !== 'ENOENT') console.warn('[discover] watching %s:', SESSIONS_DIR, e.message);
    }
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
  catch (e) { console.warn('[discover] ps for claude processes failed:', e.message); return []; }
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
