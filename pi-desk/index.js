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
import { readTail, liveSessions, evict, pickEviction } from './sessions.js';
// Which conversation a process is in, from Claude Code's own record — shared with the Mac, which
// reads the same files for the same reason.
import { sessionOf } from '../orchestrator/src/discover.js';
import { syncSessionWatch, reportTurnEnd } from '../orchestrator/src/sessionWatch.js';
import { relayFirstReply } from '../orchestrator/src/relay.js';
// Shared with the Mac rather than reimplemented: both halves of a transfer must agree byte for
// byte on what a mirrored transcript is.
import { mirrorUp, mirrorDown, mirrorReset } from '../orchestrator/src/mirror.js';

// Postgres and JS spell the same instant differently, so timestamps are compared as instants.
const TS_COLUMNS = new Set(['reply_at', 'active_at', 'ended', 'mirror_at', 'last_seen']);
const sameColumn = (col, next, current) => (TS_COLUMNS.has(col)
  ? (next ? Date.parse(next) : 0) === (current ? Date.parse(current) : 0)
  : (next ?? null) === (current ?? null));
// `last_seen` is a clock reading, not news — the reasoning is on the same constant in the Mac's
// orchestrator/src/deskSessions.js, and the two must not drift apart.
const LAST_SEEN_REFRESH_MS = 10 * 60 * 1000;

// The queue is empty essentially always — 14 rows have ever been written to it — and `desk_queue` is
// in the realtime publication now (data-layer 00104), so the tick reads it only when something has
// actually been queued. Starts true so a row queued while this process was down is still found.
let queueDirty = true;
let lastQueueRead = 0;
const QUEUE_BACKSTOP_MS = 10 * 60 * 1000;
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
// The SECOND memory bound, and the one MIN_FREE_MB cannot express.
//
// MemAvailable is the kernel's estimate of what could be handed to a new process without swapping.
// It says nothing about how much swap has ALREADY been spent, so a machine whose swap is completely
// gone reports exactly the same headroom as one that has never touched it. On 2026-09-07 this Pi sat
// at 75-100% swap for four days while MemAvailable stayed comfortably over the floor; the gate waved
// a session through, and the machine went unresponsive hard enough to need the plug pulled.
//
// Swap here is zram, which is compressed RAM rather than disk, so a full swap is not slow-but-fine
// the way a full disk swap is: it is RAM already spent, with nothing left to spill into. 70% leaves
// real margin, since the two OOM kills that day both happened with swap sitting around 75%.
const MAX_SWAP_PCT = Number(process.env.PI_DESK_MAX_SWAP_PCT || 70);

/** Percent of swap in use, 0 when there is no swap or /proc is unreadable (never block on it). */
function swapUsedPct() {
  try {
    const mi = fs.readFileSync('/proc/meminfo', 'utf8');
    const total = Number(mi.match(/^SwapTotal:\s+(\d+) kB/m)?.[1] || 0);
    const free  = Number(mi.match(/^SwapFree:\s+(\d+) kB/m)?.[1] || 0);
    return total ? Math.round(((total - free) / total) * 100) : 0;
  } catch { return 0; }
}

/**
 * Why starting a session right now would be unsafe, or null if it is safe.
 *
 * Both callers go through this rather than testing the numbers themselves: they are the same
 * question asked at two moments (a fresh dispatch, and the queue draining later), and when they were
 * two separate expressions only one of them learned about swap.
 */
