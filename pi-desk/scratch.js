// Claude Code's per-conversation scratch directories, cleared once the conversation stops running.
//
// Every session gets /tmp/claude-<uid>/<project>/<conversation id>/ for temporary work, and Claude Code
// never removes it. On the Pi /tmp is tmpfs, so whatever a session leaves there is RAM, and an idle
// tmpfs page goes to zram swap — which memoryPressure() counts. One session's re-transcription test
// (a 630MB Whisper model plus a venv) held swap over MAX_SWAP_PCT for two days after it ended, and
// every session sent to the Pi in that time queued and never launched.
//
// A conversation is running when a live process's own record (~/.claude/sessions/<pid>.json, the
// same record discover.js reads) names it. The record's existence is not enough: a process killed
// hard leaves its file behind, so the pid is checked against /proc, and against the record's
// `procStart` so a recycled pid does not keep a dead conversation's directory.
//
// A parked session loses its scratch directory and gets a fresh one on resume. Scratch is temporary
// by contract; holding it for a resume that may never come is the failure this exists to prevent.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const SESSIONS_DIR = path.join(os.homedir(), '.claude', 'sessions');
const SCRATCH_ROOT = path.join(os.tmpdir(), `claude-${process.getuid()}`);
const CONVERSATION = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Whether `pid` is running and is the process the record was written by. */
function running(pid, procStart) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    // Fields after the command name (which can contain spaces) start at field 3; starttime is 22.
    const starttime = stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19];
    return procStart == null || String(procStart) === starttime;
  } catch { return false; }
}

/** Conversation ids some live process is in; null when that cannot be read (then nothing is cleared). */
function liveConversations() {
  let files;
  try { files = fs.readdirSync(SESSIONS_DIR); } catch { return null; }
  const live = new Set();
  for (const f of files) {
    if (!f.endsWith('.json')) continue;
    let d;
    try { d = JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, f), 'utf8')); } catch { continue; }
    if (typeof d.sessionId === 'string' && running(d.pid, d.procStart)) live.add(d.sessionId);
  }
  return live;
}

export function sweepScratch() {
  const live = liveConversations();
  if (!live) return;
  let projects;
  try { projects = fs.readdirSync(SCRATCH_ROOT, { withFileTypes: true }); } catch { return; }
  for (const p of projects) {
    if (!p.isDirectory()) continue;
    const dir = path.join(SCRATCH_ROOT, p.name);
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const e of entries) {
      if (!e.isDirectory() || !CONVERSATION.test(e.name) || live.has(e.name)) continue;
      try {
        fs.rmSync(path.join(dir, e.name), { recursive: true, force: true });
        console.log(`[pi-desk] cleared scratch of ended conversation ${e.name.slice(0, 8)} (${p.name})`);
      } catch (err) { console.error('[pi-desk] scratch:', err.message); }
    }
  }
}
