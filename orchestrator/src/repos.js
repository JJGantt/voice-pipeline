// Repo publisher + desk presence.
//
// The hub server runs on Fly and can't see this filesystem, so the picker it renders has to be
// published from here: every REPO_POLL_MS, scan the configured roots for git checkouts and upsert
// what's there (path, branch, dirty, HEAD date) into `repos`.
//
// Writes are change-gated for the same reason registry.js gates its own: the scan is local and
// free, the round-trip isn't, and this set changes a few times a week at most. `dirty` and
// `last_commit` DO move during a work session, which is the point — the picker shows them.
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

async function inspect(dir) {
  const name = path.basename(dir);
  let last_commit = null;
  let dirty = false;
  try { last_commit = (await git(dir, ['log', '-1', '--format=%cI'])).trim() || null; }
  catch { /* no commits yet, or not a readable repo */ }
  try { dirty = (await git(dir, ['status', '--porcelain'])).trim().length > 0; }
  catch { /* leave clean */ }
  return { path: dir, name, branch: branchOf(dir), dirty, last_commit, updated: new Date().toISOString() };
}

// Direct children of each root that are git checkouts. Deep recursion would drag in vendored
// checkouts under node_modules, and every repo worth opening lives one level down.
function scanRoots() {
  const found = [];
  for (const root of config.REPO_ROOTS) {
    let entries = [];
    try { entries = fs.readdirSync(root, { withFileTypes: true }); }
    catch { continue; }  // root doesn't exist on this machine — fine
    for (const e of entries) {
      if (!e.isDirectory() || e.name.startsWith('.')) continue;
      const dir = path.join(root, e.name);
      if (fs.existsSync(path.join(dir, '.git'))) found.push(dir);
    }
  }
  return found.sort();
}

function needsWrite(rows) {
  if (!lastWritten || rows.length !== lastWritten.length) return true;
  for (let i = 0; i < rows.length; i++) {
    const a = rows[i], b = lastWritten[i];
    if (a.path !== b.path || a.branch !== b.branch || a.dirty !== b.dirty || a.last_commit !== b.last_commit) return true;
  }
  return false;
}

// Is `p` one of the repos we published? The command runner re-checks every launch target against
// this, so a bad or stale row in the queue can't turn into `cd` anywhere on the disk.
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
      console.log(`[repos] published ${rows.length} repos`);
    }
    lastWritten = rows;   // always refresh the launch allow-list, even when no write was needed
  } catch (e) {
    console.error('[repos] error:', e.message);
  } finally {
    running = false;
  }
}

async function beat() {
  try { await heartbeatDesk('desk', { host: os.hostname() }); }
  catch (e) { console.error('[repos] presence error:', e.message); }
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
