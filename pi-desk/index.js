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
// Shared with the Mac rather than reimplemented: both halves of a transfer must agree byte for
// byte on what a mirrored transcript is.
import { mirrorUp, mirrorDown } from '../orchestrator/src/mirror.js';
// The same self-healing subscription the Mac's orchestrator uses. Shared rather than reimplemented:
// its header documents a hard-won lesson — supabase-js does not reliably recover a dead channel, and
// callers that fall back to a tight poll burn ~20k reads a day without noticing.
import { watchTable } from '../orchestrator/src/realtime.js';

const exec = promisify(execFile);

const SUPABASE_URL = process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co';
const SUPABASE_KEY = process.env.SUPABASE_KEY;
const GITHUB_OWNER = process.env.GITHUB_OWNER || 'JJGantt';
const REPO_ROOT = process.env.REPO_ROOT || path.join(os.homedir(), 'repos');
// BACKSTOP ONLY. Commands arrive over realtime; this exists for the window where the subscription is
// down. Polling every 3s was ~28,800 reads a day that almost always found nothing — the exact waste
// realtime.js was written to eliminate, reintroduced by taking a shortcut here.
const POLL_MS = Number(process.env.POLL_MS || 300000);
// Used only while realtime is unhealthy, so a command still lands within seconds when it matters.
const POLL_DEGRADED_MS = Number(process.env.POLL_DEGRADED_MS || 5000);
const PRESENCE_MS = Number(process.env.PRESENCE_MS || 30000);
// Matches the hub's DESK_STALE_S. Below this the Mac is considered awake and owns untargeted work.
const MAC_STALE_S = Number(process.env.MAC_STALE_S || 90);
// A queued command older than this is not worth running: the moment has passed, and opening a
// session for a request made an hour ago is worse than doing nothing.
const COMMAND_TTL_S = Number(process.env.COMMAND_TTL_S || 120);
// How long a session gets to commit, push and update docs before it is closed regardless. Generous
// on purpose: this is real work, and cutting it short is the thing wrapping up exists to prevent.
const WORKER = 'pi-desk';
// A MEMORY bound: sessions cost ~334MB each whether or not they are doing anything.
//
// Two, not three, since 2026-09-02: pi-browser.service became a permanent tenant of this Pi that
// day (~450MB of Chromium on :99, capped at 1.3G). Three sessions plus the wall display plus the
// automation browser does not fit in 4GB, and the way it does not fit is the kernel killing the
// wall — see the OOM ordering note on IDLE_MAX_S below.
const RESIDENT_MAX = Number(process.env.RESIDENT_MAX || 2);

// A TIME bound, and the one the other limits do not provide.
//
// RESIDENT_MAX, WORKING_MAX, MIN_FREE_MB and pickEviction all run in ONE place: runQueue, when
// something new wants a slot. Nothing queued means nothing re-examines what is already resident, so
// a session that finished its work simply stays. On 2026-09-04 two Amazon-cart sessions from Sep 3
// (one of which had already answered, one of which returned __ESCALATE__) sat idle for two days
// holding ~516MB, and the kernel spent that afternoon killing the wall display's renderer instead —
// 13 OOM kills in the final hour. Nothing was over any limit; there was no limit to be over.
//
// Eviction is not destructive: an evicted session keeps its transcript and `resume_session` brings
// it back with full context, so the cost of reaping early is tokens on resume, not lost work. That
// asymmetry is why this can be aggressive where pickEviction is a last resort.
const IDLE_MAX_S = Number(process.env.PI_DESK_IDLE_MAX_S || 3600);
// Capacity is not only a count. This Pi also drives the wall display, and Chromium is the process
// that loses an out-of-memory fight — the kernel OOM-killed its renderer twice on 2026-08-26 while
// sessions sat inside the residency cap, leaving the screen on 'Aw, Snap' with nothing technically
// over its limit. A session costs ~350-420MB, so refusing to launch below this floor keeps enough
// headroom that starting one cannot take the display down. Below it, work queues rather than fails.
const MIN_FREE_MB = Number(process.env.PI_DESK_MIN_FREE_MB || 900);

