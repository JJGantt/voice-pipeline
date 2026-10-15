// Each Pi session in a memory cgroup of its own.
//
// pi-desk.service runs with Delegate=yes, so the unit's cgroup is ours to divide:
//
//   pi-desk.service/
//     supervisor/     this daemon, run.sh and the tmux server
//     s/<session>/    one Claude session: claude, its tool servers, every command it runs
//
// Three things come from that and nothing else provides them:
//
//   - CLEANUP. Closing a session kills everything in its cgroup, so a job it started cannot outlive it.
//     Before this, a parked session's Python jobs and a `flyctl agent` stayed resident for days.
//   - A BUDGET per session. memory.high is the point at which a session counts as doing heavy work,
//     which this Pi is not for (index.js moves it to the Mac). memory.max is the hard ceiling: past
//     it the kernel kills inside that session only, and tool.sh puts every command a session runs
//     ahead of claude itself, so what dies is the command, never the conversation.
//   - AN EVENT. The kernel rewrites memory.events when a cgroup crosses memory.high, and inotify sees
//     it, so heavy work is noticed the moment it happens without anything polling memory.
import fs from 'node:fs';
import path from 'node:path';

// The unit's own cgroup, whichever child this process has already been moved into.
const self = fs.readFileSync('/proc/self/cgroup', 'utf8').match(/^0::(.*)$/m)?.[1] || '';
export const UNIT = path.join('/sys/fs/cgroup', self.replace(/\/(supervisor|s\/[^/]+)$/, ''));
const SUPERVISOR = path.join(UNIT, 'supervisor');
const SESSIONS = path.join(UNIT, 's');

const MB = 1024 * 1024;
// A session at rest is ~490MB with its tool servers (claude ~340, the MCP servers ~150). The budget
// leaves room for ordinary commands — git, grep, a test run — and not for a Python job holding
// hundreds of megabytes, which is the work that belongs on the Mac.
export const SESSION_BUDGET_MB = Number(process.env.PI_DESK_SESSION_BUDGET_MB || 800);
const SESSION_MAX_MB = Number(process.env.PI_DESK_SESSION_MAX_MB || 1200);
const SUPERVISOR_MAX_MB = Number(process.env.PI_DESK_SUPERVISOR_MAX_MB || 400);

let enabled = false;
export const cgroupsEnabled = () => enabled;
export const sessionCgroup = (name) => path.join(SESSIONS, name);

const write = (file, value) => fs.writeFileSync(file, String(value));
const procsOf = (dir) => {
  try { return fs.readFileSync(path.join(dir, 'cgroup.procs'), 'utf8').split('\n').filter(Boolean).map(Number); }
  catch { return []; }
};
const move = (pid, dir) => { try { write(path.join(dir, 'cgroup.procs'), pid); return true; } catch { return false; } };

/** pid → parent pid for every process, read once. */
function parents() {
  const out = new Map();
  for (const d of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(d)) continue;
    try {
      // The command name is in parentheses and may contain spaces; ppid is the 2nd field after it.
      const stat = fs.readFileSync(`/proc/${d}/stat`, 'utf8');
      out.set(Number(d), Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]));
    } catch { /* exited while listing */ }
  }
  return out;
}
const descendsFrom = (pid, root, ppid) => {
  for (let p = pid, n = 0; p > 1 && n < 64; p = ppid.get(p), n++) if (p === root) return true;
  return false;
};
const comm = (pid) => { try { return fs.readFileSync(`/proc/${pid}/comm`, 'utf8').trim(); } catch { return ''; } };

/**
 * Divide the unit's cgroup. Runs once at startup and is safe to repeat: a restart finds sessions and
 * leftovers in the unit's own cgroup (KillMode=process keeps the tmux server and its sessions alive
 * across restarts) and sorts every process into place.
 *
 * `live` is liveSessions(): each running session's tmux name and claude pid. A process that is neither
 * this daemon's, the tmux server's, nor descended from a live session is a leftover from a session
 * that is gone, and is killed — that is the cleanup this module exists for.
 */
export async function setupCgroups(live) {
  try {
    fs.mkdirSync(SUPERVISOR, { recursive: true });
    fs.mkdirSync(SESSIONS, { recursive: true });
  } catch (e) {
    console.error(`[cgroups] unavailable (${e.message}) — sessions run without budgets`);
    return;
  }
  const ppid = parents();
  const sessions = live.filter((l) => l.alive && l.pid);
  const leftovers = [];
  const strays = [UNIT, SUPERVISOR, ...fs.readdirSync(SESSIONS).map((n) => sessionCgroup(n))];
  for (const dir of strays) {
    for (const pid of procsOf(dir)) {
      const owner = sessions.find((s) => descendsFrom(pid, s.pid, ppid));
      if (owner) {
        const cg = sessionCgroup(owner.name);
        if (dir !== cg) { fs.mkdirSync(cg, { recursive: true }); move(pid, cg); }
      } else if (pid === process.pid || pid === process.ppid || comm(pid) === 'tmux: server') {
        if (dir !== SUPERVISOR) move(pid, SUPERVISOR);
      } else {
        leftovers.push(pid);
      }
    }
  }
  if (leftovers.length) {
    const reap = path.join(UNIT, 'reap');
    fs.mkdirSync(reap, { recursive: true });
    for (const pid of leftovers) {
      console.log(`[cgroups] killing leftover ${pid} (${comm(pid)}) — its session is gone`);
      move(pid, reap);
    }
    await killCgroup(reap);
  }
  // Session cgroups whose session is gone (and, after the kill above, hold nothing).
  for (const n of fs.readdirSync(SESSIONS)) {
    if (!sessions.some((s) => s.name === n)) await removeSessionCgroup(n);
  }
  try {
    write(path.join(UNIT, 'cgroup.subtree_control'), '+memory');
    write(path.join(SESSIONS, 'cgroup.subtree_control'), '+memory');
    write(path.join(SUPERVISOR, 'memory.max'), SUPERVISOR_MAX_MB * MB);
    enabled = true;
  } catch (e) {
    console.error(`[cgroups] could not enable the memory controller (${e.message}) — sessions run without budgets`);
    return;
  }
  for (const s of sessions) limitSession(s.name);
  console.log(`[cgroups] ${sessions.length} session(s) budgeted at ${SESSION_BUDGET_MB}MB (ceiling ${SESSION_MAX_MB}MB)`);
}

