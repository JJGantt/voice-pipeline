// Desk command consumer for the Pi — the half of the repo picker that works when the Mac is off.
//
// The Mac's orchestrator drains `desk_commands` and opens a Terminal tab. This does the same job on
// a headless machine, so "start a session in project X" stops depending on one laptop being awake.
// It differs from the Mac in three ways that are the whole point:
//
//   1. GitHub is the source of truth, not a local disk. The Mac launches into a checkout it already
//      has; the Pi clones on demand and pulls before every session, because another machine has very
//      likely worked on that repo since the Pi last looked.
//   2. There is no Terminal. Sessions run inside tmux, so they survive this process restarting and
//      can be attached over SSH if something needs watching.
//   3. ANTHROPIC_API_KEY is stripped before launching (see launch.sh). Claude Code prefers it over
//      CLAUDE_CODE_OAUTH_TOKEN, so leaving it set silently bills the API instead of the subscription.
//
// MACHINE PREREQUISITE: `gh auth setup-git` must have been run on this host. `gh repo clone` carries
// its own auth, so the first clone of a project succeeds either way — but a later `git pull` is plain
// git over HTTPS, and with no credential helper it tries to PROMPT for a username. In a daemon with
// no terminal that fails instantly ("could not read Username for 'https://github.com'"), so every
// session after the first would quietly run against a stale tree. The pull failure is non-fatal here
// by design (the warning reaches the prompt), which is exactly how this was found rather than missed.
//
// ROUTING. A command is for the Pi when either:
//   - payload.target === 'pi'                     (the hub decided — explicit)
//   - it has no target AND the Mac's presence is stale   (nobody else is going to take it)
// The staleness check is what makes this a fallback rather than a race: while the Mac is awake and
// beating, untargeted commands are left alone.
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
// Shared with the Mac orchestrator rather than copied: it only writes ~/.claude.json and has nothing
// platform-specific in it, and two copies of "which keys make a project entry complete" would drift.
import { ensureTrusted } from '../orchestrator/src/trust.js';
import { findTranscript, readTail, liveSessions, evict, pickEviction } from './sessions.js';

const exec = promisify(execFile);

const SUPABASE_URL = process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co';
const SUPABASE_KEY = process.env.SUPABASE_KEY;
const GITHUB_OWNER = process.env.GITHUB_OWNER || 'JJGantt';
const REPO_ROOT = process.env.REPO_ROOT || path.join(os.homedir(), 'repos');
const POLL_MS = Number(process.env.POLL_MS || 3000);
const PRESENCE_MS = Number(process.env.PRESENCE_MS || 30000);
// Matches the hub's DESK_STALE_S. Below this the Mac is considered awake and owns untargeted work.
const MAC_STALE_S = Number(process.env.MAC_STALE_S || 90);
// A queued command older than this is not worth running: the moment has passed, and opening a
// session for a request made an hour ago is worse than doing nothing.
const COMMAND_TTL_S = Number(process.env.COMMAND_TTL_S || 120);
const WORKER = 'pi-desk';
// A MEMORY bound: sessions cost ~334MB each whether or not they are doing anything.
const RESIDENT_MAX = Number(process.env.RESIDENT_MAX || 3);
// A CPU bound: four ARM cores, and two sessions both running a build contend badly.
const WORKING_MAX = Number(process.env.WORKING_MAX || 2);

if (!SUPABASE_KEY) {
  console.error('[pi-desk] SUPABASE_KEY is not set — refusing to start.');
  process.exit(2);
}