/** MemAvailable in MB — the kernel's own estimate of what is actually reclaimable, not `free`. */
function memAvailableMb() {
  try {
    const m = fs.readFileSync('/proc/meminfo', 'utf8').match(/^MemAvailable:\s+(\d+) kB/m);
    return m ? Math.round(Number(m[1]) / 1024) : Infinity;   // unreadable → never block on it
  } catch { return Infinity; }
}
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

  // Transcript ids already spoken for. Adoption picks the newest transcript created after a launch,
  // and two launches close together in one directory could otherwise both land on the same file —
  // which is exactly how two root sessions ended up recording one claude id and burning two slots on
  // a single conversation.
  const claimed = new Set();
  try {
    const rows = await sb('desk_sessions?claude_session_id=not.is.null&select=claude_session_id');
    for (const r of rows || []) claimed.add(r.claude_session_id);
  } catch {}

  for (const { name, alive, created } of live) {
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
    // Self-healing: a session with no recorded transcript — the initial lookup missed it, or the
    // session predates this code — adopts one here rather than staying permanently unresumable.
    let tpath = row?.transcript_path || null;
    let cid = row?.claude_session_id || null;
    if (!tpath) {
      const repoName = row?.repo || name.replace(/^hub-/, '').replace(/-[^-]*$/, '');
      // A root session lives in the home directory, not under REPO_ROOT — joining REPO_ROOT with
      // 'root' pointed at a directory that does not exist, so root sessions never adopted anything.
      const cwd = repoName === ROOT ? os.homedir() : path.join(REPO_ROOT, repoName);
      // Bounded by when THIS session actually started (tmux's own clock, not the row's), never
      // "newest in the directory". Unbounded, a session whose transcript had not appeared yet adopted
      // its predecessor's — and resuming it would restore a different conversation entirely.
      const since = created ? created - 5000 : null;
      const found = since ? await findTranscript(cwd, since, { attempts: 1, delayMs: 0, taken: claimed }) : null;
      if (found) { tpath = found.path; cid = found.id; claimed.add(found.id); console.log(`[pi-desk] adopted transcript ${found.id} for ${name}`); }
    }
    const tail = tpath ? readTail(tpath) : { working: false, lastReply: null, bytes: 0, lastActivity: 0 };
    // Mirror at the end of a turn — the only moment the transcript is a complete thought — so this
    // session can be picked up on the Mac later, including while this machine is unreachable.
    let mirrored = null;
    if (!tail.working && cid && tpath && tail.bytes > (row?.mirror_offset || 0)) {
      try { mirrored = await mirrorUp(cid, tpath, row?.mirror_offset || 0); }
      catch (e) { console.error('[pi-desk] mirror:', e.message); }
    }
    // lastActivity is what eviction sorts on. A session with no transcript reports 0 and therefore
    // sorts first — correct, since a session nothing can be read from is the least costly to lose.
    // `created` is tmux's own start clock, carried so the idle reaper can tell a session that has
    // gone quiet from one that has not written its first transcript line yet.
    residents.push({ name, working: tail.working, bytes: tail.bytes, lastActivity: tail.lastActivity, created, row });

    try {
      await sb('desk_sessions?on_conflict=id', {
        method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
        // Upsert, not patch-if-known: a session tmux is running that the table never saw must still
        // appear, or "tmux is the source of truth" is not true.
        body: JSON.stringify([{
          id: name, worker: WORKER, status: 'resident',
          repo: row?.repo || name.replace(/^hub-/, '').replace(/-[^-]*$/, ''),
          claude_session_id: cid,
          transcript_path: tpath,
          // Carried through explicitly: this is a merge-duplicates upsert, so any column it sends
          // overwrites. Omitting the label would blank it on the very next reconcile tick.
          //
          // A name Jared typed himself (`/rename`, which writes a `custom-title` record) outranks the
          // label the router wrote — the same rule the Mac applies. Without this the rename reached
          // the session and nothing ever read it back, so the pane kept showing the old label.
          label: tail.customTitle ? tail.customTitle.slice(0, 60) : (row?.label || null),
          // Separately, so a tab can show the repo unless Jared named the session himself.
          custom_title: tail.customTitle ? tail.customTitle.slice(0, 60) : (row?.custom_title || null),
          last_reply: tail.lastReply ? tail.lastReply.slice(0, 4000) : row?.last_reply || null,
          // last_reply is the FINAL message of a turn; `working` is what says whether a newer one is
          // still being written. Without it a reader cannot tell an answer from a stale one.
          working: tail.working,
          reply_at: tail.replyAt || row?.reply_at || null,
          mirror_offset: mirrored ?? (row?.mirror_offset || 0),
          ...(mirrored ? { mirror_at: new Date().toISOString() } : {}),
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

  await runQueue(await reapIdleSessions(residents));
}

/**
 * Close resident sessions that have gone quiet, and return the ones that remain.
 *
 * Unlike pickEviction this is not about making room — it runs every tick whether or not anything is
 * queued, which is precisely the case nothing else covered. It takes ALL sessions past the cutoff,
 * not just the oldest, because the failure it exists to prevent is several of them accumulating.
 *
 * Two things are deliberately never reaped:
 *   · a `working` session — a turn is in flight, and cutting it can leave a repo half-edited.
 *   · a session younger than the cutoff by tmux's clock — `lastActivity` is 0 until the transcript's
 *     first line lands, so a freshly launched session looks maximally idle and would be killed on
 *     the tick after it started.
 */
async function reapIdleSessions(residents) {
  if (!(IDLE_MAX_S > 0)) return residents;             // 0 or unset disables reaping entirely
  const cutoff = Date.now() - IDLE_MAX_S * 1000;
  const kept = [];
  for (const r of residents) {
    const quiet = (r.lastActivity || 0) < cutoff;
    const old = (r.created || 0) < cutoff;             // never judge a session younger than the cutoff
    if (r.working || !quiet || !old) { kept.push(r); continue; }
    const idleMin = Math.round((Date.now() - (r.lastActivity || r.created || 0)) / 60000);
    console.log(`[pi-desk] reaping ${r.name}: idle ${idleMin}m (limit ${Math.round(IDLE_MAX_S / 60)}m)`);
    await evict(r.name);
    await sb(`desk_sessions?id=eq.${encodeURIComponent(r.name)}`, {
      method: 'PATCH', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify({ status: 'evicted', ended: new Date().toISOString() }),
    }).catch(() => {});
  }
  return kept;
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

  // Under the memory floor with a free slot means the machine is short for some other reason — the
  // display, a build, anything. Evicting a session would not help (the slot is not what is missing)
  // and launching into it is how the wall display gets OOM-killed. Wait; the queue never expires.
  const freeMb = memAvailableMb();
  if (freeMb < MIN_FREE_MB && residents.length < RESIDENT_MAX) {
    console.log(`[pi-desk] holding queue: ${freeMb}MB free, need ${MIN_FREE_MB}MB`);
    return;
  }

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
    const detail = await startSession({ name: next.repo, prompt: next.prompt, report: next.report === true, label: next.label || null });
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
  if (!n) return ROOT;   // no project named — the home directory is the default, not an error
  return /^[A-Za-z0-9._-]+$/.test(n) && n !== '.' && n !== '..' ? n : null;
}

/** The home directory, for work that is not about one project. */
const ROOT = 'root';

/**
 * Clone if absent, pull if present. Returns { dir, warning }.
 *
 * `root` is not a repo: it is the home directory, which is where a request that names no project
 * lands. That is the useful fallback — a command the router could not pin to a repo still gets sent
 * somewhere it can act, rather than being refused.
 */
async function ensureRepo(name) {
  if (!name || name === ROOT) return { dir: os.homedir(), warning: null };
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

// Appended only when a dispatch ASKED for a write-up (`report` on the command payload, set by the
// voice tool when Jared says so). Reports are opt-in because most dispatched work just needs
// doing, and a page about a one-line change is noise.
//
// It rides in the prompt rather than living in CLAUDE.md so it can be per-request at all: a file
// both machines share applies to every session, including the ones Jared opens by hand at the
// keyboard, which have no business filing reports at him.
const REPORT_DIRECTIVE =
  '[When you have finished this work, call the publish_report tool with an HTML write-up of what '
  + 'you did — that is how Jared reads the outcome on his phone. Then answer normally here as '
  + 'well: the report is an extra artifact, never a replacement for replying.]';

// Type into a session that is already running.
//
// Text and Enter as SEPARATE send-keys calls. Combined, tmux types the text but the Enter does not
// submit it — the prompt sits in the composer unsent while everything looks like it worked.
async function typeInto(target, prompt) {
  await exec('tmux', ['send-keys', '-t', target, prompt], { timeout: 15000 });
  await new Promise((r) => setTimeout(r, 600));
  await exec('tmux', ['send-keys', '-t', target, 'Enter'], { timeout: 10000 });
  return `sent ${prompt.length} chars to ${target}`;
}

// ---- launching ------------------------------------------------------------
function launch(dir, name, prompt, resumeId = null) {
  const session = `hub-${name}-${Date.now().toString(36)}`;
  // The Remote Control name is an IDENTITY in the Claude app and must be unique per SESSION. The
  // tmux name already carries a timestamp suffix for exactly that reason, so the RC name reuses it
  // rather than passing the bare repo — which made every session on a repo register as that repo.
  const rcName = session.slice(4);
  const here = path.dirname(new URL(import.meta.url).pathname);
  const launcher = path.join(here, 'launch.sh');

  // The prompt goes to a file, not an argv string: tmux runs its command through a shell, and
  // dictated speech is full of quotes and apostrophes. launch.sh reads and deletes it.
  let promptFile = '';
  if (prompt) {
    promptFile = path.join(os.tmpdir(), `pi-desk-${session}.txt`);
    fs.writeFileSync(promptFile, prompt, { mode: 0o600 });
  }

  const args = ['new-session', '-d', '-s', session, '-c', dir, launcher, dir, rcName, promptFile, resumeId || '', session];
  // The first of these spawns creates the tmux SERVER, and the server keeps this environment for
  // its whole life — every later session in it inherits the lot, including ones a person opens by
  // hand. run.sh loaded worker/.env into this process, so without scrubbing here the server carries
  // ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN, and any `claude` started under it that does not
  // go through launch.sh silently bills the API or loses its account linkage. launch.sh strips them
  // for its own sessions; this covers everything else.
  const env = { ...process.env };
  for (const k of ['ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN']) delete env[k];
  const child = spawn('tmux', args, { detached: true, stdio: 'ignore', env });
  child.unref();
  return session;
}

// ---- the queue ------------------------------------------------------------
async function claim(id) {
  // Compare-and-swap on status: an empty result means the Mac (or another tick) got there first.
  const rows = await sb(`desk_commands?id=eq.${encodeURIComponent(id)}&status=eq.pending`, {
    method: 'PATCH',
    headers: { Prefer: 'return=representation' },
    // claimed_by is what makes an abandoned claim recoverable: a command left 'running' by a machine
    // that died can only be swept by the machine that took it, and this is how it knows which.
    body: JSON.stringify({ status: 'running', claimed_by: WORKER, updated: new Date().toISOString() }),
  });
  return Array.isArray(rows) && rows.length > 0;
}

/**
 * Keep a command that could not run.
 *
 * The prompt inside is usually minutes of dictated speech that cannot be repeated, so it is never
 * simply logged and lost. Not a retry queue — the command is finished, and Jared decides later
 * whether he still wants it.
 */
async function deadLetter(cmd, reason) {
  try {
    await sb('desk_dead_letters', {
      method: 'POST', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify([{ id: randomUUID(), command_id: cmd.id, kind: cmd.kind,
                              payload: cmd.payload || {}, machine: 'pi',
                              reason: String(reason || '').slice(0, 500), source: WORKER }]),
    });
  } catch (e) { console.error('[pi-desk] dead letter:', e.message); }
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
async function startSession({ name, prompt, resumeId = null, commandId = null, report = false, label = null,
                              knownClaudeId = null, transcriptPath = null, mirrorOffset = null, origin = null }) {
  const { dir, warning } = await ensureRepo(name);
  // Without this the session opens onto "Do you trust the files in this folder?" and waits for a
  // click nobody is there to make. Matters most on a fresh clone, which by definition is unseen.
  const trust = ensureTrusted(dir);
  const spoken = String(prompt || '').trim();
  // The directive rides along ONLY when this dispatch asked for a write-up. Most work just needs
  // doing, and a report on a one-line change is noise — so it is opt-in, set by the voice tool
  // when Jared actually asks. A bare session with no prompt has nothing to report on either way.
  const full = [warning, spoken, (report && spoken) ? REPORT_DIRECTIVE : null].filter(Boolean).join('\n\n');
  const startedAt = Date.now() - 1000;   // slack: the transcript may be stamped just before
  const session = launch(dir, name, full, resumeId);

  // Published immediately so the indicator lights up when work starts, not a poll later.
  await sb('desk_sessions?on_conflict=id', {
    method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
    // command_id is what lets a follow-up find this session: the dispatch that created it is the
    // only durable handle the caller had at the time, since the session did not exist yet.
    body: JSON.stringify([{ id: session, worker: WORKER, repo: name, status: 'resident', command_id: commandId,
                            label: label || null,
                            ...(origin ? { origin } : {}),
                            ...(knownClaudeId ? { claude_session_id: knownClaudeId, transcript_path: transcriptPath,
                                                  mirror_offset: mirrorOffset ?? 0 } : {}),
                            prompt: String(prompt || '').trim().slice(0, 500) || null }]),
  }).catch((e) => console.error('[pi-desk] publish:', e.message));

  // A handed-over session already knows its id — it is the transcript that came across — so there is
  // nothing to search for, and searching would be wrong: the file was written a moment ago by the
  // mirror, not by Claude starting up.
  if (knownClaudeId) {
    return `tmux ${session} in ${dir} (resumed from the Mac)${warning ? ' (stale checkout)' : ''} (trust: ${trust})`;
  }

  // Backfilled once Claude has written its transcript. Async on purpose — the caller should not wait
  // on filesystem polling to learn that the launch happened.
  findTranscript(dir, startedAt).then((t) => {
    if (!t) return console.error(`[pi-desk] no transcript for ${session} — it will not be resumable`);
    console.log(`[pi-desk] ${session} -> claude session ${t.id}`);
    return sb(`desk_sessions?id=eq.${encodeURIComponent(session)}`, {
      method: 'PATCH', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify({ claude_session_id: t.id, transcript_path: t.path }),
    });
  }).catch((e) => console.error('[pi-desk] transcript:', e.message));

  return `tmux ${session} in ${dir}${full ? ' with a prompt' : ''}${resumeId ? ' (resumed)' : ''}`
    + `${warning ? ' (stale checkout)' : ''} (trust: ${trust})`;
}

async function run(cmd) {
  // Closing a session is a command like any other, so it can be driven from anywhere the queue
  // reaches — a voice command, the hub, an MCP tool — not only from a shell on this machine.
  // Type a follow-up into a session that is already running. Not a new session: the point is to add
  // to the conversation that is already there, with all of its context.
  if (cmd.kind === 'follow_up') {
    const target = String((cmd.payload || {}).session || '').trim();
    const prompt = String((cmd.payload || {}).prompt || '').trim();
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    if (!prompt) throw new Error('no prompt to send');
    return typeInto(target, prompt);
  }

  // Name the session itself, with the command Jared would type. Claude Code writes the `custom-title`
  // record, so the name comes back through the transcript rather than living only in our row.
  if (cmd.kind === 'rename_session') {
    const target = String((cmd.payload || {}).session || '').trim();
    const name = String((cmd.payload || {}).name || '').replace(/[\r\n]+/g, ' ').trim().slice(0, 60);
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    if (!name) throw new Error('no name to set');
    await typeInto(target, `/rename ${name}`);
    return `renamed ${target} to ${name}`;
  }

  if (cmd.kind === 'end_session') {
    const target = String((cmd.payload || {}).session || '').trim();
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    // Closing is a REQUEST TO FINISH, not a kill. The session is told to commit, push and bring the
    // No wrap-up handshake before closing. Every session already works under a standing rule to
    // commit and push after each logical change, so a session is safe to close at any moment by
    // construction — asking it to wrap up first only delayed freeing the machine by up to ten
    // minutes, and made a capacity problem wait on a model finishing a sentence.
    await evict(target);
    // PARKED, not deleted. Closing a session frees the machine; it does not throw the work away. The
    // row keeps the transcript id so it can be resumed with full context, and only an explicit
    // "forget" removes it.
    await sb(`desk_sessions?id=eq.${encodeURIComponent(target)}`, {
      method: 'PATCH', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify({ status: 'evicted', ended: new Date().toISOString() }),
    }).catch(() => {});
    return `parked ${target} (resumable)`;
  }
  // Bring an evicted session back with its full context. `claude --resume <id>` replays the
  // transcript, which is why the id had to be recorded when the session first started.
  if (cmd.kind === 'resume_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const rows = await sb(`desk_sessions?id=eq.${encodeURIComponent(id)}&select=*`);
    const row = rows?.[0];
    if (!row) throw new Error(`no such session: ${id}`);
    // Already running? Then "resume" means "talk to it". `claude --resume` on a LIVE session does not
    // reattach — it starts a second process replaying the same transcript, so the conversation forks
    // into two copies with one id, and a follow-up typed into one is invisible to the other. Whoever
    // is watching the original sees their message vanish.
    const alreadyUp = (await liveSessions()).some((l) => l.name === id && l.alive);
    if (alreadyUp) {
      const prompt = String((cmd.payload || {}).prompt || '').trim();
      if (!prompt) return `${id} is already running`;
      return `${id} was already running — ${await typeInto(id, prompt)}`;
    }
    if (!row.claude_session_id) throw new Error(`${id} has no recorded claude session id — cannot resume`);
    return startSession({ name: row.repo, prompt: (cmd.payload || {}).prompt || '', resumeId: row.claude_session_id, commandId: cmd.id, report: (cmd.payload || {}).report === true });
  }

  // SOURCE half of a handover: this machine owns the session and is giving it up.
  //
  // Order matters and is the whole point. Close first, because resuming a transcript that is still
  // live elsewhere does not reattach — it starts a second process replaying the same file, and the
  // two diverge with one id between them. Then flush the mirror, which is only complete once the
  // session has stopped writing. Only then is the destination asked to pick it up.
  if (cmd.kind === 'transfer_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const to = (cmd.payload || {}).to === 'mac' ? 'mac' : 'pi';
    if (!/^hub-[A-Za-z0-9._-]+$/.test(id)) throw new Error(`unusable session name: ${id}`);
    const rows = await sb(`desk_sessions?id=eq.${encodeURIComponent(id)}&select=*`);
    const row = rows?.[0];
    if (!row) throw new Error(`no such session: ${id}`);
    if (!row.claude_session_id) throw new Error(`${id} has no transcript recorded — nothing to hand over`);
    await evict(id);
    const at = await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0);
    const now = new Date().toISOString();
    await sb(`desk_sessions?id=eq.${encodeURIComponent(id)}`, {
      method: 'PATCH', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify({ status: 'transferred', ended: now, working: false,
                             ...(at != null ? { mirror_offset: at, mirror_at: now } : {}) }),
    });
    await sb('desk_commands', {
      method: 'POST', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify({ id: randomUUID(), kind: 'resume_mirrored',
                             payload: { target: to, claude_session_id: row.claude_session_id, repo: row.repo,
                                        label: row.label, origin: row.origin || 'dispatched', from: 'pi' } }),
    });
    return `handed ${id} to the ${to === 'mac' ? 'Mac' : 'Pi'}`;
  }

  // DESTINATION half: assemble the mirrored transcript here and resume it.
  if (cmd.kind === 'resume_mirrored') {
    const claudeId = String((cmd.payload || {}).claude_session_id || '').trim();
    const name = safeName((cmd.payload || {}).repo);
    if (!claudeId) throw new Error('no session id to resume');
    if (!name) throw new Error(`unusable project name: ${JSON.stringify((cmd.payload || {}).repo)}`);
    const { dir } = await ensureRepo(name);
    const file = await mirrorDown(claudeId, dir);
    // Nothing mirrored means the session never sat idle long enough to be uploaded. Resuming would
    // open an empty conversation wearing its id, which is worse than not resuming at all.
    if (!file) throw new Error(`nothing has been mirrored for ${claudeId}`);
    return startSession({ name, resumeId: claudeId, commandId: cmd.id,
                          label: (cmd.payload || {}).label || null,
                          origin: (cmd.payload || {}).origin || 'dispatched',
                          knownClaudeId: claudeId, transcriptPath: file,
                          // Everything already uploaded stays uploaded: this machine appends from here.
                          mirrorOffset: fs.statSync(file).size });
  }

  if (cmd.kind !== 'open_session') throw new Error(`unknown kind ${cmd.kind}`);
  const payload = cmd.payload || {};
  const name = safeName(payload.name);
  if (!name) throw new Error(`unusable project name: ${JSON.stringify(payload.name)}`);

  // Capacity is checked HERE, not inside startSession, so a request that cannot run right now becomes
  // a queued row rather than being dropped or forced onto a full machine. Resumes and the queue
  // runner call startSession directly and are deliberately not gated by this.
  const residents = (await liveSessions()).filter((l) => l.alive);
  const freeMb = memAvailableMb();
  if (residents.length >= RESIDENT_MAX || freeMb < MIN_FREE_MB) {
    await sb('desk_queue', {
      method: 'POST', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify([{ id: randomUUID(), worker: WORKER, repo: name,
                              prompt: String(payload.prompt || '').trim() || null,
                              report: payload.report === true,
                              label: payload.label || null }]),
    });
    return residents.length >= RESIDENT_MAX
      ? `queued behind ${residents.length} resident session(s)`
      : `queued — only ${freeMb}MB free, need ${MIN_FREE_MB}MB to start one safely`;
  }
  return startSession({ name, prompt: payload.prompt, commandId: cmd.id, report: payload.report === true, label: payload.label || null });
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
          await deadLetter(cmd, `expired unrun — queued ${Math.round(ageS)}s before this machine saw it`);
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
        await deadLetter(cmd, e.message);
      }
    }
  } catch (e) {
    console.error('[pi-desk] drain:', e.message);
  } finally {
    draining = false;
  }
}

// ---- retention -------------------------------------------------------------
// Sessions and commands accumulate for ever otherwise. Two days in, 16 session rows and 57 commands;
// the parked list is what shows in the pane, and it only ever grows.
//
// This runs HERE, on the always-on machine, and nowhere else — one retention rule in one file. The
// Mac sleeps, so a sweep there would be arbitrary.
//
// Deleting a row does NOT destroy a conversation: the transcript stays on disk on whichever machine
// ran it, and Claude Code can still resume it by id. What is dropped is the row that OFFERS it, and
// the mirrored copy in storage — which is why the mirror is cleared in the same step rather than
// left orphaned under a session id nothing can name any more.
const KEEP_CLOSED_DAYS = Number(process.env.KEEP_CLOSED_DAYS || 7);    // ended / transferred: not resumable anyway
const KEEP_PARKED_DAYS = Number(process.env.KEEP_PARKED_DAYS || 14);   // evicted: resumable, and worth keeping a while
const KEEP_COMMAND_DAYS = Number(process.env.KEEP_COMMAND_DAYS || 30); // the queue log
// Raw captures. Thousands of rows of transcribed speech, and the only time one is read is shortly
// after it goes wrong. Deleting a request does NOT delete the conversation: voice_messages rows have
// no cascade from here (verified), so the readable thread survives and only the capture row goes.
const KEEP_REQUEST_DAYS = Number(process.env.KEEP_REQUEST_DAYS || 7);
const ago = (days) => new Date(Date.now() - days * 86400000).toISOString();

async function forget(rows, why) {
  for (const r of rows || []) {
    if (r.claude_session_id) {
      try { await mirrorReset(r.claude_session_id); } catch (e) { console.error('[pi-desk] mirror reset:', e.message); }
    }
    await sb(`desk_sessions?id=eq.${encodeURIComponent(r.id)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } })
      .catch((e) => console.error('[pi-desk] forget:', e.message));
    console.log(`[pi-desk] forgot ${r.id} (${why})`);
  }
}

async function sweepRetention() {
  try {
    await forget(await sb(`desk_sessions?status=in.(ended,transferred)&ended=lt.${ago(KEEP_CLOSED_DAYS)}&select=id,claude_session_id`),
                 `closed over ${KEEP_CLOSED_DAYS} days ago`);
    await forget(await sb(`desk_sessions?status=eq.evicted&last_seen=lt.${ago(KEEP_PARKED_DAYS)}&select=id,claude_session_id`),
                 `parked over ${KEEP_PARKED_DAYS} days ago`);
    // The command log. Dead letters are NOT swept: an unretried one holds words Jared may still want,
    // and a retried or dismissed one is a decision he made, which is small and worth keeping.
    await sb(`desk_commands?created=lt.${ago(KEEP_COMMAND_DAYS)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } });
    await sb(`voice_requests?created_at=lt.${ago(KEEP_REQUEST_DAYS)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } });
  } catch (e) { console.error('[pi-desk] retention:', e.message); }
}

/**
 * Commands this machine claimed and never finished.
 *
 * A crash or a restart between claim and finish leaves a row 'running' for ever: no consumer will
 * look at it again, and nothing anywhere says it did not happen. Only the claimer can judge this —
 * hence claimed_by — and only well after the longest legitimate launch (a cold clone) could still
 * be in flight.
 */
const CLAIM_STALE_S = Number(process.env.CLAIM_STALE_S || 900);
async function sweepAbandonedClaims() {
  try {
    const cutoff = new Date(Date.now() - CLAIM_STALE_S * 1000).toISOString();
    const rows = await sb(`desk_commands?status=eq.running&claimed_by=eq.${WORKER}&updated=lt.${cutoff}&select=*`);
    for (const cmd of rows || []) {
      await finish(cmd.id, 'error', `abandoned — claimed by ${WORKER} and never finished`);
      await deadLetter(cmd, 'the machine stopped before it finished this');
      console.log(`[pi-desk] swept abandoned claim ${cmd.id}`);
    }
  } catch (e) { console.error('[pi-desk] sweep claims:', e.message); }
}

console.log(`[pi-desk] watching desk_commands (repos in ${REPO_ROOT}, owner ${GITHUB_OWNER})`);
heartbeat();
setInterval(heartbeat, PRESENCE_MS);
// Realtime is the primary path: a command inserted anywhere reaches this within a moment, rather
// than waiting out a poll interval. Both INSERT (a new command) and UPDATE (a queued row released)
// matter, so the subscription is not narrowed to inserts.
const rt = watchTable({ channel: 'pi-desk-commands', table: 'desk_commands', event: '*', onChange: drain, tag: 'pi-desk' });

drain();
// The backstop stretches out while realtime is healthy and tightens when it is not, so a subscription
// that dies silently degrades to slow-but-working instead of stopping altogether.
let pollTimer = null;
const pollLoop = () => {
  drain().catch(() => {});
  pollTimer = setTimeout(pollLoop, rt.isHealthy && rt.isHealthy() ? POLL_MS : POLL_DEGRADED_MS);
};
pollTimer = setTimeout(pollLoop, POLL_DEGRADED_MS);

reconcileSessions();
setInterval(reconcileSessions, PRESENCE_MS);
sweepAbandonedClaims();
setInterval(sweepAbandonedClaims, 300000);
sweepRetention();
setInterval(sweepRetention, 3600000);   // hourly is far more often than a daily boundary needs
