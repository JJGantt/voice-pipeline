// Codex CLI sessions: which conversation a `codex` process is in, and what its rollout says.
//
// The Codex counterpart of discover.js and transcripts.js. A Codex session is a row like any other
// (`desk_sessions.agent = 'codex'`); what differs is where its facts are written:
//
//   - THE CONVERSATION is a rollout file, $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread id>.jsonl.
//     Codex has no per-process record like Claude Code's ~/.claude/sessions/<pid>.json, but the native
//     binary holds its rollout OPEN for the whole session (and a lock under thread-writer-locks/), so the
//     conversation of a process is READ from its open files — never inferred from which rollout is newest.
//     The process launched (`codex`) is a node wrapper that spawns the native binary as its child, so the
//     search covers the pid and its descendants.
//   - THE TURN STATE is in the rollout: `event_msg` `task_started` opens a turn, `task_complete` (carrying
//     `last_agent_message`, the final answer) or `turn_aborted` closes it. The user's words are the
//     `item_completed` UserMessage item (0.16x) or `user_message` event (older rollouts). The developer and
//     AGENTS.md context Codex injects are `response_item` messages and are never read as the user's words.
//
// The thread id is stored in `desk_sessions.claude_session_id`: it is the conversation's identity, the
// same role the column plays for Claude, and `codex resume <id>` keeps it.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { resolvePrompt, fromHim, flatten } from './transcripts.js';

export const CODEX_HOME = process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
const ROLLOUT = /(^|\/)rollout-[^/]*\.jsonl$/;

/** Whether a transcript path is a Codex rollout (as against a Claude Code transcript). */
export const isRollout = (p) => ROLLOUT.test(String(p || ''));

let codexBin;   // resolved once: undefined = not looked yet, null = not installed
/** The `codex` binary on PATH, or null. */
export function codexBinary() {
  if (codexBin !== undefined) return codexBin;
  try { codexBin = execFileSync('which', ['codex'], { encoding: 'utf8', timeout: 5000 }).trim() || null; }
  catch { codexBin = null; }
  return codexBin;
}

/**
 * Whether this machine can run a Codex session: the CLI installed and signed in. Published in the desk
 * heartbeat (`info.codex`), which is what data-layer routes a Codex dispatch on.
 */
export function codexReady() {
  return !!codexBinary() && fs.existsSync(path.join(CODEX_HOME, 'auth.json'));
}

// ---- which rollout a process has open ------------------------------------------------------------

/** `pid` and every process under it. */
function family(pid) {
  const out = [Number(pid)];
  if (process.platform === 'linux') {
    for (let i = 0; i < out.length && out.length < 64; i++) {
      let kids = '';
      try { kids = fs.readFileSync(`/proc/${out[i]}/task/${out[i]}/children`, 'utf8'); } catch { continue; }
      for (const k of kids.trim().split(/\s+/).filter(Boolean)) out.push(Number(k));
    }
    return out;
  }
  let table = '';
  try { table = execFileSync('ps', ['-Ao', 'pid=,ppid='], { encoding: 'utf8', timeout: 10000 }); } catch { return out; }
  const kids = new Map();
  for (const l of table.split('\n')) {
    const [p, pp] = l.trim().split(/\s+/).map(Number);
    if (!p) continue;
    if (!kids.has(pp)) kids.set(pp, []);
    kids.get(pp).push(p);
  }
  for (let i = 0; i < out.length && out.length < 64; i++) out.push(...(kids.get(out[i]) || []));
  return out;
}

/** The rollout one of `pids` holds open, or null. */
function rolloutHeldBy(pids) {
  if (process.platform === 'linux') {
    for (const p of pids) {
      let fds;
      try { fds = fs.readdirSync(`/proc/${p}/fd`); } catch { continue; }
      for (const fd of fds) {
        let target;
        try { target = fs.readlinkSync(`/proc/${p}/fd/${fd}`); } catch { continue; }
        if (isRollout(target)) return target;
      }
    }
    return null;
  }
  let out = '';
  // lsof exits 1 when one of the pids has gone; what it printed for the rest is still the answer.
  try { out = execFileSync('lsof', ['-a', '-p', pids.join(','), '-Fn'], { encoding: 'utf8', timeout: 10000 }); }
  catch (e) { out = String(e.stdout || ''); }
  const hit = out.split('\n').find((l) => l.startsWith('n') && isRollout(l.slice(1)));
  return hit ? hit.slice(1) : null;
}