const rest = (p) => `${SUPABASE_URL}/rest/v1/${p}`;
const headers = (extra = {}) => ({
  apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}`,
  'Content-Type': 'application/json', ...extra,
});

async function sb(p, init) {
  // `...init` must come FIRST: spreading it last let a caller's own `headers` (the Prefer ones)
  // replace the computed object wholesale, stripping apikey/Authorization and 401ing every write.
  const res = await fetch(rest(p), { ...init, headers: headers(init?.headers) });
  const text = await res.text();
  if (!res.ok) throw new Error(`${p} ${res.status}: ${text.slice(0, 160)}`);
  // `Prefer: return=minimal` answers 201 with an EMPTY body, not 204, so keying the skip on the
  // status code alone left JSON.parse choking on ''. Parse only when there is something to parse.
  return text ? JSON.parse(text) : null;
}

// ---- presence -------------------------------------------------------------
// Unconditional and separate from the queue, so the hub can tell the difference between "the Pi is
// there and idle" and "nothing is listening at all".
async function heartbeat() {
  try {
    await sb('worker_presence?on_conflict=worker', {
      method: 'POST',
      headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
      body: JSON.stringify([{ worker: WORKER, last_seen: new Date().toISOString(), info: { host: os.hostname() } }]),
    });
  } catch (e) { console.error('[pi-desk] heartbeat:', e.message); }
}

// ---- live sessions --------------------------------------------------------
// Publish what is running and reap what is not, then run the queue. tmux is the source of truth
// rather than a list this process keeps, because this process restarts and tmux does not.
async function reconcileSessions() {
  const live = await liveSessions();
  const residents = [];

  for (const { name, alive } of live) {
    if (!alive) {
      // A husk: tmux session whose claude has exited. These are what accumulate, one per launch.
      try { await exec('tmux', ['kill-session', '-t', name], { timeout: 10000 }); } catch {}
      console.log(`[pi-desk] reaped finished session ${name}`);
      continue;
    }
    let row = null;
    try {
      const rows = await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}&select=*`);
      row = rows?.[0] || null;
    } catch {}
    const tail = row?.transcript_path ? readTail(row.transcript_path) : { working: false, lastReply: null, bytes: 0 };
    residents.push({ name, working: tail.working, bytes: tail.bytes, row });

    try {
      await sb('desk_sessions?on_conflict=id', {
        method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
        // Upsert, not patch-if-known: a session tmux is running that the table never saw must still
        // appear, or "tmux is the source of truth" is not true.
        body: JSON.stringify([{
          id: name, worker: WORKER, status: 'resident',
          repo: row?.repo || name.replace(/^hub-/, '').replace(/-[^-]*$/, ''),
          claude_session_id: row?.claude_session_id || null,
          transcript_path: row?.transcript_path || null,
          last_reply: tail.lastReply ? tail.lastReply.slice(0, 2000) : row?.last_reply || null,
          last_seen: new Date().toISOString(),
        }]),
      });
    } catch (e) { console.error('[pi-desk] publish:', e.message); }
  }

  // Rows we publish that tmux no longer has: mark evicted rather than delete, so the transcript stays
  // reachable and the session can be resumed. Only genuinely forgotten sessions are deleted, and only
  // when Jared says so.
  try {
    const rows = await sb(`desk_sessions?worker=eq.${WORKER}&status=eq.resident&select=id`);
    const liveNames = new Set(residents.map((r) => r.name));
    for (const r of rows || []) {
      if (liveNames.has(r.id)) continue;
      await sb(`desk_sessions?id=eq.${encodeURIComponent(r.id)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({ status: 'evicted', ended: new Date().toISOString() }),
      });
      console.log(`[pi-desk] session left residency: ${r.id}`);
    }
  } catch (e) { console.error('[pi-desk] sweep:', e.message); }

  await runQueue(residents);
}

// ---- the queue ------------------------------------------------------------
// Deliberately not desk_commands: those expire, because a command queued while a machine slept should
// not fire hours later. A queued SESSION is the opposite — it is waiting on purpose, and must never
// expire. It waits as long as it takes for a slot.
async function runQueue(residents) {
  let queued;
  try { queued = await sb(`desk_queue?worker=eq.${WORKER}&claimed=is.null&select=*&order=created.asc&limit=1`); }
  catch { return; }
  const next = queued?.[0];
  if (!next) return;

  const working = residents.filter((r) => r.working).length;
  if (working >= WORKING_MAX) return;          // CPU bound: let the busy ones finish first

  if (residents.length >= RESIDENT_MAX) {      // memory bound: someone has to go
    const victim = pickEviction(residents);
    if (!victim) return;                       // everything resident is mid-turn — wait, don't kill
    console.log(`[pi-desk] evicting ${victim.name} to make room for ${next.repo}`);
    await evict(victim.name);
    await sb(`desk_sessions?id=eq.${encodeURIComponent(victim.name)}`, {
      method: 'PATCH', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify({ status: 'evicted', ended: new Date().toISOString() }),
    }).catch(() => {});
  }

  // Claim before launching so two ticks cannot both take the same queued item.
  try {
    const claimed = await sb(`desk_queue?id=eq.${encodeURIComponent(next.id)}&claimed=is.null`, {
      method: 'PATCH', headers: { Prefer: 'return=representation' },
      body: JSON.stringify({ claimed: new Date().toISOString() }),
    });
    if (!Array.isArray(claimed) || !claimed.length) return;
  } catch { return; }

  try {
    const detail = await startSession({ name: next.repo, prompt: next.prompt });
    console.log(`[pi-desk] from queue -> ${detail}`);
  } catch (e) {
    console.error('[pi-desk] queued launch failed:', e.message);
  }
  await sb(`desk_queue?id=eq.${encodeURIComponent(next.id)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } }).catch(() => {});
}

