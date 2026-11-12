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
import { spawn, execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
// Shared with the Mac orchestrator rather than copied: it only writes ~/.claude.json and has nothing
// platform-specific in it, and two copies of "which keys make a project entry complete" would drift.
import { ensureTrusted } from '../orchestrator/src/trust.js';
import { readTail, liveSessions, evict, pickEviction, typeInto } from './sessions.js';
// Pictures attached in the hub, fetched to this machine for the session to read — shared with the Mac.
import { withAttachments, sweepStashedPrompts } from '../orchestrator/src/handoff.js';
import { loginExpiresAt } from '../orchestrator/src/login.js';
// Which conversation a process is in, from Claude Code's own record — shared with the Mac, which
// reads the same files for the same reason.
import { sessionOf } from '../orchestrator/src/discover.js';
// The same, for a Codex session (`agent = 'codex'`): its conversation is the rollout its process holds open.
import { codexSessionOf, codexReady } from '../orchestrator/src/codex.js';
import { sweepScratch } from './scratch.js';
import { forwardMovedFollowUp } from './moved.js';
import { createRequire } from 'node:module';
import { setupCgroups, limitSession, watchSession, liftBudget, sessionUsage, SESSION_BUDGET_MB, COMMAND_BUDGET_MB } from './cgroups.js';
import { syncSessionWatch, reportTurnEnd, expectPrompt } from '../orchestrator/src/sessionWatch.js';
import { relayFirstReply } from '../orchestrator/src/relay.js';

// Shared with the Mac rather than reimplemented: both halves of a transfer must agree byte for
// byte on what a mirrored transcript is.
import { mirrorUp, mirrorDown, mirrorReset, mirrorHas } from '../orchestrator/src/mirror.js';
// Where work goes when this machine cannot take it now, shared with the Mac (route.js).
import { wakeFly, presenceReady, nextMachine, handedVerdict, Reroute, mirroredPayload, flyCommandFor,
         MOVED_DEAD_MARKER, RESTARTED_MARKER, evacuateVerdict, movedTo, DESK_WORKER } from '../orchestrator/src/route.js';

// Postgres and JS spell the same instant differently, so timestamps are compared as instants.
const TS_COLUMNS = new Set(['reply_at', 'active_at', 'ended', 'mirror_at', 'last_seen']);
const sameColumn = (col, next, current) => (TS_COLUMNS.has(col)
  ? (next ? Date.parse(next) : 0) === (current ? Date.parse(current) : 0)
  : (next ?? null) === (current ?? null));
// `last_seen` is a clock reading, not news — the reasoning is on the same constant in the Mac's
// orchestrator/src/deskSessions.js, and the two must not drift apart.
const LAST_SEEN_REFRESH_MS = 10 * 60 * 1000;

// The same self-healing subscription the Mac's orchestrator uses. Shared rather than reimplemented:
// its header documents a hard-won lesson — supabase-js does not reliably recover a dead channel, and
// callers that fall back to a tight poll burn ~20k reads a day without noticing.
import { watchTable } from '../orchestrator/src/realtime.js';
import { missedByChannel } from './heard.js';
import { streak } from '../orchestrator/src/streak.js';
import { startLive } from '../orchestrator/src/live.js';

// Errors go to the ops responder (health/responder) through a copy of its reporter, as does the beat from
// the reconcile tick below. The Fly host sleeps whenever nothing runs, so it sends no beat: its silence
// is its normal state, not a fault.
const ops = createRequire(import.meta.url)('../orchestrator/src/ops_report.cjs');
ops.install({ machine: process.env.DESK_MACHINE === 'fly' ? 'fly' : 'pi', service: process.env.DESK_MACHINE === 'fly' ? 'fly-desk' : 'pi-desk' });

const exec = promisify(execFile);

/** The conversation a pane's process is in, read the way its agent records it. */
const sessionFor = (agent, pid) => (!pid ? null : agent === 'codex' ? codexSessionOf(pid) : sessionOf(pid));

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
// A pending command older than this was meant for a machine that did not take it in time. It is not
// run late here and not dropped: it goes to Fly (sendToFly), which is always there. Fly itself runs
// whatever reaches it, however old.
const COMMAND_TTL_S = Number(process.env.COMMAND_TTL_S || 120);
// How long a session gets to commit, push and update docs before it is closed regardless. Generous
// on purpose: this is real work, and cutting it short is the thing wrapping up exists to prevent.
// The same consumer runs on Fly as the last resort, when neither the Pi nor the Mac can take a session
// (fly-desk/). Everything below that only makes sense on the Pi — memory cgroups, the hand-offs to the
// Mac, the retention sweeps, the login date — is switched on MACHINE, so there is one copy of how a
// session is launched, watched and parked.
const MACHINE = process.env.DESK_MACHINE === 'fly' ? 'fly' : 'pi';
const FLY = MACHINE === 'fly';
const WORKER = FLY ? 'fly-desk' : 'pi-desk';
// MEMORY is the only bound on how many sessions run here. A session holds ~250-420MB for as long as
// it is open, whatever it is doing, and grows as its conversation does; its CPU use is negligible,
// because a turn is almost entirely waiting on the API. So there is no session count and no cap on how
// many may be mid-turn: a count only stands in for memory, and a "busy" cap held a request behind two
// working sessions while the processor sat idle. Admission is makeRoom: launch when shortfall() is
// clear, park idle sessions until it is, and hand the work on when it still is not: to the Mac when it is
// awake and signed in, else to Fly (route.js). Nothing waits here for room.
//
// This Pi is not for memory-heavy work. Each session runs in a cgroup (cgroups.js), and one whose
// commands hold more than COMMAND_BUDGET_MB is moved off it (onHeavy) rather than left to push the
// machine into the kernel's OOM killer, which takes whole sessions mid-turn.

// A TIME bound, and the one the other limits do not provide.
//
// makeRoom runs only when something new wants to start. Nothing starting means nothing re-examines
// what is already resident, so a session that finished its work simply stays. On 2026-09-04 two Amazon-cart sessions from Sep 3
// (one of which had already answered, one of which returned __ESCALATE__) sat idle for two days
// holding ~516MB, and the kernel spent that afternoon killing the wall display's renderer instead —
// 13 OOM kills in the final hour. Nothing was over any limit; there was no limit to be over.
//
// Eviction is not destructive: an evicted session keeps its transcript and `resume_session` brings
// it back with full context, so the cost of reaping early is tokens on resume, not lost work. That
// asymmetry is why this can be aggressive where pickEviction is a last resort.
const IDLE_MAX_S = Number(process.env.PI_DESK_IDLE_MAX_S || 3600);
// Memory kept free beyond every session's budget, for what is not a session: the wall display, the
// automation browser and the voice worker all move around, and Chromium is the process that loses an
// out-of-memory fight on this machine.
const RESERVE_MB = Number(process.env.PI_DESK_RESERVE_MB || 300);

/** MemAvailable in MB — the kernel's own estimate of what is actually reclaimable, not `free`. */
function memAvailableMb() {
  try {
    const m = fs.readFileSync('/proc/meminfo', 'utf8').match(/^MemAvailable:\s+(\d+) kB/m);
    return m ? Math.round(Number(m[1]) / 1024) : Infinity;   // unreadable → never block on it
  } catch { return Infinity; }
}
// The SECOND memory bound, and the one MemAvailable cannot express.
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
 * Why starting one more session right now would be unsafe, or null if it is safe.
 *
 * Every session may grow to its budget, so room is counted against budgets rather than against what
 * sessions hold this minute: the new one's whole budget, plus whatever each running session has left
 * before it reaches its own, plus the reserve. Admitting against current usage alone is what let
 * sessions that grew after launch run the Pi out of memory. A session's growth past its budget is not
 * counted here because it does not stay — onHeavy moves it to the Mac.
 */
function shortfall(residents) {
  const growth = residents.reduce((t, r) =>
    t + Math.max(0, SESSION_BUDGET_MB - (sessionUsage(r.name)?.currentMb ?? 0)), 0);
  const need = SESSION_BUDGET_MB + growth + RESERVE_MB;
  const freeMb = memAvailableMb();
  if (freeMb < need) {
    return `${freeMb}MB free, need ${need}MB (${SESSION_BUDGET_MB} for it, ${growth} for ` +
           `${residents.length} running to reach their budgets, ${RESERVE_MB} reserve)`;
  }
  const swap = swapUsedPct();
  if (swap > MAX_SWAP_PCT) return `swap ${swap}% used, ceiling is ${MAX_SWAP_PCT}%`;
  return null;
}

/**
 * Resident sessions as admission sees them: live in tmux, with whether a turn is in flight and when
 * each was last active. The tick builds the same list from transcripts; this reads the row the tick
 * and the session watcher keep current, because a launch should not wait on a full reconcile. A
 * session with no row yet is treated as working — it was launched a moment ago, and parking it would
 * kill a request before it answered.
 */
async function residentsNow() {
  const live = (await liveSessions()).filter((l) => l.alive);
  if (!live.length) return [];
  const rows = await sb(`desk_sessions?worker=eq.${WORKER}&select=id,working,active_at`).catch(() => []);
  const byId = new Map((rows || []).map((r) => [r.id, r]));
  return live.map((l) => {
    const row = byId.get(l.name);
    return { ...l, working: row ? row.working === true : true,
             lastActivity: (row?.active_at && Date.parse(row.active_at)) || l.created || 0 };
  });
}

// Sessions being closed right now, so the watcher and the tick never both close one.
const closing = new Set();

/**
 * Close a session that asked to close itself (end_session on its own row stamps `close_after_turn`),
 * once the turn it asked in has ended and its reply is out. Asked mid-turn, closing at once would kill
 * the process before the reply is written. Re-reads the row: the flag is set mid-turn, after the tick
 * that the watcher's rows came from. Returns true when it closed it.
 */
async function closeAfterTurn(name, tail) {
  if (closing.has(name)) return false;
  closing.add(name);
  try {
    const row = (await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}&select=*`))?.[0];
    if (!row || row.status !== 'resident' || !row.close_after_turn) return false;
    const at = (t) => (t ? Date.parse(t) : 0);
    if (!tail?.replyAt || at(tail.replyAt) <= at(row.close_after_turn)) return false;
    if (row.pid && sessionFor(row.agent, row.pid)?.busy) return false;   // still writing; the next turn end or tick closes it
    // The whole conversation goes up first, so it can be resumed on either machine. A Codex rollout is
    // not mirrored: it is resumed only where it ran.
    let mirror = {};
    if (row.claude_session_id && row.transcript_path && row.agent !== 'codex') {
      try {
        const to = await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0);
        if (to != null) mirror = { mirror_offset: to, mirror_at: new Date().toISOString() };
      } catch (e) { console.error(`[pi-desk] mirror before self-close of ${name}:`, e.message); }
    }
    await park(name, { close_after_turn: null, working: false, ...mirror });
    console.log(`[pi-desk] closed ${name} after its reply, as it asked`);
    return true;
  } finally { closing.delete(name); }
}

/** Close a session so it can be resumed later, and record that it was parked. */
async function park(name, extra = {}) {
  await evict(name);
  await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}`, {
    method: 'PATCH', headers: { Prefer: 'return=minimal' },
    body: JSON.stringify({ status: 'evicted', ended: new Date().toISOString(), working: false, ...extra }),
  }).catch((e) => console.error('[pi-desk] mark evicted:', e.message));
}

/**
 * Make room here for one more session. Returns null when it may start now, else why it cannot.
 *
 * Parks idle sessions, least recently active first (pickEviction), until shortfall clears. A
 * session mid-turn is never parked. Memory is measured again after each one, because what a session
 * frees varies with how long its conversation has grown, and the shortage may not be sessions at all.
 */
async function makeRoom(residents, forWhat) {
  let left = residents.slice();
  for (let why = shortfall(left); why; why = shortfall(left)) {
    const victim = pickEviction(left);
    if (!victim) return why;
    console.log(`[pi-desk] parking ${victim.name} to make room for ${forWhat} (${why})`);
    await park(victim.name);
    left = left.filter((r) => r !== victim);
  }
  return null;
}

/**
 * Why new work should start on the Mac rather than here, or null. The Pi holds one session comfortably
 * beside the wall display and the automation browser, and a second is what runs it into swap, so while
 * one is running and the Mac is awake the next goes to the Mac. With the Mac asleep this says nothing,
 * and the Pi takes the work if memory allows (makeRoom), parking idle sessions to fit it.
 */
async function preferMac(residents) {
  if (FLY || !residents.length || !(await macIsAwake(false))) return null;
  return `the Pi is already running ${residents.length === 1 ? 'a session' : `${residents.length} sessions`}`;
}

/** Where the Mac keeps a project, from the list its orchestrator publishes; the Pi's layout differs. */
async function macPath(name) {
  if (name === 'root') return '~';
  const rows = await sb(`repos?name=eq.${encodeURIComponent(name)}&select=path`).catch(() => null);
  return rows?.[0]?.path || null;
}

/**
 * Where work this machine cannot take now goes: the Mac when it is awake and signed in (for `agent`),
 * else Fly. Null on Fly, which is the last resort and launches anyway.
 */
async function elsewhere(agent = 'claude') {
  if (FLY) return null;
  return nextMachine('pi', { macReady: await macIsAwake(false, agent) });
}

/** Insert a command for another machine, waking Fly first when it is for Fly. Returns its id. */
async function sendCommand(kind, payload) {
  if (payload.target === 'fly' && !FLY) await wakeFly();
  const id = randomUUID();
  await sb('desk_commands', {
    method: 'POST', headers: { Prefer: 'return=minimal' },
    body: JSON.stringify({ id, kind, payload }),
  });
  return id;
}

/**
 * Send a new session to another machine because this one cannot take it. Returns the verdict.
 *
 * A NEW command rather than re-targeting this one: each machine wakes on inserts only. The id goes back in
 * this command's detail (`handed to the Mac as <id>`, `handed to Fly as <id>`), which is how
 * start_session follows the work to where it actually launched and reports that, instead of reporting a
 * hand-off as a start. The payload's path is this Pi's layout; the Mac launches only paths it published,
 * so it is re-resolved by name, and a project the Mac does not have goes to Fly, which clones it.
 */
async function handOpen(payload, why) {
  let target = await elsewhere(payload.agent === 'codex' ? 'codex' : 'claude');
  let p = null;
  if (target === 'mac') {
    p = await macPath(safeName(payload.name) || ROOT);
    if (!p) { why = `${why}; the Mac has no project ${payload.name}`; target = 'fly'; }
  }
  const id = await sendCommand('open_session', { ...payload, ...(p ? { path: p } : {}), target });
  return handedVerdict(target, id, why);
}

/**
 * Resume a session on another machine from the mirror — the move for a session that is already closed
 * here. Its transcript is flushed to the mirror, the row is marked transferred so only one machine ever
 * holds it, and `target` resumes it. Fly is woken before anything is marked, so a machine that will not
 * take it leaves the session where it was. Returns the command id.
 */
async function handResume(row, target, { prompt = null, msg = null, moved = null, midTurn = false } = {}) {
  if (target === 'fly' && !FLY) await wakeFly();
  const at = await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0);
  const now = new Date().toISOString();
  await sb(`desk_sessions?id=eq.${encodeURIComponent(row.id)}`, {
    method: 'PATCH', headers: { Prefer: 'return=minimal' },
    body: JSON.stringify({ status: 'transferred', ended: now, working: false,
                           ...(at != null ? { mirror_offset: at, mirror_at: now } : {}) }),
  });
  return sendCommand('resume_mirrored', mirroredPayload(row, { target, from: MACHINE, prompt, msg, moved, midTurn }));
}