/** Create a session's cgroup with its budget, before launch.sh moves into it. Returns its path. */
export function limitSession(name) {
  const cg = sessionCgroup(name);
  try { fs.mkdirSync(cg, { recursive: true }); } catch { return ''; }   // no cgroups: launch.sh skips the move
  if (enabled) {
    try {
      write(path.join(cg, 'memory.max'), SESSION_MAX_MB * MB);
      write(path.join(cg, 'memory.high'), SESSION_BUDGET_MB * MB);
    } catch (e) { console.error(`[cgroups] limits for ${name}: ${e.message}`); }
  }
  return cg;
}

/**
 * Stop treating a session's budget as a reason to act: the work stays here because the Mac is not
 * there to take it, and memory.high would otherwise throttle it for as long as it runs. memory.max
 * still holds, so the rest of the Pi is protected either way.
 */
export function liftBudget(name) {
  try { write(path.join(sessionCgroup(name), 'memory.high'), 'max'); } catch {}
}

/** Kill every process in a cgroup and wait until it is empty. */
async function killCgroup(dir) {
  try { write(path.join(dir, 'cgroup.kill'), 1); } catch { return; }
  // cgroup.kill is asynchronous; `populated 0` in cgroup.events is the kernel saying it is done.
  for (let i = 0; i < 100; i++) {
    try { if (/^populated 0$/m.test(fs.readFileSync(path.join(dir, 'cgroup.events'), 'utf8'))) break; }
    catch { break; }
    await new Promise((r) => setTimeout(r, 20));
  }
  try { fs.rmdirSync(dir); } catch {}
}

/**
 * Clean up after a session that has been closed: anything it left running is killed and its cgroup
 * removed. Called once claude itself has exited, so this only ever reaps what it left behind.
 */
export async function removeSessionCgroup(name) {
  const cg = sessionCgroup(name);
  if (!fs.existsSync(cg)) return;
  const left = procsOf(cg);
  if (left.length) console.log(`[cgroups] ${name}: killing ${left.length} leftover process(es): ${left.map(comm).join(', ')}`);
  stopWatching(name);
  await killCgroup(cg);
}

/**
 * Megabytes a session holds: `anonMb`, what its cgroup cannot simply drop, and `currentMb`, its total.
 *
 * The total is the larger of the cgroup's count and the resident size of its processes. A process
 * moved into a cgroup leaves the memory it already had charged where it was, so a session that was
 * running before setupCgroups looks almost empty to its cgroup until it allocates afresh — and
 * admission would then reserve room for it to grow that it has already used.
 */
export function sessionUsage(name) {
  try {
    const cg = sessionCgroup(name);
    const stat = fs.readFileSync(path.join(cg, 'memory.stat'), 'utf8');
    const anon = Number(stat.match(/^anon (\d+)$/m)?.[1] || 0);
    const current = Number(fs.readFileSync(path.join(cg, 'memory.current'), 'utf8'));
    let rssKb = 0;
    for (const pid of procsOf(cg)) {
      try { rssKb += Number(fs.readFileSync(`/proc/${pid}/status`, 'utf8').match(/^VmRSS:\s+(\d+)/m)?.[1] || 0); } catch {}
    }
    return { anonMb: Math.round(anon / MB), currentMb: Math.max(Math.round(current / MB), Math.round(rssKb / 1024)) };
  } catch { return null; }
}

// ---- heavy-work events ----------------------------------------------------------------------
const watchers = new Map();

/**
 * Call `onHeavy(name, usage)` when a session goes over its budget with memory it cannot give back.
 *
 * memory.events counts every crossing of memory.high, and the kernel crosses it reclaiming page cache
 * too — a session reading large files touches the budget without holding anything. So a crossing
 * counts only when anon memory alone is over the budget: that is a process actually holding it.
 */
export function watchSession(name, onHeavy) {
  if (!enabled || watchers.has(name)) return;
  const file = path.join(sessionCgroup(name), 'memory.events');
  let seen = highCount(file);
  try {
    const w = fs.watch(file, () => {
      const n = highCount(file);
      if (n <= seen) return;
      seen = n;
      const u = sessionUsage(name);
      if (u && u.anonMb >= SESSION_BUDGET_MB) onHeavy(name, u);
    });
    w.on('error', () => stopWatching(name));
    watchers.set(name, w);
  } catch (e) { console.error(`[cgroups] cannot watch ${name}: ${e.message}`); }
}

export function stopWatching(name) {
  const w = watchers.get(name);
  if (w) { try { w.close(); } catch {} watchers.delete(name); }
}

function highCount(file) {
  try { return Number(fs.readFileSync(file, 'utf8').match(/^high (\d+)$/m)?.[1] || 0); } catch { return 0; }
}