async function macIsAwake() {
  try {
    const rows = await sb('worker_presence?worker=eq.desk&select=last_seen');
    const seen = rows?.[0]?.last_seen;
    if (!seen) return false;
    return (Date.now() - new Date(seen).getTime()) / 1000 <= MAC_STALE_S;
  } catch { return true; }   // can't tell → assume the Mac has it, rather than double-launching
}

// ---- the repo -------------------------------------------------------------
// The Mac publishes filesystem paths that mean nothing here, so the NAME is the identifier: it is
// both the directory under ~/repos and the GitHub repo. Validated because it reaches a shell.
function safeName(name) {
  const n = String(name || '').trim();
  return /^[A-Za-z0-9._-]+$/.test(n) && n !== '.' && n !== '..' ? n : null;
}

/** Clone if absent, pull if present. Returns { dir, warning }. */
async function ensureRepo(name) {
  const dir = path.join(REPO_ROOT, name);
  fs.mkdirSync(REPO_ROOT, { recursive: true });

  if (!fs.existsSync(path.join(dir, '.git'))) {
    console.log(`[pi-desk] cloning ${GITHUB_OWNER}/${name}`);
    await exec('gh', ['repo', 'clone', `${GITHUB_OWNER}/${name}`, dir], { timeout: 180000 });
    return { dir, warning: null };
  }

  // Pull before every session. Another machine has very likely pushed since this checkout was last
  // touched, and the failure mode of skipping it — writing code against a stale tree, then pushing —
  // is a conflict at best and silently reverted work at worst.
  try {
    await exec('git', ['-C', dir, 'fetch', '--all', '--prune'], { timeout: 120000 });
    await exec('git', ['-C', dir, 'pull', '--ff-only'], { timeout: 120000 });
    return { dir, warning: null };
  } catch (e) {
    // Diverged or dirty. Launch anyway, but say so in the prompt — a session that knows its checkout
    // is behind can deal with it; one that doesn't will quietly build on the wrong base.
    const msg = String(e.stderr || e.message || '').split('\n')[0].slice(0, 200);
    console.error(`[pi-desk] pull failed in ${name}: ${msg}`);
    return { dir, warning: `NOTE: this checkout could not be fast-forwarded (${msg}). Check git status and reconcile with origin before making changes.` };
  }
}

// ---- launching ------------------------------------------------------------
function launch(dir, name, prompt, resumeId = null) {
  const session = `hub-${name}-${Date.now().toString(36)}`;
  const here = path.dirname(new URL(import.meta.url).pathname);
  const launcher = path.join(here, 'launch.sh');

  // The prompt goes to a file, not an argv string: tmux runs its command through a shell, and
  // dictated speech is full of quotes and apostrophes. launch.sh reads and deletes it.
  let promptFile = '';
  if (prompt) {
    promptFile = path.join(os.tmpdir(), `pi-desk-${session}.txt`);
    fs.writeFileSync(promptFile, prompt, { mode: 0o600 });
  }

  const args = ['new-session', '-d', '-s', session, '-c', dir, launcher, dir, name, promptFile, resumeId || ''];
  const child = spawn('tmux', args, { detached: true, stdio: 'ignore' });
  child.unref();
  return session;
}

// ---- the queue ------------------------------------------------------------
async function claim(id) {
  // Compare-and-swap on status: an empty result means the Mac (or another tick) got there first.
  const rows = await sb(`desk_commands?id=eq.${encodeURIComponent(id)}&status=eq.pending`, {
    method: 'PATCH',
    headers: { Prefer: 'return=representation' },
    body: JSON.stringify({ status: 'running', updated: new Date().toISOString() }),
  });
  return Array.isArray(rows) && rows.length > 0;
}