/**
 * A session went over its memory budget holding memory it cannot give back: heavy work, which this
 * Pi is not for. It moves now, mid-turn — to the Mac when it is awake, else to Fly — closed here,
 * everything it was running killed with its cgroup, and resumed there with its whole conversation and an
 * instruction to redo what was cut off. Waiting for the turn to end is not an option, because the turn
 * is the heavy work.
 *
 * Everything that can refuse the move is checked before the session is closed: Fly is woken first, so
 * a destination that will not answer leaves the session running here, unthrottled. A handoff that fails
 * after the close resumes the session here again rather than losing it. A session that cannot move (a
 * Codex one, or one with no conversation yet) stays, unthrottled; its hard ceiling still keeps the rest
 * of the Pi safe, and tool.sh makes the command that crosses it the thing that dies, not the session.
 */
// The message that restarts a moved session's turn where it lands. It shows in the feeds as the prompt
// that turn answers, so it is a bare label saying what sent it — never a word like "Continue", which a
// session that had just proposed something would take as the user approving it.
const MOVED_MARKER = '[Moved from the Pi]';
const moving = new Set();
async function onHeavy(name, usage) {
  if (moving.has(name)) return;
  moving.add(name);
  try {
    const why = `its commands were holding ${usage.commandMb}MB, over the ${COMMAND_BUDGET_MB}MB a session's commands may hold on the Pi, which is not for memory-heavy work`;
    const row = (await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}&select=*`))?.[0];
    if (!row?.claude_session_id || row.agent === 'codex') {
      console.log(`[pi-desk] ${name}: ${why}, but ${row?.agent === 'codex' ? 'a Codex session cannot move' : 'it has no transcript to move'} — it stays, unthrottled`);
      liftBudget(name);
      return;
    }
    const target = await elsewhere();
    if (target === 'fly') {
      try { await wakeFly(); } catch (e) {
        console.error(`[pi-desk] ${name}: ${why}, and Fly would not wake to take it (${e.message}) — it stays, unthrottled`);
        liftBudget(name);
        return;
      }
    }
    // Whether to restart a turn there is read from the live process BEFORE it is closed. Only a
    // session mid-turn gets one: a background job can outgrow the budget after the turn has ended on a
    // question for the user, and any message that then restarted the session would read as the user's answer.
    // A session that was waiting on the user resumes idle, with the note, and waits for the user there.
    const pid = (await liveSessions()).find((l) => l.name === name)?.pid;
    const rec = pid ? sessionOf(pid) : null;
    const midTurn = rec?.busy === true && !rec.waiting;
    console.log(`[pi-desk] ${name}: ${why} — moving it to ${target} (${midTurn ? 'mid-turn' : 'between turns'})`);
    await evict(name);
    try {
      const id = await handResume(row, target, { prompt: midTurn ? MOVED_MARKER : null, moved: why, midTurn });
      console.log(`[pi-desk] ${name} ${handedVerdict(target, id)}`);
    } catch (e) {
      console.error(`[pi-desk] ${name} was closed to move it to ${target} and the handoff failed (${e.message}); resuming it here`);
      await startSession(resumeArgs(row, { prompt: midTurn ? MOVED_MARKER : null }))
        .catch((e2) => console.error(`[pi-desk] resuming ${name} here after the failed move also failed:`, e2.message));
    }
  } catch (e) {
    console.error(`[pi-desk] moving ${name} off the Pi failed:`, e.message);
  } finally {
    moving.delete(name);
  }
}

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
  return stats;
}

let lastResidentCount = null;   // set by reconcileSessions, read by the next heartbeat

async function heartbeat() {
  try {
    await sb('worker_presence?on_conflict=worker', {
      method: 'POST',
      headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
      body: JSON.stringify([{ worker: WORKER, last_seen: new Date().toISOString(),
                              // Fly runs on the year-long token, which has no date to publish.
                              // `codex`: whether a Codex session can start here (data-layer routes agent=codex on it).
                              info: { ...hostStats(lastResidentCount), codex: codexReady(),
                                      ...(FLY ? {} : { login_expires_at: loginExpiresAt() }) } }]),
    });
  } catch (e) { console.error('[pi-desk] heartbeat:', e.message); }
}

// ---- live sessions --------------------------------------------------------
/**
 * Upload what a session has just written (the transcript watcher calls this a few seconds after a
 * write, sessionWatch.js `onAppend`). Re-reads the row so a session moved off this machine is not
 * uploaded over by the copy left here: only this worker's resident row mirrors.
 */
async function mirrorAsWritten(name, tpath) {
  const row = (await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}`
    + '&select=status,worker,agent,claude_session_id,transcript_path,mirror_offset'))?.[0];
  if (!row || row.status !== 'resident' || row.worker !== WORKER || row.agent === 'codex') return;
  if (!row.claude_session_id || row.transcript_path !== tpath) return;
  const at = await mirrorUp(row.claude_session_id, tpath, row.mirror_offset || 0);
  if (at == null || at === row.mirror_offset) return;
  await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}&status=eq.resident`, {
    method: 'PATCH', headers: { Prefer: 'return=minimal' },
    body: JSON.stringify({ mirror_offset: at, mirror_at: new Date().toISOString() }),
  });
}

// The later of two timestamps, either possibly missing.
const laterOf = (a, b) => (!a ? b || null : !b ? a : (Date.parse(a) >= Date.parse(b) ? a : b));

// Publish what is running and reap what is not, then run the queue. tmux is the source of truth
// rather than a list this process keeps, because this process restarts and tmux does not.
// The running sessions as of the last tick: a change means one ended (or began), which is when a
// finished conversation's scratch directory is cleared (scratch.js) — not on every tick.
let lastLiveKey = null;
// Until the first sweep has run: a resident row whose session is gone then was running when this
// machine (or this process's tmux) went down, rather than one that ended while it watched.
let startupSweep = true;
// Sessions already reported as moved away while this machine was gone, so each is reported once.
const fenced = new Set();

/**
 * Close the copy left here of a session another machine has taken over (contract §5): it was moved
 * while this machine was unreachable. Its row is not written back to resident, and nothing of it is
 * mirrored again — the row's status already stops mirrorAsWritten.
 */
async function fence(name, where) {
  if (!fenced.has(name)) {
    fenced.add(name);
    console.error(`[pi-desk] ${name} was moved to ${where} while this machine was away; closed the local copy`);
  }
  await evict(name);
}

async function reconcileSessions() {
  const selfClose = [];   // sessions that asked to close themselves and have finished their turn
  const live = await liveSessions();
  // Conversations another machine holds resident, for fencing below: one read per tick.
  const elsewhere = new Map(((await sb(`desk_sessions?status=eq.resident&worker=neq.${WORKER}`
    + '&claude_session_id=not.is.null&select=id,worker,claude_session_id,started')) || [])
    .map((r) => [r.claude_session_id, r]));
  const residents = [];
  const liveKey = live.filter((l) => l.alive).map((l) => `${l.name}:${l.pid}`).sort().join(',');
  if (liveKey !== lastLiveKey) { lastLiveKey = liveKey; sweepScratch(); }

  for (const { name, alive, created, pid, agent: paneAgent } of live) {
    if (!alive) {
      // A husk: tmux session whose claude has exited. These are what accumulate, one per launch.
      try { await exec('tmux', ['kill-session', '-t', name], { timeout: 10000 }); } catch {}
      console.log(`[pi-desk] reaped finished session ${name}`);
      continue;
    }
    // A failed read ends the tick rather than reading as "no row". Treated as absent, the upsert below
    // went on to write the session as though it were new — label null, conversation "rebound" — and on
    // a flaky network the read fails while the write a moment later succeeds, which wiped dispatch labels.
    const rows = await sb(`desk_sessions?id=eq.${encodeURIComponent(name)}&select=*`);
    const row = rows?.[0] || null;
    const where = movedTo(row, elsewhere, created);
    if (where) { await fence(name, where); continue; }
    // Which conversation the session is in, from its process's own record (orchestrator/src/
    // discover.js) — including after a `/clear`, which starts a new one inside the same process. Until
    // claude has written that record (its first second or so) the row keeps what it had.
    let tpath = row?.transcript_path || null;
    let cid = row?.claude_session_id || null;
    let mirrorFrom = row?.mirror_offset || 0;
    let rebound = false;   // what the old conversation last said is not what this one said
    // Which agent: the pane says, once its process is up; until then the row (written at launch) does.
    const agent = paneAgent || row?.agent || 'claude';
    const s = sessionFor(agent, pid);
    // A Codex resume may write its turns to a rollout of its own under the same thread id: follow the file.
    if (s && s.claudeId === cid && s.transcriptPath !== tpath) tpath = s.transcriptPath;
    if (s && s.claudeId !== cid) {
      // `/clear` leaves the OLD conversation's uploaded parts with nothing pointing at them: the row
      // is about to name the new one, and the mirror is keyed by conversation id. Clear them here or
      // they sit in the bucket for ever — retention cannot reach them, because by then the row it
      // sweeps names a different conversation entirely.
      const abandoned = cid;
      if (abandoned) {
        mirrorReset(abandoned).catch((e) => console.error('[pi-desk] mirror reset on rebind:', e.message));
      }
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
    // The transcript watcher mirrors as the session writes (mirrorAsWritten), so it can be picked up on
    // another machine at any moment, including mid-turn while this one is unreachable. This is the net
    // under it for a write fs.watch dropped, at the end of a turn.
    let mirrored = null;
    if (!busy && cid && tpath && agent !== 'codex' && tail.bytes > mirrorFrom) {
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
          id: name, worker: WORKER, status: 'resident', agent,
          repo: row?.repo || name.replace(/^hub-/, '').replace(/-[^-]*$/, ''),
          claude_session_id: cid,
          transcript_path: tpath,
          // Carried through explicitly: this is a merge-duplicates upsert, so any column it sends
          // overwrites. Omitting the label would blank it on the very next reconcile tick.
          //
          // A name typed by hand (`/rename`, which writes a `custom-title` record) outranks the
          // label the router wrote — the same rule the Mac applies. Without this the rename reached
          // the session and nothing ever read it back, so the pane kept showing the old label.
          //
          // With no row yet, neither is sent unless the transcript names the session: a tick that reads
          // between launch() and startSession's own publish would otherwise land after it and blank the
          // dispatch label that publish just wrote.
          ...(tail.customTitle || row ? {
            label: tail.customTitle ? tail.customTitle.slice(0, 60) : (row?.label || null),
            // Separately, so a tab can show the repo unless the session was named by hand.
            custom_title: tail.customTitle ? tail.customTitle.slice(0, 60) : (row?.custom_title || null),
          } : {}),
          last_reply: tail.lastReply ? tail.lastReply.slice(0, 20000) : (rebound ? null : row?.last_reply || null),
          // The prompt that reply answers, and carried by the same rule: it belongs to the reply, so it
          // is replaced only when the reply is.
          last_prompt: tail.lastReply ? (tail.lastPrompt ? tail.lastPrompt.slice(0, 20000) : null)
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
    // And under the watcher's self-close, for the same dropped turn end. Closed after this loop, so
    // the publish above cannot land after the park and mark it running again.
    if (!busy && row?.close_after_turn) selfClose.push([name, tail]);
  }
  for (const [name, tail] of selfClose) {
    const i = residents.findIndex((r) => r.name === name);
    if (await closeAfterTurn(name, tail) && i >= 0) residents.splice(i, 1);
  }

  // Rows we publish that tmux no longer has: mark evicted rather than delete, so the transcript stays
  // reachable and the session can be resumed. Only genuinely forgotten sessions are deleted, and only
  // when the user says so.
  //
  // On the first sweep after this process starts, a row that was mid-turn is not just parked: its
  // session died with the machine (or its tmux), in the middle of work, so it is resumed with a note
  // saying so — here, through the same admission as any resume, which hands it on when there is no
  // room. Fly resumes from the mirror, since its disk does not survive a restart. The mark is
  // conditional on the row still being resident, so a session another machine already took over is left.
  try {
    const rows = await sb(`desk_sessions?worker=eq.${WORKER}&status=eq.resident&select=id,working,claude_session_id,agent,repo,label,origin`);
    const liveNames = new Set(residents.map((r) => r.name));
    for (const r of rows || []) {
      if (liveNames.has(r.id)) continue;
      const parked = await sb(`desk_sessions?id=eq.${encodeURIComponent(r.id)}&status=eq.resident`, {
        method: 'PATCH', headers: { Prefer: 'return=representation' },
        body: JSON.stringify({ status: 'evicted', ended: new Date().toISOString(), working: false }),
      });
      if (!parked?.length) continue;
      if (startupSweep && r.working && r.claude_session_id && !elsewhere.has(r.claude_session_id)
          && !(FLY && r.agent === 'codex')) {
        console.error(`[pi-desk] ${r.id} was mid-turn when this machine went down; resuming it`);
        const prompt = RESTARTED_MARKER(MACHINE);
        const cmd = FLY
          ? { kind: 'resume_mirrored', payload: mirroredPayload(r, { target: MACHINE, from: MACHINE, prompt }) }
          : { kind: 'resume_session', payload: { target: MACHINE, session: r.id, prompt } };
        await sendCommand(cmd.kind, cmd.payload)
          .catch((e) => console.error(`[pi-desk] resuming ${r.id} after the restart failed:`, e.message));
      } else {
        console.log(`[pi-desk] session left residency: ${r.id}`);
      }
    }
    startupSweep = false;
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
          last_reply: tail.lastReply ? tail.lastReply.slice(0, 20000) : null,
          last_prompt: tail.lastPrompt ? tail.lastPrompt.slice(0, 20000) : null,
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
    },
    (id, tail) => closeAfterTurn(id, tail),
    mirrorAsWritten);

  const kept = await reapIdleSessions(residents);
  lastResidentCount = kept.length;   // what the next heartbeat publishes to the hub's status strip
  exitIfIdle(kept.length);
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
    await park(r.name);
  }
  return kept;
}

// ---- the old queue --------------------------------------------------------
// `desk_queue` held work this machine had no room for, until a slot came free. Nothing writes it any
// more: work this machine cannot take goes to another machine at once (handOpen, handResume). Rows left
// from before are routed out once, at startup, through the same admission as any command.
async function drainQueue() {
  let rows;
  try { rows = await sb(`desk_queue?worker=eq.${WORKER}&claimed=is.null&select=*&order=created.asc`); }
  catch (e) { return console.error('[pi-desk] reading the old queue:', e.message); }
  for (const next of rows || []) {
    try {
      const claimed = await sb(`desk_queue?id=eq.${encodeURIComponent(next.id)}&claimed=is.null`, {
        method: 'PATCH', headers: { Prefer: 'return=representation' },
        body: JSON.stringify({ claimed: new Date().toISOString() }),
      });
      if (!Array.isArray(claimed) || !claimed.length) continue;
    } catch (e) { console.error('[pi-desk] claiming a queued row:', e.message); continue; }
    // As a command, so it is admitted, launched or handed on exactly as one would be.
    let cmd = { id: randomUUID(), kind: 'open_session',
                payload: { name: next.repo, prompt: next.prompt || undefined, report: next.report === true || undefined,
                           label: next.label || undefined, ...(next.agent === 'codex' ? { agent: 'codex' } : {}) } };
    try {
      if (next.resume_id) {
        const parked = (await sb(`desk_sessions?claude_session_id=eq.${encodeURIComponent(next.resume_id)}` +
          '&select=id&order=started.desc.nullslast&limit=1'))?.[0];
        if (!parked) throw new Error(`no parked row for ${next.resume_id}`);
        cmd = { id: cmd.id, kind: 'resume_session', payload: { session: parked.id, prompt: next.prompt || undefined, report: next.report === true } };
      }
      console.log(`[pi-desk] from the old queue -> ${await runCommand(cmd)}`);
    } catch (e) {
      console.error('[pi-desk] queued launch failed:', e.message);
      // The queued row is deleted below either way, so its words are kept here, as for any command.
      await deadLetter(cmd, `queued launch failed: ${e.message}`);
    }
    await sb(`desk_queue?id=eq.${encodeURIComponent(next.id)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } })
      .catch((e) => console.error('[pi-desk] dequeue:', e.message));
  }
}

