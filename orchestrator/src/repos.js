// Project publisher + desk presence.
//
// The hub server runs on Fly and can't see this filesystem, so the picker it renders has to be
// published from here: every REPO_POLL_MS, scan the configured roots and upsert what's there into
// `repos`.
//
// Every DIRECTORY counts, not just git checkouts. Filtering on `.git` hid some of the most active
// work here — several large working directories are deliberately not repos, and neither are
// various research dirs, but they're exactly what you'd want to open a session in. So git is
// decoration: branch + dirty when the directory happens to be a repo, absent when it isn't.
//
// Writes are change-gated for the same reason registry.js gates its own: the scan is local and
// free, the round-trip isn't, and this set changes a few times a week at most. `dirty` and
// `last_active` DO move during a work session, which is the point — the picker shows them.
//
// Presence is separate and unconditional (PRESENCE_MS into worker_presence as 'desk'), so the hub
// can grey out the launch buttons when the Mac is asleep rather than queue a command nobody runs.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { config } from './config.js';
import { upsertRepos, pruneRepos, heartbeatDesk } from './supabase.js';
import { loginExpiresAt } from './login.js';

const execFileP = promisify(execFile);

let timer = null;
let presenceTimer = null;
let running = false;
let lastWritten = null;

// Current branch without spawning git: .git/HEAD is `ref: refs/heads/<branch>` on a normal
// checkout. A detached HEAD (a bare sha) or a worktree link (`gitdir: …`) yields null, which the
// UI renders as no branch chip.
function branchOf(dir) {
  try {
    const head = fs.readFileSync(path.join(dir, '.git', 'HEAD'), 'utf8').trim();
    const m = head.match(/^ref:\s*refs\/heads\/(.+)$/);
    return m ? m[1] : null;
  } catch { return null; }
}

async function git(dir, args) {
  const { stdout } = await execFileP('git', ['-C', dir, ...args], { timeout: 10000 });
  return stdout;
}

// `last_active` is the HEAD commit date for a repo and the directory's own mtime otherwise — same
// question either way ("when did I last touch this"), and it keeps the picker's ordering sane for
// the non-repo projects that would otherwise sort last forever.
async function inspect(dir) {
  const name = path.basename(dir);
  const isRepo = fs.existsSync(path.join(dir, '.git'));
  let last_active = null;
  let dirty = false;
  if (isRepo) {
    try { last_active = (await git(dir, ['log', '-1', '--format=%cI'])).trim() || null; }
    catch { /* no commits yet, or not a readable repo */ }
    try { dirty = (await git(dir, ['status', '--porcelain'])).trim().length > 0; }
    catch { /* leave clean */ }
  }
  if (!last_active) {
    try { last_active = fs.statSync(dir).mtime.toISOString(); } catch { /* leave null */ }
  }
  return { path: dir, name, branch: isRepo ? branchOf(dir) : null, dirty, last_active,
    updated: new Date().toISOString() };
}

// Direct children of each root. Deep recursion would drag in vendored checkouts under
// node_modules, and everything worth opening a session in lives one level down.
function scanRoots() {
  const found = [];
  for (const root of config.REPO_ROOTS) {
    let entries = [];
    try { entries = fs.readdirSync(root, { withFileTypes: true }); }
    catch { continue; }  // root doesn't exist on this machine — fine
    for (const e of entries) {
      if (!e.isDirectory() || e.name.startsWith('.')) continue;
      found.push(path.join(root, e.name));
    }
  }
  return found.sort();
}

function needsWrite(rows) {
  if (!lastWritten || rows.length !== lastWritten.length) return true;
  for (let i = 0; i < rows.length; i++) {
    const a = rows[i], b = lastWritten[i];
    if (a.path !== b.path || a.branch !== b.branch || a.dirty !== b.dirty || a.last_active !== b.last_active) return true;
  }
  return false;
}

// Is `p` one of the directories we published? The command runner re-checks every launch target
// against this, so a bad or stale row in the queue can't turn into `cd` anywhere on the disk.
export function isKnownRepo(p) {
  return !!(lastWritten || []).find((r) => r.path === p);
}

async function tick() {
  if (running) return;
  running = true;
  try {
    const rows = [];
    for (const dir of scanRoots()) rows.push(await inspect(dir));
    if (needsWrite(rows)) {
      await upsertRepos(rows);
      await pruneRepos(rows.map((r) => r.path));
      console.log(`[repos] published ${rows.length} projects`);
    }
    lastWritten = rows;   // always refresh the launch allow-list, even when no write was needed
  } catch (e) {
    console.error('[repos] error:', e.message);
  } finally {
    running = false;
  }
}

// A process whose fetches have all gone bad stays alive and keeps failing — every call answering
// `fetch failed` while a fresh node on the same Mac connects at once — and the hub, reading the
// silence, refuses every follow-up with "the Mac is not responding". Nothing inside the process
// recovers from that state, so once presence has failed continuously for PRESENCE_FAIL_EXIT_MS it
// exits and launchd (KeepAlive) starts a clean one. A Mac that is genuinely offline just restarts
// every few minutes, which costs nothing.
let failingSince = null;

async function beat() {
  try {
    await heartbeatDesk('desk', { host: os.hostname(), login_expires_at: loginExpiresAt() });
    failingSince = null;
  } catch (e) {
    const cause = e.cause ? ` (${e.cause.code || e.cause.message || e.cause})` : '';
    console.error(`[repos] presence error ${new Date().toISOString()}: ${e.message}${cause}`);
    failingSince ??= Date.now();
    if (Date.now() - failingSince >= config.PRESENCE_FAIL_EXIT_MS) {
      console.error(`[repos] presence failing since ${new Date(failingSince).toISOString()} — exiting for launchd to restart`);
      process.exit(1);
    }
  }
}

export function startRepos() {
  tick();
  beat();
  timer = setInterval(tick, config.REPO_POLL_MS);
  presenceTimer = setInterval(beat, config.PRESENCE_MS);
  return () => {
    if (timer) clearInterval(timer);
    if (presenceTimer) clearInterval(presenceTimer);
  };
}