function memoryPressure() {
  const freeMb = memAvailableMb();
  if (freeMb < MIN_FREE_MB) return `only ${freeMb}MB free, need ${MIN_FREE_MB}MB`;
  const swap = swapUsedPct();
  if (swap > MAX_SWAP_PCT) return `swap ${swap}% used, ceiling is ${MAX_SWAP_PCT}%`;
  return null;
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
/**
 * What this machine looks like right now, for the hub's status strip.
 *
 * It rides the heartbeat rather than getting a poller of its own: this already runs every 30s, the
 * numbers come from files this process reads anyway for the admission gate, and a second timer
 * hitting Supabase on a schedule is exactly the kind of thing that ran the egress bill up before.
 *
 * `sessions` is what the gate reasons about, so the strip shows the same count the gate does — the
 * point of putting this on the wall is to see the machine the way the thing refusing work sees it.
 */
function hostStats(residentCount) {
  const stats = { host: os.hostname() };
  try {
    const mi = fs.readFileSync('/proc/meminfo', 'utf8');
    const kb = (k) => Number(mi.match(new RegExp('^' + k + ':\\s+(\\d+) kB', 'm'))?.[1] || 0);
    const total = kb('MemTotal');
    // MemAvailable, not MemFree: page cache is not "used" in any sense a person means by it, and
    // `free` on this Pi reads ~85% used at rest purely because the kernel is caching.
    if (total) stats.mem_pct = Math.round(((total - kb('MemAvailable')) / total) * 100);
    const swapTotal = kb('SwapTotal');
    if (swapTotal) stats.swap_pct = Math.round(((swapTotal - kb('SwapFree')) / swapTotal) * 100);
  } catch { /* a machine that cannot read /proc simply reports no numbers */ }
  if (Number.isFinite(residentCount)) stats.sessions = residentCount;
  stats.session_max = RESIDENT_MAX;
  return stats;
}

let lastResidentCount = null;   // set by reconcileSessions, read by the next heartbeat

async function heartbeat() {
  try {
    await sb('worker_presence?on_conflict=worker', {
      method: 'POST',
      headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
      body: JSON.stringify([{ worker: WORKER, last_seen: new Date().toISOString(),
                              info: hostStats(lastResidentCount) }]),
    });
  } catch (e) { console.error('[pi-desk] heartbeat:', e.message); }
}

// ---- live sessions --------------------------------------------------------
// The later of two timestamps, either possibly missing.
const laterOf = (a, b) => (!a ? b || null : !b ? a : (Date.parse(a) >= Date.parse(b) ? a : b));

// Publish what is running and reap what is not, then run the queue. tmux is the source of truth
// rather than a list this process keeps, because this process restarts and tmux does not.
async function reconcileSessions() {
  const live = await liveSessions();
  const residents = [];

  for (const { name, alive, created, pid } of live) {
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
    // Which conversation the session is in, from its process's own record (orchestrator/src/
    // discover.js) — including after a `/clear`, which starts a new one inside the same process. Until
    // claude has written that record (its first second or so) the row keeps what it had.
    let tpath = row?.transcript_path || null;
    let cid = row?.claude_session_id || null;
    let mirrorFrom = row?.mirror_offset || 0;
    let rebound = false;   // what the old conversation last said is not what this one said
    const s = pid ? sessionOf(pid) : null;
    if (s && s.claudeId !== cid) {
      tpath = s.transcriptPath; cid = s.claudeId; rebound = true;
      mirrorFrom = 0;   // the mirror is keyed by conversation id, so a new one uploads from zero
      console.log(`[pi-desk] ${name} is in conversation ${cid.slice(0, 8)} now`);
    }
    const tail = tpath ? readTail(tpath) : { working: false, lastReply: null, turnAt: null, bytes: 0, lastActivity: 0 };
    // Whether a turn is in flight: the process's own record first. The transcript is only the fallback,
    // and on a long turn whose prompt has scrolled out of readTail's window it reads as idle — which
    // the reaper and pickEviction would take as leave to close the session mid-thought.
    const busy = s && s.busy != null ? s.busy : tail.working;
    // The net under the watcher for the reply's notification, as on the Mac: a finished turn newer than
    // the one this row records, and newer than the row (a resume's inherited history is not news), is
    // one fs.watch dropped.
    const at = (t) => (t ? Date.parse(t) : 0);
    // Reported only once the row below is written, as the watcher does: a surface told `done` fetches
    // the reply at once, and asked first it gets the previous one.
    const unreported = row && tail.replyAt && at(tail.replyAt) > Math.max(at(row.reply_at), at(row.started));
    // Mirror at the end of a turn — the only moment the transcript is a complete thought — so this
    // session can be picked up on the Mac later, including while this machine is unreachable.
    let mirrored = null;
    if (!busy && cid && tpath && tail.bytes > mirrorFrom) {
      try { mirrored = await mirrorUp(cid, tpath, mirrorFrom); }
      catch (e) { console.error('[pi-desk] mirror:', e.message); }
    }
    // lastActivity is what eviction sorts on. A session with no transcript reports 0 and therefore
    // sorts first — correct, since a session nothing can be read from is the least costly to lose.
    // `created` is tmux's own start clock, carried so the idle reaper can tell a session that has
    // gone quiet from one that has not written its first transcript line yet.
    // `tpath` rather than row.transcript_path: a session that adopted its transcript on THIS pass has
    // not been published yet, and the watcher would otherwise not start on it until the next tick.
    residents.push({ name, working: busy, bytes: tail.bytes, lastActivity: tail.lastActivity, created, row, tpath });

    // Upsert, not patch-if-known: a session tmux is running that the table never saw must still
    // appear, or "tmux is the source of truth" is not true.
    const payload = {
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
          last_reply: tail.lastReply ? tail.lastReply.slice(0, 4000) : (rebound ? null : row?.last_reply || null),
          // The prompt that reply answers, and carried by the same rule: it belongs to the reply, so it
          // is replaced only when the reply is.
          last_prompt: tail.lastReply ? (tail.lastPrompt ? tail.lastPrompt.slice(0, 4000) : null)
                                      : (rebound ? null : row?.last_prompt || null),
          // last_reply is the FINAL message of a turn; `working` is what says whether a newer one is
          // still being written. Without it a reader cannot tell an answer from a stale one.
          working: busy,
          reply_at: tail.replyAt || (rebound ? null : row?.reply_at) || null,
          // Both carried through for the same merge-duplicates reason as the label. `active_at` is the
          // last turn boundary, which orders and focuses sessions everywhere: the process's record when
          // there is one, written as-is so a wrong value cannot stick; otherwise the transcript's
          // reconstruction, which may only move it forward.
          pid: pid || row?.pid || null,
          active_at: s?.statusAt || laterOf(row?.active_at, tail.turnAt) || row?.started || new Date().toISOString(),
          mirror_offset: mirrored ?? mirrorFrom,
          ...(mirrored ? { mirror_at: new Date().toISOString() } : {}),
          last_seen: new Date().toISOString(),
    };
    // Realtime carries every real change the moment it happens; this tick is only the net under it. So
    // an unchanged session is not republished: the unconditional upsert rewrote rows nothing had
    // touched every 30s, and once `desk_sessions` is in the realtime publication each of those writes
    // also becomes a broadcast to every open screen.
    const same = row && Object.entries(payload)
      .every(([k, v]) => k === 'last_seen' || sameColumn(k, v, row[k]));
    const seenStale = !row || Date.now() - (row.last_seen ? Date.parse(row.last_seen) : 0) > LAST_SEEN_REFRESH_MS;
    if (!same || seenStale) {
      try {
        await sb('desk_sessions?on_conflict=id', {
          method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
          body: JSON.stringify([payload]),
        });
      } catch (e) { console.error('[pi-desk] publish:', e.message); }
    }
    if (unreported) reportTurnEnd(name, tail);
    // The net under the watcher's relay below, for a finished turn fs.watch dropped.
    if (!busy) relayFirstReply(name, tail).catch((e) => console.error('[pi-desk] relay:', e.message));
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

  // Watch what is running. The tick's job here is only to NOTICE a session — everything the session
  // then does reaches the wall the instant it happens, without waiting for the next one.
  syncSessionWatch(
    residents.map((r) => ({ id: r.name, status: 'resident', transcript_path: r.tpath })),
    (id, tail) => {
      relayFirstReply(id, tail).catch((e) => console.error('[pi-desk] relay:', e.message));
      // Returned: the watcher sends `done` only once the row holds the reply.
      return sb(`desk_sessions?id=eq.${encodeURIComponent(id)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({
          working: false,
          last_reply: tail.lastReply ? tail.lastReply.slice(0, 4000) : null,
          last_prompt: tail.lastPrompt ? tail.lastPrompt.slice(0, 4000) : null,
          reply_at: tail.replyAt || null,
          active_at: tail.turnAt || new Date().toISOString(),
          last_seen: new Date().toISOString(),
        }),
      }).catch((e) => console.error('[pi-desk] turn-end patch:', e.message));
    },
    (id, at) => {
      sb(`desk_sessions?id=eq.${encodeURIComponent(id)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({ working: true, active_at: at }),
      }).catch((e) => console.error('[pi-desk] turn-start patch:', e.message));
    });

  const kept = await reapIdleSessions(residents);
  lastResidentCount = kept.length;   // what the next heartbeat publishes to the hub's status strip
  await runQueue(kept);
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
  // Realtime says when something is queued; this read is what acts on it. Between events there is
  // nothing to act on, so the tick no longer asks — it asked every 30s for ever against a queue that
  // is almost always empty. The backstop covers an event missed while the socket was deaf.
  if (!queueDirty && Date.now() - lastQueueRead < QUEUE_BACKSTOP_MS) return;
  queueDirty = false;
  lastQueueRead = Date.now();
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
  const pressure = memoryPressure();
  if (pressure && residents.length < RESIDENT_MAX) {
    console.log(`[pi-desk] holding queue: ${pressure}`);
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
    // A queued resume names its conversation; everything else about it comes from the parked row.
    let args = { name: next.repo, prompt: next.prompt, report: next.report === true, label: next.label || null };
    if (next.resume_id) {
      const parked = await sb(`desk_sessions?claude_session_id=eq.${encodeURIComponent(next.resume_id)}` +
        '&select=*&order=started.desc.nullslast&limit=1').catch(() => null);
      args = parked?.[0] ? resumeArgs(parked[0], { prompt: next.prompt, report: next.report === true })
                         : { ...args, resumeId: next.resume_id };
    }
    const detail = await startSession(args);
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

/** Record which conversation a just-launched session is in, as soon as its claude has written its record. */
async function bindWhenUp(session, { attempts = 90, delayMs = 1000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    const me = (await liveSessions()).find((x) => x.name === session);
    const s = me?.pid ? sessionOf(me.pid) : null;
    if (s) {
      console.log(`[pi-desk] ${session} -> claude session ${s.claudeId}`);
      return sb(`desk_sessions?id=eq.${encodeURIComponent(session)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({ pid: me.pid, claude_session_id: s.claudeId, transcript_path: s.transcriptPath }),
      });
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  console.error(`[pi-desk] no claude came up in ${session} — it will not be resumable`);
}

/**
 * Launch a session and record everything needed to bring it back later.
 *
 * The Claude session id is the point: without it, closing a session destroys it. It cannot be asked
 * for (it is generated inside the process), so it is recovered by finding the transcript Claude Code
 * writes at startup — the filename IS the id.
 */
// Launch arguments for resuming one of this machine's own conversations. Its id is already known —
// `claude --resume` keeps it — and so is the transcript it appends to, so the row is published with
// both at launch instead of a tick later. That is what lets the hub see the conversation running again
// at once and never resume it a second time, which would put two processes on one transcript.
function resumeArgs(row, { prompt = null, report = false, commandId = null } = {}) {
  return { name: row.repo, prompt: prompt || '', resumeId: row.claude_session_id, commandId, report,
           label: row.label || null, origin: row.origin || null,
           knownClaudeId: row.claude_session_id, transcriptPath: row.transcript_path || null,
           mirrorOffset: row.mirror_offset || 0 };
}

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
  const session = launch(dir, name, full, resumeId);

  // Published immediately so the indicator lights up when work starts, not a poll later.
  await sb('desk_sessions?on_conflict=id', {
    method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
    // command_id is what lets a follow-up find this session: the dispatch that created it is the
    // only durable handle the caller had at the time, since the session did not exist yet.
    // The most recently active session from the moment it exists: it is the work he just asked for.
    body: JSON.stringify([{ id: session, worker: WORKER, repo: name, status: 'resident', command_id: commandId,
                            label: label || null, active_at: new Date().toISOString(),
                            ...(origin ? { origin } : {}),
                            ...(knownClaudeId ? { claude_session_id: knownClaudeId, transcript_path: transcriptPath,
                                                  mirror_offset: mirrorOffset ?? 0 } : {}),
                            prompt: String(prompt || '').trim().slice(0, 500) || null }]),
  }).catch((e) => console.error('[pi-desk] publish:', e.message));

  // A handed-over session already knows its id — it is the transcript that came across — so there is
  // nothing to search for, and searching would be wrong: the file was written a moment ago by the
  // mirror, not by Claude starting up.
  if (knownClaudeId) {
    return `tmux ${session} in ${dir} (resumed ${knownClaudeId.slice(0, 8)})${warning ? ' (stale checkout)' : ''} (trust: ${trust})`;
  }

  // Which conversation it is, from the process's own record once claude is up. Async on purpose — the
  // caller should not wait on the session coming up to learn that the launch happened. The reconcile
  // tick reads the same record, so a miss here costs one tick, not the session.
  bindWhenUp(session).catch((e) => console.error('[pi-desk] session:', e.message));

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
    const prompt = String((cmd.payload || {}).prompt || '').trim() || null;
    const report = (cmd.payload || {}).report === true;
    // A resume takes a slot like any other session. On a full Pi it waits in the queue instead of
    // becoming a third resident — two idle sessions have already been OOM-killed here (transcripts.js)
    // — and runQueue resumes it when one frees, evicting the longest-idle as it does for new work.
    const residents = (await liveSessions()).filter((l) => l.alive);
    const pressure = memoryPressure();
    if (residents.length >= RESIDENT_MAX || pressure) {
      await sb('desk_queue', {
        method: 'POST', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify([{ id: randomUUID(), worker: WORKER, repo: row.repo, prompt, report,
                                label: row.label || null, resume_id: row.claude_session_id }]),
      });
      return residents.length >= RESIDENT_MAX
        ? `queued resume of ${id} behind ${residents.length} resident session(s)`
        : `queued resume of ${id} — ${pressure}`;
    }
    return startSession(resumeArgs(row, { prompt, report, commandId: cmd.id }));
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
  // a queued row rather than being dropped or forced onto a full machine. A resume checks the same
  // limits in its own handler (resume_session, above); the queue runner calls startSession directly
  // because it has just made the room.
  const residents = (await liveSessions()).filter((l) => l.alive);
  const pressure = memoryPressure();
  if (residents.length >= RESIDENT_MAX || pressure) {
    await sb('desk_queue', {
      method: 'POST', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify([{ id: randomUUID(), worker: WORKER, repo: name,
                              prompt: String(payload.prompt || '').trim() || null,
                              report: payload.report === true,
                              label: payload.label || null }]),
    });
    return residents.length >= RESIDENT_MAX
      ? `queued behind ${residents.length} resident session(s)`
      : `queued — ${pressure}`;
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

// The phone recordings behind those requests: the hub keeps each command's clip in the voice-clips bucket
// so a bad transcription can be listened to. Same lifetime as the request row, so neither outlives the other.
const CLIP_BUCKET = 'voice-clips';
async function sweepClips() {
  const cutoff = Date.now() - KEEP_REQUEST_DAYS * 86400000;
  const headers = { apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}`, 'Content-Type': 'application/json' };
  for (;;) {
    const r = await fetch(`${SUPABASE_URL}/storage/v1/object/list/${CLIP_BUCKET}`, {
      method: 'POST', headers,
      body: JSON.stringify({ prefix: '', limit: 1000, sortBy: { column: 'created_at', order: 'asc' } }),
    });
    if (!r.ok) throw new Error(`clip list failed (${r.status})`);
    const old = (await r.json()).filter((o) => o.name && Date.parse(o.created_at) < cutoff).map((o) => o.name);
    if (!old.length) return;
    const d = await fetch(`${SUPABASE_URL}/storage/v1/object/${CLIP_BUCKET}`, {
      method: 'DELETE', headers, body: JSON.stringify({ prefixes: old }),
    });
    if (!d.ok) throw new Error(`clip delete failed (${d.status})`);
    console.log(`[pi-desk] deleted ${old.length} phone clip(s) older than ${KEEP_REQUEST_DAYS} days`);
    if (old.length < 1000) return;   // sorted oldest first, so a partly-old page means nothing older remains
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
    await sweepClips();
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
// A queued session gets its own subscription rather than a poll. Reconciling straight away means a
// freed slot is taken the moment the row lands instead of up to a tick later, and the flag is what
// makes the read happen at all — see runQueue.
watchTable({ channel: 'pi-desk-queue', table: 'desk_queue', event: '*', tag: 'pi-desk-queue',
  onChange: () => {
    queueDirty = true;
    reconcileSessions().catch((e) => console.error('[pi-desk] queue reconcile:', e.message));
  } });

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