// Awake AND signed in (route.js presenceReady): a Mac whose /login has expired launches a session that
// does nothing, so it cannot take work, and work it cannot take goes to Fly. A beat without the field
// (an older orchestrator) counts as signed in. For a Codex session, Codex signed in there is what counts.
async function macIsAwake(ifUnsure = true, agent = 'claude') {
  try {
    const rows = await sb('worker_presence?worker=eq.desk&select=last_seen,info');
    return presenceReady(rows?.[0], { staleS: MAC_STALE_S, agent });
  } catch { return ifUnsure; }   // routing assumes the Mac has it rather than double-launch; a hand-off assumes not
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
// voice tool when the user says so). Reports are opt-in because most dispatched work just needs
// doing, and a page about a one-line change is noise.
//
// It rides in the prompt rather than living in CLAUDE.md so it can be per-request at all: a file
// both machines share applies to every session, including the ones opened by hand at the
// keyboard, which have no business filing reports at the user.
const REPORT_DIRECTIVE =
  '[When you have finished this work, call the publish_report tool with an HTML write-up of what '
  + 'you did — that is how Jared reads the outcome on his phone. Then answer normally here as '
  + 'well: the report is an extra artifact, never a replacement for replying.]';

// ---- launching ------------------------------------------------------------
// An ops-responder incident session starts through the health repo's launcher, which runs claude
// with permission checks off so it can fix what it finds (health/responder/README.md § The incident
// agent's permissions). The label is the marker
// because it is the one field every relaunch carries: dispatch, queue, resume and hand-over alike.
const INCIDENT_LAUNCHER = path.join(REPO_ROOT, 'health', 'responder', 'bin', 'incident-claude');
const isIncident = (label) => /^Incident: /.test(String(label || ''));

function launch(dir, name, prompt, resumeId = null, reuse = null, agent = 'claude', incident = false) {
  const session = reuse || `hub-${name}-${Date.now().toString(36)}`;
  // The Remote Control name is an IDENTITY in the Claude app and must be unique per SESSION. The
  // tmux name already carries a timestamp suffix for exactly that reason, so the RC name reuses it
  // rather than passing the bare repo — which made every session on a repo register as that repo.
  //
  // A resume passes the parked session's own name (`reuse`), so the conversation comes back under the
  // identity it had: same tmux name, same Remote Control name, same row. It is the same session, and
  // naming it afresh made the Claude app list it as a different one. Unique still holds — the caller
  // has made sure nothing by that name is running.
  const rcName = session.slice(4);
  const here = path.dirname(new URL(import.meta.url).pathname);
  // A Codex session has its own launcher, taking the same arguments.
  const launcher = path.join(here, agent === 'codex' ? 'launch-codex.sh' : 'launch.sh');

  // The prompt goes to a file, not an argv string: tmux runs its command through a shell, and
  // dictated speech is full of quotes and apostrophes. launch.sh reads and deletes it.
  let promptFile = '';
  if (prompt) {
    promptFile = path.join(os.tmpdir(), `pi-desk-${session}.txt`);
    fs.writeFileSync(promptFile, prompt, { mode: 0o600 });
  }

  // The session's cgroup exists before it starts; launch.sh moves itself in before exec'ing claude, so
  // nothing the session ever runs is charged anywhere else.
  // Fly has no cgroup budgets: the machine is the session's own, and there is no Mac hand-off to make.
  const cg = FLY ? '' : limitSession(session);
  if (!FLY) watchSession(session, onHeavy);
  const args = ['new-session', '-d', '-s', session, '-c', dir, launcher, dir, rcName, promptFile, resumeId || '', session, cg,
                incident ? INCIDENT_LAUNCHER : ''];
  // The first of these spawns creates the tmux SERVER, and the server keeps this environment for
  // its whole life — every later session in it inherits the lot, including ones a person opens by
  // hand. run.sh loaded worker/.env into this process, so without scrubbing here the server carries
  // ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN, and any `claude` started under it that does not
  // go through launch.sh silently bills the API or loses its account linkage. launch.sh strips them
  // for its own sessions; this covers everything else.
  //
  // Fly is the exception for CLAUDE_CODE_OAUTH_TOKEN: it has no interactive login, so the token IS its
  // credential (see launch.sh).
  const env = { ...process.env };
  for (const k of ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', ...(FLY ? [] : ['CLAUDE_CODE_OAUTH_TOKEN'])]) delete env[k];
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
 * simply logged and lost. Not a retry queue — the command is finished, and the user decides later
 * whether the user still wants it.
 */
async function deadLetter(cmd, reason) {
  try {
    await sb('desk_dead_letters', {
      method: 'POST', headers: { Prefer: 'return=minimal' },
      body: JSON.stringify([{ id: randomUUID(), command_id: cmd.id, kind: cmd.kind,
                              payload: cmd.payload || {}, machine: MACHINE,
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
async function bindWhenUp(session, { attempts = 90, delayMs = 1000, agent = 'claude' } = {}) {
  for (let i = 0; i < attempts; i++) {
    const me = (await liveSessions()).find((x) => x.name === session);
    const s = sessionFor(me?.agent || agent, me?.pid);
    if (s) {
      console.log(`[pi-desk] ${session} -> ${agent} session ${s.claudeId}`);
      return sb(`desk_sessions?id=eq.${encodeURIComponent(session)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({ pid: me.pid, claude_session_id: s.claudeId, transcript_path: s.transcriptPath }),
      });
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  console.error(`[pi-desk] no ${agent} came up in ${session} — it will not be resumable`);
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
function resumeArgs(row, { prompt = null, report = false, commandId = null, msg = null } = {}) {
  return { name: row.repo, prompt: prompt || '', resumeId: row.claude_session_id, commandId, report, msg,
           // Only one of this machine's own names can come back as itself; a Mac row's id is not a tmux name.
           reuse: row.worker === WORKER && /^hub-[A-Za-z0-9._-]+$/.test(row.id) ? row.id : null,
           label: row.label || null, origin: row.origin || null, agent: row.agent || 'claude',
           knownClaudeId: row.claude_session_id, transcriptPath: row.transcript_path || null,
           mirrorOffset: row.mirror_offset || 0 };
}

async function startSession({ name, prompt, resumeId = null, commandId = null, report = false, label = null,
                              knownClaudeId = null, transcriptPath = null, mirrorOffset = null, origin = null,
                              reuse = null, msg = null, agent = 'claude' }) {
  // Reroute: another machine may well have what this one lacks, so the command is handed on (runCommand).
  if (agent === 'codex' && !codexReady()) throw new Reroute(`Codex is not signed in on the ${FLY ? 'Fly machine' : 'Pi'}`);
  const incident = isIncident(label);
  if (incident) {
    // Refuse rather than fall back: an incident session started as an ordinary one has every permission.
    if (agent !== 'claude') throw new Error('an incident session runs only as Claude');
    await ensureRepo('health');
    if (!fs.existsSync(INCIDENT_LAUNCHER)) {
      console.error(`[pi-desk] no incident launcher at ${INCIDENT_LAUNCHER}`);
      throw new Reroute(`no incident launcher at ${INCIDENT_LAUNCHER}`);
    }
  }
  if (reuse) {
    const same = (await liveSessions()).find((l) => l.name === reuse);
    // Running already: a second process on one transcript forks the conversation (see resume_session).
    if (same?.alive) throw new Error(`${reuse} is already running`);
    // A husk — tmux still holding the name after claude exited — would make new-session fail.
    if (same) await exec('tmux', ['kill-session', '-t', `=${reuse}`], { timeout: 10000 }).catch(() => {});
  }
  const { dir, warning } = await ensureRepo(name);
  // Without this the session opens onto "Do you trust the files in this folder?" and waits for a
  // click nobody is there to make. Matters most on a fresh clone, which by definition is unseen.
  const trust = ensureTrusted(dir);
  const spoken = String(prompt || '').trim();
  // The directive rides along ONLY when this dispatch asked for a write-up. Most work just needs
  // doing, and a report on a one-line change is noise — so it is opt-in, set by the voice tool
  // when the user actually asks. A bare session with no prompt has nothing to report on either way.
  const full = [warning, spoken, (report && spoken) ? REPORT_DIRECTIVE : null].filter(Boolean).join('\n\n');
  const session = launch(dir, name, full, resumeId, reuse, agent, incident);
  // The sender's message id, stamped on the prompt these words become, exactly as a follow-up typed
  // into a running session is (`typeInto`). Without it the device that sent the words never sees its
  // own id come back, and its placeholder stays under the whole turn.
  if (msg && full) expectPrompt(session, full, msg);

  // Published immediately so the indicator lights up when work starts, not a poll later.
  await sb('desk_sessions?on_conflict=id', {
    method: 'POST', headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
    // command_id is what lets a follow-up find this session: the dispatch that created it is the
    // only durable handle the caller had at the time, since the session did not exist yet.
    // The most recently active session from the moment it exists: it is the work the user just asked for.
    // `ended: null` for a resume under its own name: the row was parked, and is running again.
    body: JSON.stringify([{ id: session, worker: WORKER, repo: name, status: 'resident', command_id: commandId, agent,
                            label: label || null, active_at: new Date().toISOString(), ended: null,
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
  bindWhenUp(session, { agent }).catch((e) => console.error('[pi-desk] session:', e.message));

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
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    // Sent moments after this session was handed to another machine: the words follow it there (moved.js).
    // The answer is the handoff form, so a sender that follows handoffs reports that machine's verdict.
    if (!(await liveSessions()).some((l) => l.name === target && l.alive)) {
      const row = (await sb(`desk_sessions?id=eq.${encodeURIComponent(target)}&select=status,claude_session_id`))?.[0];
      const resume = row?.status === 'transferred' && row.claude_session_id
        ? (await sb(`desk_commands?kind=eq.resume_mirrored&payload->>claude_session_id=eq.${encodeURIComponent(row.claude_session_id)}`
                    + '&select=id,payload&order=created.desc&limit=1'))?.[0]
        : null;
      const fwd = forwardMovedFollowUp(cmd.payload || {}, row, resume, randomUUID());
      if (fwd) {
        if (fwd.payload.target === 'fly' && !FLY) await wakeFly();
        await sb('desk_commands', { method: 'POST', headers: { Prefer: 'return=minimal' }, body: JSON.stringify(fwd) });
        return handedVerdict(fwd.payload.target, fwd.id, `${target} moved there as ${fwd.payload.session}`);
      }
    }
    const prompt = await withAttachments((cmd.payload || {}).prompt, cmd.payload);
    if (!prompt) throw new Error('no prompt to send');
    // The hub passes the sender's own message id as `msg` when it has one; the command id otherwise.
    return typeInto(target, prompt, { cmd: (cmd.payload || {}).msg || cmd.id });
  }

  // Name the session itself, with the command the user would type. Claude Code writes the `custom-title`
  // record, so the name comes back through the transcript rather than living only in our row.
  if (cmd.kind === 'rename_session') {
    const target = String((cmd.payload || {}).session || '').trim();
    const name = String((cmd.payload || {}).name || '').replace(/[\r\n]+/g, ' ').trim().slice(0, 60);
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    if (!name) throw new Error('no name to set');
    // Codex has no rename command a session records, so a Codex session's name lives on its row, where
    // the reconcile tick carries it (`custom_title` outranks the dispatch label as a typed name would).
    const named = (await sb(`desk_sessions?id=eq.${encodeURIComponent(target)}&select=agent`))?.[0];
    if (named?.agent === 'codex') {
      await sb(`desk_sessions?id=eq.${encodeURIComponent(target)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({ label: name, custom_title: name }),
      });
      return `renamed ${target} to ${name}`;
    }
    // The receipt is the `custom-title` record carrying the name, not the command text.
    await typeInto(target, `/rename ${name}`, { expect: name });
    return `renamed ${target} to ${name}`;
  }

  if (cmd.kind === 'end_session') {
    const target = String((cmd.payload || {}).session || '').trim();
    if (!/^hub-[A-Za-z0-9._-]+$/.test(target)) throw new Error(`unusable session name: ${target}`);
    // No wrap-up handshake before closing. Every session already works under a standing rule to
    // commit and push after each logical change, so a session is safe to close at any moment by
    // construction — asking it to wrap up first only delayed freeing the machine by up to ten
    // minutes, and made a capacity problem wait on a model finishing a sentence.
    // PARKED, not deleted. Closing a session frees the machine; it does not throw the work away. The
    // row keeps the transcript id so it can be resumed with full context, and only an explicit
    // "forget" removes it.
    await park(target);
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
    const prompt = await withAttachments((cmd.payload || {}).prompt, cmd.payload) || null;
    const alreadyUp = (await liveSessions()).some((l) => l.name === id && l.alive);
    if (alreadyUp) {
      if (!prompt) return `${id} is already running`;
      return `${id} was already running — ${await typeInto(id, prompt, { cmd: (cmd.payload || {}).msg || cmd.id })}`;
    }
    if (!row.claude_session_id) throw new Error(`${id} has no recorded claude session id — cannot resume`);
    const report = (cmd.payload || {}).report === true;
    // A resume is admitted exactly like new work: on the Mac with its whole conversation when a session
    // is already running here and the Mac is awake (preferMac), else here if memory allows (makeRoom),
    // else on the Mac, else on Fly. On Fly it launches regardless.
    // A Codex conversation is not mirrored, so it cannot move: with no room here, no machine can take it.
    const residents = await residentsNow();
    const why = (row.agent !== 'codex' && await preferMac(residents)) || await makeRoom(residents, id);
    if (why && FLY) console.error(`[pi-desk] resuming ${id} on Fly despite a shortage: ${why}`);
    else if (why) {
      if (row.agent === 'codex') throw new Error(`${id} is a Codex conversation, which cannot move, and the Pi has no room: ${why}`);
      const target = await elsewhere();
      return handedVerdict(target, await handResume(row, target, { prompt, msg: (cmd.payload || {}).msg || cmd.id }), why);
    }
    return startSession(resumeArgs(row, { prompt, report, commandId: cmd.id, msg: (cmd.payload || {}).msg || cmd.id }));
  }

  // SOURCE half of a handover: this machine owns the session and is giving it up.
  //
  // Order matters and is the whole point. Close first, because resuming a transcript that is still
  // live elsewhere does not reattach — it starts a second process replaying the same file, and the
  // two diverge with one id between them. Then flush the mirror, which is only complete once the
  // session has stopped writing. Only then is the destination asked to pick it up.
  if (cmd.kind === 'transfer_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const to = ['mac', 'pi', 'fly'].includes((cmd.payload || {}).to) ? cmd.payload.to : 'pi';
    if (!/^hub-[A-Za-z0-9._-]+$/.test(id)) throw new Error(`unusable session name: ${id}`);
    const rows = await sb(`desk_sessions?id=eq.${encodeURIComponent(id)}&select=*`);
    const row = rows?.[0];
    if (!row) throw new Error(`no such session: ${id}`);
    if (!row.claude_session_id) throw new Error(`${id} has no transcript recorded — nothing to hand over`);
    if (row.agent === 'codex') throw new Error(`${id} is a Codex session, and Codex sessions do not move between machines`);
    // A session that moves ITSELF — because the work needs the Mac (an iOS build, the login keychain) —
    // is always mid-turn: the move is the tool call it is making. Closing it cuts that turn off, so
    // the Mac restarts it with the marker and the moved note, as for heavy work (onHeavy), or it lands
    // there idle and the task stops. Read from the live process before it is closed.
    const pid = (await liveSessions()).find((l) => l.name === id)?.pid;
    const rec = pid ? sessionOf(pid) : null;
    const midTurn = to === 'mac' && rec?.busy === true && !rec.waiting;
    // Woken before the session is closed: a Fly that will not answer leaves it running here.
    if (to === 'fly' && !FLY) await wakeFly();
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
                                        label: row.label, origin: row.origin || 'dispatched', from: MACHINE,
                                        ...(midTurn ? { prompt: MOVED_MARKER, moved: 'it was transferred there while working, so it can use the Mac', mid_turn: true } : {}) } }),
    });
    return `handed ${id} to the ${{ mac: 'Mac', pi: 'Pi', fly: 'Fly machine' }[to]}`;
  }

  // DESTINATION half: assemble the mirrored transcript here and resume it.
  if (cmd.kind === 'resume_mirrored') {
    const claudeId = String((cmd.payload || {}).claude_session_id || '').trim();
    const name = safeName((cmd.payload || {}).repo);
    if (!claudeId) throw new Error('no session id to resume');
    if (!name) throw new Error(`unusable project name: ${JSON.stringify((cmd.payload || {}).repo)}`);
    const { dir } = await ensureRepo(name);
    const { moved, mid_turn: midTurn } = cmd.payload || {};
    // A session the Pi moved mid-turn was closed with C-c, which Claude Code records as the user rejecting
    // the step it was on; mirrorDown rewrites those records so the session carries on here.
    const file = await mirrorDown(claudeId, dir, { movedMidTurn: !!(moved && midTurn) });
    // Nothing mirrored means the session never wrote a whole line. Resuming would open an empty
    // conversation wearing its id, which is worse than not resuming at all.
    if (!file) throw new Error(`nothing has been mirrored for ${claudeId}`);
    // A follow-up rides along when its session's machine was asleep and the sender moved the session
    // here to deliver it (data-layer deliverFollowUp, the hub's session followup).
    const prompt = await withAttachments((cmd.payload || {}).prompt, cmd.payload) || null;
    return startSession({ name, resumeId: claudeId, commandId: cmd.id, prompt,
                          msg: prompt ? ((cmd.payload || {}).msg || cmd.id) : null,
                          // Named from the command, so a follow-up sent to the machine it left can be
                          // forwarded to it before it exists (moved.js).
                          reuse: `hub-${name}-${cmd.id.slice(0, 8)}`,
                          label: (cmd.payload || {}).label || null,
                          origin: (cmd.payload || {}).origin || 'dispatched',
                          knownClaudeId: claudeId, transcriptPath: file,
                          // Everything already uploaded stays uploaded: this machine appends from here.
                          mirrorOffset: fs.statSync(file).size });
  }

  // A machine stopped answering (the ops responder's /live, health/responder): every session it held
  // that can move is taken over HERE, from its last upload. Marking each row transferred is the claim —
  // conditional on it still being resident, so two machines evacuating at once never both resume one,
  // and the machine itself, if it comes back, closes its copy (the reconcile tick's fencing).
  if (cmd.kind === 'evacuate') {
    const machine = String((cmd.payload || {}).machine || '');
    const worker = DESK_WORKER[machine];
    if (!worker) throw new Error(`unknown machine to evacuate: ${JSON.stringify(machine)}`);
    if (worker === WORKER) return `${machine} is this machine, and it is up — nothing moved`;
    const rows = await sb(`desk_sessions?worker=eq.${worker}&status=eq.resident&select=*`);
    const moved = [], stuck = [];
    for (const row of rows || []) {
      if (row.agent === 'codex' || !row.claude_session_id) {
        stuck.push(`${row.id} (${row.agent === 'codex' ? 'a Codex session' : 'no conversation'})`);
        continue;
      }
      if (!(await mirrorHas(row.claude_session_id))) { stuck.push(`${row.id} (nothing mirrored)`); continue; }
      const now = new Date().toISOString();
      const won = await sb(`desk_sessions?id=eq.${encodeURIComponent(row.id)}&status=eq.resident`, {
        method: 'PATCH', headers: { Prefer: 'return=representation' },
        body: JSON.stringify({ status: 'transferred', ended: now, working: false }),
      });
      if (!won?.length) continue;   // it moved or ended while this ran
      try {
        await run({ id: randomUUID(), kind: 'resume_mirrored',
                    payload: mirroredPayload(row, { target: MACHINE, from: machine,
                                                    prompt: row.working ? MOVED_DEAD_MARKER(machine) : null }) });
        moved.push(row.id);
      } catch (e) {
        // Not resumed here, so it is handed back to its row as it was rather than left marked as moved.
        await sb(`desk_sessions?id=eq.${encodeURIComponent(row.id)}&status=eq.transferred`, {
          method: 'PATCH', headers: { Prefer: 'return=minimal' },
          body: JSON.stringify({ status: 'resident', ended: row.ended || null, working: row.working === true }),
        }).catch((e2) => console.error(`[pi-desk] restoring ${row.id} after a failed evacuation:`, e2.message));
        stuck.push(`${row.id} (${e.message})`);
      }
    }
    if (stuck.length) console.error(`[pi-desk] evacuating ${machine}: could not move ${stuck.join(', ')}`);
    return evacuateVerdict(machine, moved, stuck);
  }

  if (cmd.kind !== 'open_session') throw new Error(`unknown kind ${cmd.kind}`);
  const payload = cmd.payload || {};
  const name = safeName(payload.name);
  if (!name) throw new Error(`unusable project name: ${JSON.stringify(payload.name)}`);
  const agent = payload.agent === 'codex' ? 'codex' : 'claude';
  if (agent === 'codex' && !codexReady()) throw new Reroute(`Codex is not signed in on the ${FLY ? 'Fly machine' : 'Pi'}`);

  // Capacity is checked HERE, not inside startSession, so a request that should not run on this Pi —
  // a session is already running here (preferMac) or memory is short (makeRoom) — goes to the Mac when
  // it can take it, else to Fly, rather than being dropped, held, or forced onto a full machine. A
  // resume does the same in its own handler. Fly is the last resort and launches regardless.
  const residents = await residentsNow();
  const why = await preferMac(residents) || await makeRoom(residents, name);
  if (why && FLY) console.error(`[pi-desk] launching on Fly despite a shortage: ${why}`);
  else if (why) return handOpen(payload, why);
  const spoken = await withAttachments(payload.prompt, payload);
  return startSession({ name, prompt: spoken, commandId: cmd.id, report: payload.report === true, label: payload.label || null, agent });
}

/**
 * Run a command, handing it to the next machine when this one cannot (a Reroute: no Codex login here,
 * no incident launcher). Returns the verdict. On Fly there is no next machine, so a Reroute is a failure.
 */
async function runCommand(cmd) {
  try { return await run(cmd); }
  catch (e) {
    if (!(e instanceof Reroute) || FLY) throw e;
    const p = cmd.payload || {};
    console.log(`[pi-desk] ${cmd.id} (${cmd.kind}) cannot run here: ${e.message} — handing it on`);
    if (cmd.kind === 'open_session') return handOpen(p, e.message);
    if (cmd.kind === 'resume_mirrored') {
      const target = await elsewhere(p.agent === 'codex' ? 'codex' : 'claude');
      return handedVerdict(target, await sendCommand('resume_mirrored', { ...p, target }), e.message);
    }
    if (cmd.kind === 'resume_session') {
      const row = (await sb(`desk_sessions?id=eq.${encodeURIComponent(String(p.session || ''))}&select=*`))?.[0];
      if (!row?.claude_session_id || row.agent === 'codex') throw e;   // a Codex conversation cannot move
      const target = await elsewhere();
      return handedVerdict(target, await handResume(row, target, { prompt: p.prompt || null, msg: p.msg || cmd.id }), e.message);
    }
    throw e;
  }
}

/**
 * A command this machine claimed and cannot run in time — it expired before this machine saw it, or
 * this machine claimed it and stopped before finishing. It goes to Fly (route.js flyCommandFor): a
 * resume or a follow-up moves its conversation there from the mirror, words included. Dead-lettered only
 * when it cannot move (a Codex session, a kind that does not move) or Fly will not wake. Every case is a
 * console.error: work that reaches Fly this way means the machine it was meant for did not take it.
 */
async function sendToFly(cmd, why, status) {
  const p = cmd.payload || {};
  const row = ['resume_session', 'follow_up'].includes(cmd.kind)
    ? (await sb(`desk_sessions?id=eq.${encodeURIComponent(String(p.session || ''))}&select=*`).catch(() => null))?.[0]
    : null;
  const fly = flyCommandFor(cmd, row, MACHINE);
  if (!fly) {
    console.error(`[pi-desk] ${cmd.id} (${cmd.kind}) ${why}, and it cannot move to Fly — dead-lettered`);
    await finish(cmd.id, status, why);
    return deadLetter(cmd, why);
  }
  try {
    if (row && fly.kind === 'resume_mirrored') {
      // The conversation moves: the row is marked first, so the machine it was on closes its copy.
      if (row.worker === WORKER && row.transcript_path) {
        await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0).catch(() => null);
      }
      await sb(`desk_sessions?id=eq.${encodeURIComponent(row.id)}`, {
        method: 'PATCH', headers: { Prefer: 'return=minimal' },
        body: JSON.stringify({ status: 'transferred', ended: new Date().toISOString(), working: false }),
      });
    }
    const id = await sendCommand(fly.kind, fly.payload);
    await finish(cmd.id, 'done', handedVerdict('fly', id, why));
    console.error(`[pi-desk] ${cmd.id} (${cmd.kind}) ${why} — sent to Fly as ${id}`);
  } catch (e) {
    console.error(`[pi-desk] ${cmd.id} (${cmd.kind}) ${why}, and sending it to Fly failed (${e.message}) — dead-lettered`);
    await finish(cmd.id, status, `${why}; Fly: ${e.message}`);
    await deadLetter(cmd, `${why}; sending it to Fly failed: ${e.message}`);
  }
}

/** Whether a tmux session by this name is running here with its agent in it. */
const aliveHere = async (name) => (await liveSessions()).some((l) => l.name === name && l.alive);

// Ids the desk_commands channel has delivered, with when. A pending command this machine owns that was
// inserted while the channel was SUBSCRIBED, and that the channel never delivered, means it is deaf
// while still reporting SUBSCRIBED: supabase-js raises nothing for that, and the backstop that found
// the row is too slow for COMMAND_TTL_S, so the command expires. Report it and rejoin the channel.
let rt = null;
const heard = new Map();
function onCommandEvent(payload) {
  const id = payload?.new?.id;
  if (id) heard.set(id, Date.now());
  for (const [k, at] of heard) if (Date.now() - at > 3600000) heard.delete(k);
  return drain();
}
function checkHeard(cmd) {
  if (!rt || !missedByChannel(cmd, heard, rt.subscribedAt())) return;
  heard.set(cmd.id, Date.now());   // one report per row
  const ageS = Math.round((Date.now() - new Date(cmd.created).getTime()) / 1000);
  console.error(`[pi-desk] desk_commands channel deaf while SUBSCRIBED: ${cmd.id} (${cmd.kind}) inserted ${ageS}s ago was never delivered; rejoining`);
  rt.rejoin();
}

let draining = false;
// A command that lands while a drain is running would not be in the rows that drain already read, and its
// realtime event would be dropped by the guard below; without this it waits for the backstop poll.
let drainAgain = false;
async function drain() {
  if (draining) { drainAgain = true; return; }
  draining = true;
  drainAgain = false;
  try {
    const rows = await sb('desk_commands?status=eq.pending&select=*&order=created.asc');
    if (!rows?.length) return;
    const macAwake = FLY || await macIsAwake();

    for (const cmd of rows) {
      const target = (cmd.payload || {}).target;
      // Fly takes only what is addressed to it: it is the last resort, chosen by the dispatcher.
      const mine = FLY ? target === 'fly' : (target === 'pi' || (!target && !macAwake));
      if (!mine) continue;

      const ageS = (Date.now() - new Date(cmd.created).getTime()) / 1000;
      checkHeard(cmd);
      // Late. Fly runs whatever reaches it; elsewhere it goes to Fly — except a follow-up for a session
      // running here, which this machine can deliver now.
      const late = !FLY && ageS > COMMAND_TTL_S
        && !(cmd.kind === 'follow_up' && await aliveHere(String((cmd.payload || {}).session || '')));
      if (late) {
        if (await claim(cmd.id)) await sendToFly(cmd, `expired unrun — queued ${Math.round(ageS)}s before this machine saw it`, 'expired');
        continue;
      }
      if (!(await claim(cmd.id))) continue;

      try {
        const detail = await runCommand(cmd);
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
    if (drainAgain) drain().catch((e) => console.error(`[desk] drain re-run failed: ${e.message || e}`));
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
    // The command log. Dead letters are NOT swept: an unretried one holds words the user may still want,
    // and a retried or dismissed one is a decision the user made, which is small and worth keeping.
    await sb(`desk_commands?created=lt.${ago(KEEP_COMMAND_DAYS)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } });
    await sb(`voice_requests?created_at=lt.${ago(KEEP_REQUEST_DAYS)}`, { method: 'DELETE', headers: { Prefer: 'return=minimal' } });
    await sweepClips();
    // Messages handed to sessions by file, and the pictures fetched for them (handoff.js).
    sweepStashedPrompts();
    // The net under the tick's sweep, for a conversation that ended inside a process that kept
    // running (a `/clear`, or the voice worker's long-lived claude moving on to a new one).
    sweepScratch();
  } catch (e) { console.error('[pi-desk] retention:', e.message); }
}

/**
 * Commands this machine claimed and never finished.
 *
 * A crash or a restart between claim and finish leaves a row 'running' for ever: no consumer will
 * look at it again, and nothing anywhere says it did not happen. Only the claimer can judge this —
 * hence claimed_by — and only well after the longest legitimate launch (a cold clone) could still
 * be in flight. What it finds goes to Fly (sendToFly) rather than being dropped.
 */
const CLAIM_STALE_S = Number(process.env.CLAIM_STALE_S || 900);
async function sweepAbandonedClaims() {
  try {
    const cutoff = new Date(Date.now() - CLAIM_STALE_S * 1000).toISOString();
    const rows = await sb(`desk_commands?status=eq.running&claimed_by=eq.${WORKER}&updated=lt.${cutoff}&select=*`);
    for (const cmd of rows || []) {
      await sendToFly(cmd, `abandoned — claimed by ${WORKER} and never finished`, 'error');
    }
  } catch (e) { console.error('[pi-desk] sweep claims:', e.message); }
}

// ---- Fly: asleep unless there is work --------------------------------------
// The Fly machine is stopped almost always. The dispatcher wakes it with a request to /wake — Fly's proxy
// starts a stopped machine for any request to it, and answers once this server is listening — and then
// writes the command, which the drain below finds on startup or over realtime.
//
// It stops itself: once nothing has run here for FLY_IDLE_EXIT_S, the process exits cleanly, and a
// machine whose process exits 0 stays stopped (fly.toml's restart policy is on-failure). The check rides
// the reconcile tick that already runs; idle sessions are parked first, by the same reaper as the Pi's.
const FLY_IDLE_EXIT_S = Number(process.env.FLY_IDLE_EXIT_S || 600);
let idleSince = Date.now();
function exitIfIdle(residentCount) {
  if (!FLY) return;
  if (residentCount > 0 || draining || incidentRuns > 0) { idleSince = Date.now(); return; }
  if (Date.now() - idleSince < FLY_IDLE_EXIT_S * 1000) return;
  console.log(`[pi-desk] nothing has run for ${Math.round(FLY_IDLE_EXIT_S / 60)}m — stopping the machine`);
  process.exit(0);
}
// The ops responder's way in when Supabase is down (health/responder/README.md). Every other route to a
// session here reads or writes Supabase, so this one touches neither: the responder wakes the machine,
// posts the incident prompt, and it runs as `claude -p` in the health checkout, through that repo's
// incident launcher. Nothing about it
// appears in the session list, which is itself in Supabase. When it exits the run is reported done, so
// whatever it left open is handed to the user.
let incidentRuns = 0;
const OPS_URL = process.env.OPS_URL || 'https://ops-responder.fly.dev';
async function incidentRun(run, prompt) {
  const dir = path.join(REPO_ROOT, 'health');
  try {
    if (fs.existsSync(path.join(dir, '.git'))) execFileSync('git', ['-C', dir, 'pull', '-q', '--ff-only'], { timeout: 60e3 });
    else execFileSync('gh', ['repo', 'clone', 'JJGantt/health', dir], { timeout: 120e3 });
  } catch (e) { console.error(`[pi-desk] incident ${run}: health checkout: ${e.message}`); }
  incidentRuns += 1;
  const finish = async (why) => {
    incidentRuns -= 1;
    idleSince = Date.now();
    try {
      await fetch(`${OPS_URL}/runs/${run}/done`, { method: 'POST', signal: AbortSignal.timeout(15e3),
        headers: { Authorization: `Bearer ${process.env.OPS_TOKEN || ''}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: why }) });
    } catch (e) { console.error(`[pi-desk] incident ${run}: could not report done: ${e.message}`); }
  };
  // The launcher, never plain claude: health/responder/README.md § The incident agent's permissions.
  // Without it the run is refused.
  const launcher = path.join(dir, 'responder', 'bin', 'incident-claude');
  if (!fs.existsSync(launcher)) {
    console.error(`[pi-desk] incident ${run}: no incident launcher at ${launcher}`);
    return finish(`fly-desk has no incident launcher (${launcher})`);
  }
  const log = fs.openSync(path.join(process.env.HOME || '/tmp', `incident-${run}.log`), 'a');
  const child = spawn(launcher, ['-p', prompt, '--model', 'opus', '--output-format', 'text'], {
    cwd: dir, stdio: ['ignore', log, log],
    env: { ...process.env, OPS_URL, OPS_TOKEN: process.env.OPS_TOKEN || '' },
  });
  child.on('error', (e) => { console.error(`[pi-desk] incident ${run}: claude did not start: ${e.message}`); finish(`claude did not start on fly-desk: ${e.message}`); });
  child.on('exit', (code) => { console.log(`[pi-desk] incident ${run}: claude exited ${code}`); finish(`fly-desk claude exited ${code}`); });
}

if (FLY) {
  const http = await import('node:http');
  http.createServer((req, res) => {
    idleSince = Date.now();
    if (req.method === 'POST' && req.url === '/incident') {
      const token = process.env.OPS_TOKEN || '';
      if (!token || req.headers.authorization !== `Bearer ${token}`) { res.writeHead(401); return res.end(); }
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        try {
          const { run, prompt } = JSON.parse(body);
          if (!Number.isInteger(run) || !prompt) throw new Error('needs run and prompt');
          incidentRun(run, String(prompt));
          res.writeHead(202); res.end('started\n');
        } catch (e) { res.writeHead(400); res.end(`${e.message}\n`); }
      });
      return;
    }
    if (req.url === '/wake') drain().catch((e) => console.error('[pi-desk] drain after wake:', e.message));
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('awake\n');
  }).listen(Number(process.env.PORT || 8080), '0.0.0.0');
}

