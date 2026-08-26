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
// Shared with the Mac orchestrator rather than copied: it only writes ~/.claude.json and has nothing
// platform-specific in it, and two copies of "which keys make a project entry complete" would drift.
import { ensureTrusted } from '../orchestrator/src/trust.js';

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
// Publish what is actually running, and delete what is not. Nothing closed sessions before this:
// every launch left a tmux session holding a Claude process, forever, on a machine with 8GB of RAM.
//
// tmux is the source of truth rather than a list this process keeps, because this process restarts
// and tmux does not — sessions started before the last restart are still real and must still appear.
// A session counts as ALIVE only if a claude process is running inside it; tmux keeps the shell alive
// after claude exits, so "the session exists" is not the same question as "work is happening".
async function reconcileSessions() {
  let live = [];
  try {
    const { stdout } = await exec('tmux', ['list-sessions', '-F', '#{session_name}'], { timeout: 10000 });
    const names = stdout.split('\n').map((n) => n.trim()).filter((n) => n.startsWith('hub-'));
    for (const name of names) {
      let alive = false;
      try {
        // Panes in this session whose command is claude. Empty output = the shell outlived claude.
        const { stdout: panes } = await exec('tmux',
          ['list-panes', '-t', name, '-F', '#{pane_current_command}'], { timeout: 10000 });
        alive = panes.split('\n').some((c) => c.trim() === 'claude');
      } catch { alive = false; }
      if (alive) live.push(name);
      else {
        // A husk: tmux session with no claude in it. Kill it rather than leave it to accumulate.
        try { await exec('tmux', ['kill-session', '-t', name], { timeout: 10000 }); } catch {}
        console.log(`[pi-desk] reaped finished session ${name}`);
      }
    }
  } catch { /* no server running = no sessions, which is a valid state, not an error */ }

  try {
    const rows = await sb(`desk_sessions?worker=eq.${WORKER}&select=id`);
    const known = new Set((rows || []).map((r) => r.id));
    for (const id of live) {
      // UPSERT, not patch-if-known. Patching only what the table already had meant a session tmux was
      // running but the table had never seen — one started before this code existed, or after the row
      // was lost — stayed invisible forever, which makes "tmux is the source of truth" a false claim.
      await sb('desk_sessions?on_conflict=id', {
        method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
        // Repo comes out of the session name for a row being adopted rather than launched; the name
        // is hub-<repo>-<suffix> and the suffix is base36 with no dashes, so the last one splits it.
        body: JSON.stringify([{ id, worker: WORKER, repo: id.replace(/^hub-/, '').replace(/-[^-]*$/, ''),
                                last_seen: new Date().toISOString() }]),
      });
      known.delete(id);
    }
    // Whatever is still in `known` is published but no longer running — drop it. Deleting rather than
    // tombstoning keeps "select from desk_sessions" honest for anything using it as an indicator.
    for (const gone of known) {
      await sb(`desk_sessions?id=eq.${encodeURIComponent(gone)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } });
      console.log(`[pi-desk] session ended: ${gone}`);
    }
  } catch (e) { console.error('[pi-desk] reconcile:', e.message); }
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
function launch(dir, name, prompt) {
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

  const args = ['new-session', '-d', '-s', session, '-c', dir, launcher, dir, name, promptFile];
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