const finish = (id, status, detail) =>
  sb(`desk_commands?id=eq.${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { Prefer: 'return=minimal' },
    body: JSON.stringify({ status, detail: String(detail || '').slice(0, 500), updated: new Date().toISOString() }),
  }).catch((e) => console.error('[pi-desk] finish:', e.message));

/**
 * Launch a session and record everything needed to bring it back later.
 *
 * The Claude session id is the point: without it, closing a session destroys it. It cannot be asked
 * for (it is generated inside the process), so it is recovered by finding the transcript Claude Code
 * writes at startup — the filename IS the id.
 */
async function startSession({ name, prompt, resumeId = null }) {
  // Capacity check happens HERE rather than at launch, so a request that cannot run right now becomes
  // a queued row instead of being dropped or forced through.
  const live = await liveSessions();
  const residents = live.filter((l) => l.alive);
  if (residents.length >= RESIDENT_MAX) {
    const id = randomUUID();
    await sb('desk_queue', {
      method: 'POST', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify([{ id, worker: WORKER, repo: name, prompt: String(payload.prompt || '').trim() || null }]),
    });
    return `queued behind ${residents.length} resident session(s)`;
  }
  return startSession({ name, prompt: payload.prompt });
}

async function run(cmd) {
  // Closing a session is a command like any other, so it can be driven from anywhere the queue
  // reaches — a voice command, the hub, an MCP tool — not only from a shell on this machine.
  if (cmd.kind === 'end_session') {
    const target = String((cmd.payload || {}).session || '').trim();
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    await exec('tmux', ['kill-session', '-t', target], { timeout: 10000 });
    await sb(`desk_sessions?id=eq.${encodeURIComponent(target)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } }).catch(() => {});
    return `ended ${target}`;
  }
  // Bring an evicted session back with its full context. `claude --resume <id>` replays the
  // transcript, which is why the id had to be recorded when the session first started.
  if (cmd.kind === 'resume_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const rows = await sb(`desk_sessions?id=eq.${encodeURIComponent(id)}&select=*`);
    const row = rows?.[0];
    if (!row) throw new Error(`no such session: ${id}`);
    if (!row.claude_session_id) throw new Error(`${id} has no recorded claude session id — cannot resume`);
    return startSession({ name: row.repo, prompt: (cmd.payload || {}).prompt || '', resumeId: row.claude_session_id });
  }

  if (cmd.kind !== 'open_session') throw new Error(`unknown kind ${cmd.kind}`);
  const payload = cmd.payload || {};
  const name = safeName(payload.name);
  if (!name) throw new Error(`unusable project name: ${JSON.stringify(payload.name)}`);

  const { dir, warning } = await ensureRepo(name);
  // Without this the session opens onto "Do you trust the files in this folder?" and waits for a
  // click nobody is there to make — the launch looks like it worked and is actually stuck. Matters
  // more here than on the Mac: a freshly cloned repo has never been seen before by definition.
  const trust = ensureTrusted(dir);
  const prompt = [warning, String(payload.prompt || '').trim()].filter(Boolean).join('\n\n');
  const session = launch(dir, name, prompt);
  // Publish immediately rather than waiting for the next reconcile tick — the indicator should light
  // up when the work starts, not up to a poll later.
  try {
    await sb('desk_sessions?on_conflict=id', {
      method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
      body: JSON.stringify([{ id: session, worker: WORKER, repo: name,
                              prompt: String(payload.prompt || '').trim().slice(0, 500) || null }]),
    });
  } catch (e) { console.error('[pi-desk] publish:', e.message); }
  return `tmux ${session} in ${dir}${prompt ? ' with a prompt' : ''}${warning ? ' (stale checkout)' : ''} (trust: ${trust})`;
}

let draining = false;
async function drain() {
  if (draining) return;
  draining = true;
  try {
    const rows = await sb('desk_commands?status=eq.pending&select=*&order=created.asc');
    if (!rows?.length) return;
    const macAwake = await macIsAwake();

    for (const cmd of rows) {
      const target = (cmd.payload || {}).target;
      const mine = target === 'pi' || (!target && !macAwake);
      if (!mine) continue;

      const ageS = (Date.now() - new Date(cmd.created).getTime()) / 1000;
      if (ageS > COMMAND_TTL_S) {
        if (await claim(cmd.id)) {
          await finish(cmd.id, 'expired', `queued ${Math.round(ageS)}s ago`);
          console.log(`[pi-desk] expired ${cmd.id} (${Math.round(ageS)}s old)`);
        }
        continue;
      }
      if (!(await claim(cmd.id))) continue;

      try {
        const detail = await run(cmd);
        await finish(cmd.id, 'done', detail);
        console.log(`[pi-desk] ${cmd.id} -> ${detail}`);
      } catch (e) {
        console.error('[pi-desk] failed:', e.message);
        await finish(cmd.id, 'error', e.message);
      }
    }
  } catch (e) {
    console.error('[pi-desk] drain:', e.message);
  } finally {
    draining = false;
  }
}

console.log(`[pi-desk] watching desk_commands (repos in ${REPO_ROOT}, owner ${GITHUB_OWNER})`);
heartbeat();
setInterval(heartbeat, PRESENCE_MS);
drain();
setInterval(drain, POLL_MS);
reconcileSessions();
setInterval(reconcileSessions, PRESENCE_MS);