console.log(`[pi-desk] ${WORKER} watching desk_commands (repos in ${REPO_ROOT}, owner ${GITHUB_OWNER})`);
if (!FLY) {
  const live = await liveSessions();
  await setupCgroups(live);
  for (const l of live) if (l.alive) watchSession(l.name, onHeavy);
}
heartbeat();
setInterval(heartbeat, PRESENCE_MS);
// The 5s liveness beat the responder reads to move this Pi's sessions when it stops answering (live.js).
// Not from Fly: it sleeps whenever nothing runs, and its silence is its normal state.
if (!FLY) startLive('pi');
// Realtime is the primary path: a command inserted anywhere reaches this within a moment, rather
// than waiting out a poll interval. Both INSERT (a new command) and UPDATE (a queued row released)
// matter, so the subscription is not narrowed to inserts.
rt = watchTable({ channel: 'pi-desk-commands', table: 'desk_commands', event: '*', onChange: onCommandEvent, tag: 'pi-desk' });
drain();
drainQueue();
// The backstop stretches out while realtime is healthy and tightens when it is not, so a subscription
// that dies silently degrades to slow-but-working instead of stopping altogether.
let pollTimer = null;
const pollLoop = () => {
  drain().catch((e) => console.error('[pi-desk] drain:', e.message));
  pollTimer = setTimeout(pollLoop, rt.isHealthy && rt.isHealthy() ? POLL_MS : POLL_DEGRADED_MS);
};
pollTimer = setTimeout(pollLoop, POLL_DEGRADED_MS);

// Every caller goes through here: a bare call let one Supabase connect timeout become an unhandled
// rejection that exited the process. The next tick re-reads everything, so one failure warns and the
// second in a row is an error. Single blips arrive about daily; a minute-long outage is rarer and real.
const reconcileStreak = streak('[pi-desk] reconcile', { failures: 2 });
function reconcile() {
  reconcileSessions().then(() => reconcileStreak.ok(), (e) => reconcileStreak.fail(e));
}
reconcile();
setInterval(() => { if (!FLY) ops.beat(Math.max(60, PRESENCE_MS / 1000)); reconcile(); }, PRESENCE_MS);
sweepAbandonedClaims();
setInterval(sweepAbandonedClaims, 300000);
// One retention rule in one place: the Pi, which is always on. Fly is asleep almost always.
if (!FLY) {
  sweepRetention();
  setInterval(sweepRetention, 3600000);   // hourly is far more often than a daily boundary needs
}
