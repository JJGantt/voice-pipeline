#!/usr/bin/env node
// Watchlist check (health/WATCHLIST.md, "Session identity"): every Claude session running on THIS
// machine is on the wall as itself. Prints one line per process and row, and exits 1 on any mismatch.
//
//   node ~/repos/voice-pipeline/orchestrator/check-sessions.mjs     (Mac)
//   node ~/voice-pipeline/orchestrator/check-sessions.mjs           (Pi)
//
// Ground truth is read here directly, not through discover.js, so a bug there cannot hide from it:
//   - the claude processes actually running — on the Mac every one with a terminal, on the Pi every
//     `hub-*` tmux pane (launch.sh `exec`s claude, so the pane's pid is claude's);
//   - Claude Code's record for each, ~/.claude/sessions/<pid>.json. It is internal to Claude Code, not
//     an interface, so every field the orchestrators read must be present and well-formed — this is
//     what catches a `claude` update that renames or drops one, which would otherwise make sessions
//     vanish from the wall or order wrongly without anything erroring;
//   - this machine's running rows in `desk_sessions`.
//
// PASS: every process has a well-formed record and exactly one running row carrying its pid and its
// current conversation, and no running row names a process that is not running.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// A session this young may not have been published yet: the reconcile tick is 30s, and a launch
// takes a while to bring claude up.
const STARTING_S = 90;

const here = path.dirname(new URL(import.meta.url).pathname);
// The service key: from the environment, else the files the services themselves load it from — the
// voice worker's .env (the Pi's pi-desk borrows it from there), then data-layer's, which is where it
// lives on the Mac (the orchestrator gets its own copy from its launchd plist).
function key() {
  if (process.env.SUPABASE_KEY) return process.env.SUPABASE_KEY;
  const candidates = [
    path.join(here, '..', 'worker', '.env'),
    path.join(os.homedir(), 'repos', 'data-layer', '.env'),
    path.join(os.homedir(), 'data-layer', '.env'),
  ];
  for (const f of candidates) {
    try {
      const line = fs.readFileSync(f, 'utf8').split('\n').find((l) => l.startsWith('SUPABASE_KEY='));
      if (line) return line.slice('SUPABASE_KEY='.length).trim().replace(/^['"]|['"]$/g, '');
    } catch { /* not on this machine */ }
  }
  return '';
}
const SUPABASE_URL = process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co';
const KEY = key();
const MAC = process.platform === 'darwin';
const WORKER = MAC ? 'desk' : 'pi-desk';
const SESSIONS_DIR = path.join(os.homedir(), '.claude', 'sessions');

// stderr discarded: no tmux server at all is a normal state on the Pi, and tmux says so there.
const sh = (cmd, args) => {
  try { return execFileSync(cmd, args, { encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'] }); }
  catch { return ''; }
};

/** [{ pid, where, startedAt }] for every claude that is somebody's session here. */
function processes() {
  if (MAC) {
    // comm LAST: macOS truncates it to 16 characters when another column follows.
    return sh('ps', ['-Ao', 'pid=,tty=,lstart=,comm=']).split('\n').map((l) => l.trim())
      .map((l) => l.match(/^(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.+)$/))
      .filter((m) => m && m[2] !== '??' && m[4].split('/').pop() === 'claude')
      .map((m) => ({ pid: Number(m[1]), where: m[2], startedAt: new Date(m[3]).getTime() }));
  }
  const out = [];
  for (const l of sh('tmux', ['list-sessions', '-F', '#{session_name}\t#{session_created}']).split('\n')) {
    const [name, created] = l.trim().split('\t');
    if (!name || !name.startsWith('hub-')) continue;
    const pane = sh('tmux', ['list-panes', '-t', name, '-F', '#{pane_current_command}\t#{pane_pid}'])
      .split('\n').map((p) => p.trim().split('\t')).find(([c]) => c === 'claude');
    if (pane) out.push({ pid: Number(pane[1]), where: name, startedAt: Number(created) * 1000 });
  }
  return out;
}

/** The record's problems, or [] when every field the orchestrators read is there. */
function recordProblems(r) {
  if (!r) return ['no ~/.claude/sessions record'];
  const bad = [];
  if (typeof r.sessionId !== 'string' || !r.sessionId) bad.push('sessionId missing');
  if (typeof r.cwd !== 'string' || !r.cwd) bad.push('cwd missing');
  // The point of this check is a field the orchestrators read going MISSING, not the arrival of a
  // new state — Claude Code adds those (2.1.276 brought 'shell' and 'waiting') and a hardcoded pair
  // turns every one of them into a false FAIL. deskSessions.js never branches on the value, only on
  // statusUpdatedAt, so any non-empty string is fine and only absence is a finding.
  if (typeof r.status !== 'string' || !r.status) bad.push(`status is ${JSON.stringify(r.status)}, expected a state string`);
  if (!Number.isFinite(r.statusUpdatedAt)) bad.push('statusUpdatedAt missing');
  return bad;
}

const readRecord = (pid) => { try { return JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, `${pid}.json`), 'utf8')); } catch { return null; } };

if (!KEY) { console.log('FAIL no SUPABASE_KEY (env, worker/.env or data-layer/.env)'); process.exit(1); }
const res = await fetch(`${SUPABASE_URL}/rest/v1/desk_sessions?worker=eq.${WORKER}&status=eq.resident&select=id,pid,claude_session_id,started`,
  { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } });
if (!res.ok) { console.log(`FAIL desk_sessions ${res.status}: ${await res.text()}`); process.exit(1); }
const rows = await res.json();

let fails = 0;
const say = (ok, msg) => { if (!ok) fails++; console.log(`${ok ? 'OK  ' : 'FAIL'} ${msg}`); };
const now = Date.now();
const procs = processes();
const livePids = new Set(procs.map((p) => p.pid));

for (const p of procs) {
  const young = (now - p.startedAt) / 1000 < STARTING_S;
  const rec = readRecord(p.pid);
  const bad = recordProblems(rec);
  if (bad.length) { say(false, `pid ${p.pid} (${p.where}): record — ${bad.join('; ')}`); continue; }
  const mine = rows.filter((r) => r.pid === p.pid);
  if (!mine.length) { if (young) console.log(`--   pid ${p.pid} (${p.where}): starting, no row yet`); else say(false, `pid ${p.pid} (${p.where}): no running row`); continue; }
  if (mine.length > 1) { say(false, `pid ${p.pid} (${p.where}): ${mine.length} running rows (${mine.map((r) => r.id).join(', ')})`); continue; }
  const row = mine[0];
  // A /clear moves the record before the next tick moves the row; a mismatch that lasts is a failure.
  say(row.claude_session_id === rec.sessionId,
      `pid ${p.pid} (${p.where}) -> ${row.id}: conversation ${String(row.claude_session_id).slice(0, 8)}`
      + (row.claude_session_id === rec.sessionId ? '' : `, but the process is in ${rec.sessionId.slice(0, 8)}`));
}
for (const r of rows) {
  if (r.pid && livePids.has(r.pid)) continue;
  const young = (now - new Date(r.started).getTime()) / 1000 < STARTING_S;
  if (!r.pid && young) { console.log(`--   ${r.id}: starting, no process bound yet`); continue; }
  say(false, `${r.id}: running row, but ${r.pid ? `pid ${r.pid} is not a running session` : 'no process bound'}`);
}
if (!procs.length && !rows.length) console.log('OK   nothing running');
console.log(fails ? `\n${fails} mismatch(es) on ${WORKER}` : `\nall sessions on ${WORKER} match`);
process.exit(fails ? 1 : 0);