/** The thread id and working directory a rollout records on its first line. */
function rolloutMeta(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    // The first line carries Codex's whole base prompt, so it is tens of KB; read until its newline.
    let buf = Buffer.alloc(0);
    for (let pos = 0; buf.indexOf(0x0a) < 0 && pos < 4 * 1024 * 1024;) {
      const chunk = Buffer.allocUnsafe(64 * 1024);
      const n = fs.readSync(fd, chunk, 0, chunk.length, pos);
      if (!n) break;
      buf = Buffer.concat([buf, chunk.subarray(0, n)]);
      pos += n;
    }
    const nl = buf.indexOf(0x0a);
    const d = JSON.parse(buf.subarray(0, nl < 0 ? buf.length : nl).toString('utf8'));
    const p = d.type === 'session_meta' ? d.payload || {} : {};
    return { id: p.id || p.session_id || null, cwd: p.cwd || null };
  } catch { return { id: null, cwd: null }; }
  finally { if (fd !== undefined) fs.closeSync(fd); }
}

/**
 * The conversation Codex process `pid` is in — the same shape as discover.js `sessionOf`, so a host
 * treats both alike. Null until the process has opened its rollout (Codex writes it when the first turn
 * starts, so a session launched without a prompt has none until it is spoken to).
 */
export function codexSessionOf(pid) {
  if (!pid) return null;
  const file = rolloutHeldBy(family(pid));
  if (!file) return null;
  const meta = rolloutMeta(file);
  const id = meta.id || (path.basename(file).match(/([0-9a-f]{8}-[0-9a-f-]{27,})\.jsonl$/) || [])[1] || null;
  if (!id) return null;
  const tail = readCodexTail(file);
  const cwd = meta.cwd || os.homedir();
  return {
    claudeId: id, cwd, transcriptPath: file,
    repo: cwd === os.homedir() ? 'root' : path.basename(cwd),
    busy: tail.working, statusAt: tail.turnAt,
    // Codex never stops on a question here: every launch runs with approvals off.
    waiting: null,
    agent: 'codex',
  };
}

/** `codexSessionOf`, waiting up to `timeoutMs` for the rollout to be opened. */
export async function awaitCodexSession(pid, timeoutMs = 15000) {
  for (const end = Date.now() + timeoutMs; ; await new Promise((r) => setTimeout(r, 500))) {
    const s = codexSessionOf(pid);
    if (s || Date.now() >= end) return s;
  }
}

// ---- reading a rollout ---------------------------------------------------------------------------

/** The user's words in a record, or null when the record is not the user speaking. */
export function codexPrompt(d) {
  const p = d && d.type === 'event_msg' ? d.payload || {} : null;
  if (!p) return null;
  let text = null;
  if (p.type === 'item_completed' && p.item && p.item.type === 'UserMessage') {
    text = (Array.isArray(p.item.content) ? p.item.content : [])
      .map((c) => (c && typeof c.text === 'string' ? c.text : '')).join('\n');
  } else if (p.type === 'user_message' && typeof p.message === 'string') {
    text = p.message;
  }
  if (!text || !text.trim() || !fromHim(text)) return null;
  return resolvePrompt(text).prompt;
}

/** What the agent said between tool calls, or null. Its final answer is `task_complete`'s, not this. */
export function codexCommentary(d) {
  const p = d && d.type === 'event_msg' ? d.payload || {} : null;
  if (!p) return null;
  if (p.type === 'item_completed' && p.item && p.item.type === 'AgentMessage') {
    if (p.item.phase === 'final_answer') return null;
    const t = (Array.isArray(p.item.content) ? p.item.content : [])
      .map((c) => (c && typeof c.text === 'string' ? c.text : '')).join('\n').trim();
    return t || null;
  }
  if (p.type === 'agent_message' && typeof p.message === 'string' && p.phase !== 'final_answer') return p.message.trim() || null;
  return null;
}

/** A tool call as one line, the shape sessionWatch's `toolLine` gives Claude's. */
export function codexToolLine(d) {
  const p = d && d.type === 'response_item' ? d.payload || {} : null;
  if (!p || !/_call$/.test(String(p.type || ''))) return null;
  const name = p.name || (p.type === 'web_search_call' ? 'web_search' : p.type.replace(/_call$/, ''));
  let args = p.arguments ?? p.input ?? p.action ?? '';
  if (typeof args !== 'string') { try { args = JSON.stringify(args); } catch { args = ''; } }
  args = args.replace(/\s+/g, ' ').trim();
  if (args.length > 240) args = `${args.slice(0, 239)}…`;
  return `${name}${args ? ` ${args}` : ''}`.trim().slice(0, 260);
}

/** The turn a record opens (`task_started`) as its key, or null. */
export const codexTurnStart = (d) => (d && d.type === 'event_msg' && d.payload && d.payload.type === 'task_started'
  ? String(d.payload.turn_id || d.timestamp || '') : null);
/** Whether a record ends a turn. */
export const codexTurnEnd = (d) => !!(d && d.type === 'event_msg' && d.payload
  && (d.payload.type === 'task_complete' || d.payload.type === 'turn_aborted'));

/**
 * A rollout's tail, in the shape transcripts.js `readTail` returns for a Claude transcript, which hands
 * a rollout path straight here. `lastReply` is the final answer of the last COMPLETED turn
 * (`task_complete.last_agent_message`), and `lastPrompt` the words that turn answered.
 */
export function readCodexTail(file, { maxBytes = 512 * 1024 } = {}) {
  const empty = { working: false, lastReply: null, lastPrompt: null, replyAt: null, turnAt: null, title: null,
                  customTitle: null, bytes: 0, lastActivity: 0 };
  let st;
  try { st = fs.statSync(file); } catch { return empty; }
  if (!st.isFile()) return empty;
  const from = Math.max(0, st.size - maxBytes);
  let lines;
  try {
    const fd = fs.openSync(file, 'r');
    try {
      const buf = Buffer.allocUnsafe(st.size - from);
      fs.readSync(fd, buf, 0, buf.length, from);
      lines = buf.toString('utf8').split('\n');
    } finally { fs.closeSync(fd); }
  } catch { return empty; }
  if (from > 0) lines.shift();

  let working = false, newest = 0, promptAt = null, replyAt = null;
  let turnPrompt = null, finalReply = null, finalPrompt = null, lastSaid = null;
  for (const line of lines) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    const ms = typeof d.timestamp === 'string' ? Date.parse(d.timestamp) : 0;
    if (ms > newest) newest = ms;
    if (codexTurnStart(d) !== null) { working = true; promptAt = d.timestamp || promptAt; turnPrompt = null; continue; }
    const said = codexPrompt(d);
    if (said) { if (!turnPrompt || flatten(turnPrompt) !== flatten(said)) turnPrompt = turnPrompt ? `${turnPrompt}\n\n${said}` : said; continue; }
    const words = codexCommentary(d);
    if (words) { lastSaid = words; continue; }
    if (codexTurnEnd(d)) {
      working = false;
      const reply = d.payload.type === 'task_complete' && typeof d.payload.last_agent_message === 'string'
        ? d.payload.last_agent_message.trim() : '';
      if (reply) { finalReply = reply; replyAt = d.timestamp || null; finalPrompt = turnPrompt; }
    }
  }
  if (finalReply === null && from > 0) return readCodexTail(file, { maxBytes: maxBytes * 4 });
  const turnAt = [promptAt, replyAt].filter(Boolean).sort().pop() || null;
  return { working, lastReply: finalReply ?? lastSaid, lastPrompt: finalReply !== null ? finalPrompt : null,
           replyAt, turnAt, title: null, customTitle: null, bytes: st.size, lastActivity: newest || st.mtimeMs };
}

// ---- the Mac: Codex sessions in Terminal tabs ----------------------------------------------------

/**
 * Every native `codex` process sitting in a terminal on this machine, with the conversation it is in —
 * discover.js `liveClaudeSessions` for Codex. The native binary, not the node wrapper that launched it:
 * the binary holds the rollout, and its `comm` is `codex`. Oldest first.
 */
export function liveCodexSessions() {
  let stdout;
  // `comm` LAST, for the reason given in discover.js.
  try { stdout = execFileSync('ps', ['-Ao', 'pid=,tty=,lstart=,comm='], { encoding: 'utf8', timeout: 10000 }); }
  catch { return []; }
  return stdout.split('\n').map((l) => l.trim()).filter(Boolean)
    .map((l) => {
      const m = l.match(/^(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.+)$/);
      if (!m) return null;
      const [, pid, tty, lstart, comm] = m;
      if (tty === '??' || tty === '?' || comm.split('/').pop() !== 'codex') return null;
      const startedAt = new Date(lstart).getTime();
      if (!Number.isFinite(startedAt)) return null;
      const s = codexSessionOf(Number(pid));
      return s ? { pid: Number(pid), tty: `/dev/${tty}`, startedAt, ...s } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.startedAt - b.startedAt);
}

/** The Codex session a launch just started on `tty`: the first there that began after `since`. */
export async function awaitCodexOn(tty, since, { attempts = 90, delayMs = 1000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const hit = liveCodexSessions().find((s) => s.tty === tty && s.startedAt >= since);
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}
