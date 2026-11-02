// Desk command queue consumer.
//
// The hub (on Fly) can't reach this Mac, so it enqueues into `desk_commands` and this drains the
// queue: realtime fires on INSERT for an immediate launch, and a slow poll catches anything the
// subscription missed. Claiming is a compare-and-swap on status, so a duplicate event can't open
// the same terminal twice.
//
// Two guards, because this ends in "run a shell command on the desk":
//   - the target path must be a directory THIS process published (repos.js owns that list) — a bad
//     or stale queue row can't `cd` anywhere else on disk;
//   - commands older than COMMAND_TTL_S expire unrun, so a Mac that was asleep doesn't wake up and
//     open every terminal that was queued in the meantime.
//
// Launching also pre-accepts the workspace-trust prompt for the target (see trust.js), since a
// session stuck on that modal is indistinguishable from one that never started.
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { config } from './config.js';

const exec = promisify(execFile);
import { pendingCommands, claimCommand, finishCommand, upsertDeskSession, deskSession,
         patchDeskSession, deadLetterCommand, abandonedClaims, insertCommand } from './supabase.js';
import { awaitSessionOn, sessionOf, awaitNotWaiting } from './discover.js';
import { awaitCodexOn, codexSessionOf, codexReady } from './codex.js';

/** The conversation a row's process is in, read the way its agent records it. */
const sessionFor = (row) => (!row?.pid ? null : row.agent === 'codex' ? codexSessionOf(row.pid) : sessionOf(row.pid));
import { beginLaunch, endLaunch } from './deskSessions.js';
import { watchTable } from './realtime.js';
import { isKnownRepo } from './repos.js';

// What a session the Pi closed for doing heavy work is told when it resumes here: what happened, then
// one of two endings, for a session cut off mid-turn and for one that was waiting on the user.
const [MOVED_NOTE, MOVED_MID_TURN, MOVED_BETWEEN_TURNS] = fs.readFileSync(new URL('../moved-session.txt', import.meta.url), 'utf8')
  .split(/^---$/m).map((s) => s.trim());
import { ensureTrusted } from './trust.js';
import { openFreshTab, openCodexTab, sendToTty, closeTabForTty } from './applescript.js';
import { isIncident } from './incident.js';
import { withAttachments, transcriptSize, awaitSubmitted } from './handoff.js';
import { expectPrompt } from './sessionWatch.js';
import { mirrorUp, mirrorDown } from './mirror.js';
import { fingerprint, noteRemoteInput } from './idle.js';

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

let timer = null;
let draining = false;

/**
 * Bring a checkout up to date before a session opens in it.
 *
 * Non-fatal by design: a checkout that cannot fast-forward still opens, and the warning rides along
 * in the prompt so the session knows rather than silently building on a stale base.
 */
async function pull(dir) {
  try {
    await exec('git', ['-C', dir, 'fetch', '--all', '--prune'], { timeout: 120000 });
    await exec('git', ['-C', dir, 'pull', '--ff-only'], { timeout: 120000 });
    return null;
  } catch (e) {
    const msg = String(e.stderr || e.message || '').split('\n')[0].slice(0, 200);
    console.warn(`[commands] pull failed in ${dir}: ${msg}`);
    return `NOTE: this checkout could not be fast-forwarded (${msg}). Check git status and reconcile with origin before making changes.`;
  }
}

/** Where a repo lives on this machine. Never taken from a payload — only ever built and checked. */
function repoDir(name) {
  if (!name || name === 'root') return os.homedir();
  const dir = path.join(os.homedir(), 'repos', name);
  if (!isKnownRepo(dir)) throw new Error(`not a known project: ${name}`);
  return dir;
}

/**
 * Type `text` into a running session and confirm it was submitted.
 *
 * "Sent" is reported only once the session's transcript holds the prompt (handoff.js): typing itself
 * reports nothing, and text can sit in the composer unsent while everything looks like it worked. A
 * session blocked on a permission prompt is never typed into, since the keys would answer the dialog:
 * the words are held until the dialog is gone (answered, or timed out by Claude Code after two
 * minutes) and delivered then. Only a dialog still up after DIALOG_WAIT_MS fails the command. `expect` is what the transcript will hold when that differs from the text typed. `cmd`
 * is the id the watcher stamps on the prompt these words become (sessionWatch.js `expectPrompt`), so
 * the device that sent them can recognise its own message coming back by id rather than by text.
 */
// Claude Code times its own dialogs out at two minutes; this is only the bound past that.
const DIALOG_WAIT_MS = 5 * 60 * 1000;

async function deliver(row, text, { expect = null, cmd = null } = {}) {
  if (!row) throw new Error('no such session');
  if (!row.tty) throw new Error(`no terminal recorded for ${row.id}`);
  let s = sessionFor(row);
  if (s?.waiting) {
    console.log(`[commands] ${row.id} is on a ${s.waiting}; holding the follow-up until it clears`);
    try { s = await awaitNotWaiting(row.pid, DIALOG_WAIT_MS); }
    catch (e) { throw new Error(`${row.id} ${e.message} — not typing into it`); }
    if (!s) throw new Error(`${row.id} ended while a follow-up waited on its dialog`);
  }
  const tpath = s?.transcriptPath || row.transcript_path || null;
  const from = tpath ? transcriptSize(tpath) : 0;
  if (cmd) expectPrompt(row.id, text, cmd);
  // Before sending, so the idle reaper never reads these words in the transcript as typed at the Mac.
  await noteRemoteInput(row, text).catch((e) => console.error('[commands] note remote input:', e.message));
  const line = await sendToTty(row.tty, text);
  if (!tpath) return `sent ${text.length} chars to ${row.tty} (unconfirmed: no transcript bound yet)`;
  if (!(await awaitSubmitted(tpath, from, expect || line))) {
    throw new Error(`typed into ${row.tty} but it never reached the transcript — likely sitting unsent in the composer`);
  }
  return `sent ${text.length} chars to ${row.tty}`;
}

async function run(cmd) {
  // Type a follow-up into a session this machine already has open, addressed by its tab's tty.
  if (cmd.kind === 'follow_up') {
    const session = String((cmd.payload || {}).session || '').trim();
    const prompt = await withAttachments((cmd.payload || {}).prompt, cmd.payload);
    if (!prompt) throw new Error('no prompt to send');
    const row = await deskSession(session);
    // A session the reconciler has already found dead has no tab to type into. Fail here rather than
    // letting AppleScript hunt for a tty that a later tab may have been given.
    if (row && row.status !== 'resident') throw new Error(`${session} has ended`);
    // The hub passes the sender's own message id as `msg` when it has one; the command id otherwise.
    return deliver(row, prompt, { cmd: (cmd.payload || {}).msg || cmd.id });
  }

  // Name the session itself. `/rename` is the command the user types, so this is the same act rather than
  // a parallel label: Claude Code renames itself, the tab title changes, and it writes the
  // `custom-title` record the reconcile tick reads straight back into the row.
  if (cmd.kind === 'rename_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const name = String((cmd.payload || {}).name || '').replace(/[\r\n]+/g, ' ').trim().slice(0, 60);
    if (!name) throw new Error('no name to set');
    const row = await deskSession(id);
    if (!row) throw new Error(`no such session: ${id}`);
    if (row.status !== 'resident') throw new Error(`${id} is not running`);
    // Codex has no rename a session records, so a Codex session's name lives on its row (`custom_title`,
    // which the tick keeps, as it keeps a name typed with /rename).
    if (row.agent === 'codex') {
      await patchDeskSession(id, { label: name, custom_title: name });
      return `renamed ${id} to ${name}`;
    }
    // The receipt is the `custom-title` record carrying the name, not the command text.
    await deliver(row, `/rename ${name}`, { expect: name });
    return `renamed ${id} to ${name}`;
  }

  // Close a session here — parked, not destroyed, whenever there is a transcript to come back to.
  // Same meaning as the Pi's eviction: the tab goes, the conversation does not.
  if (cmd.kind === 'end_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const row = await deskSession(id);
    if (!row) throw new Error(`no such session: ${id}`);
    if (row.tty) await closeTabForTty(row.tty);
    const status = row.claude_session_id ? 'evicted' : 'ended';
    await patchDeskSession(id, { status, ended: new Date().toISOString(), working: false });
    return `${status === 'evicted' ? 'parked' : 'closed'} ${id}`;
  }

  // Bring a parked session back, in a new tab, with its whole conversation. `claude --resume` replays
  // the transcript, which is why the id is recorded the moment a session is launched or adopted.
  if (cmd.kind === 'resume_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const row = await deskSession(id);
    if (!row) throw new Error(`no such session: ${id}`);
    // Already open: resuming it is just talking to it, the same as the Pi does. Refusing here threw
    // away every follow-up that named a running session.
    const prompt = await withAttachments((cmd.payload || {}).prompt, cmd.payload) || null;
    if (row.status === 'resident') {
      if (!prompt) return `${id} was already running`;
      return `${id} was already running — ${await deliver(row, prompt, { cmd: (cmd.payload || {}).msg || cmd.id })}`;
    }
    if (!row.claude_session_id) throw new Error(`${id} has no transcript recorded — cannot resume`);
    const dir = repoDir(row.repo);
    ensureTrusted(dir);
    if (row.repo !== 'root') await pull(dir);
    const sessionId = `mac-${cmd.id.slice(0, 8)}`;
    // The sender's message id, stamped on the prompt these words become, as `deliver` does for a
    // running session — without it the sender's placeholder stays under the whole turn.
    if (prompt) expectPrompt(sessionId, prompt, (cmd.payload || {}).msg || cmd.id);
    if (row.agent === 'codex' && !codexReady()) throw new Error('Codex is not signed in on the Mac');
    if (row.agent === 'codex' && isIncident(row.label)) throw new Error('an incident session runs only as Claude');
    beginLaunch();   // until the row exists — see open_session below
    try {
      const raw = row.agent === 'codex'
        ? await openCodexTab(dir, row.repo, prompt, sessionId, row.claude_session_id)
        : await openFreshTab(dir, row.repo, prompt, sessionId, row.claude_session_id, null, row.label);
      const [how, tty] = String(raw).split('\t');
      // A NEW row for the new tab, exactly as the Pi makes a new tmux session on resume. The old row
      // stays parked until retention drops it; it is where that stretch of the conversation happened.
      await upsertDeskSession({
        id: sessionId, worker: 'desk', repo: row.repo, status: 'resident', agent: row.agent || 'claude',
        origin: row.origin || 'dispatched', command_id: cmd.id, tty: tty || null,
        label: row.label || null, claude_session_id: row.claude_session_id,
        transcript_path: row.transcript_path, mirror_offset: row.mirror_offset || 0,
        remote_inputs: prompt ? [fingerprint(prompt)] : [],
        // A session the user used at the Mac stays the user's through a resume: the new row's transcript scan
        // starts at its own start, so it would otherwise read as hosted-only and be parked again.
        touched_at: row.touched_at || null,
        active_at: new Date().toISOString(),
      });
      return `resumed ${id} here (${how})`;
    } finally { endLaunch(); }
  }

  // SOURCE half of a handover: this machine owns the session and is giving it up.
  //
  // Order matters and is the whole point. Close first, because resuming a transcript that is still
  // live elsewhere does not reattach — it starts a second process replaying the same file, and the
  // two diverge with one id between them. Then flush the mirror, which is only complete once the
  // session has stopped writing. Only then is the destination asked to pick it up.
  if (cmd.kind === 'transfer_session') {
    const id = String((cmd.payload || {}).session || '').trim();
    const to = (cmd.payload || {}).to === 'pi' ? 'pi' : 'mac';
    const row = await deskSession(id);
    if (!row) throw new Error(`no such session: ${id}`);
    if (!row.claude_session_id) throw new Error(`${id} has no transcript recorded — nothing to hand over`);
    if (row.agent === 'codex') throw new Error(`${id} is a Codex session, and Codex sessions do not move between machines`);
    if (row.tty) await closeTabForTty(row.tty);
    await new Promise((r) => setTimeout(r, 2000));   // let claude exit and flush its last records
    const at = await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0);
    const now = new Date().toISOString();
    await patchDeskSession(id, { status: 'transferred', ended: now, working: false,
                                 ...(at != null ? { mirror_offset: at, mirror_at: now } : {}) });
    await insertCommand('resume_mirrored', {
      target: to, claude_session_id: row.claude_session_id, repo: row.repo,
      label: row.label, origin: row.origin || 'dispatched', from: 'mac',
    });
    return `handed ${id} to the ${to === 'pi' ? 'Pi' : 'Mac'}`;
  }

  // DESTINATION half: assemble the mirrored transcript here and resume it.
  if (cmd.kind === 'resume_mirrored') {
    const claudeId = String((cmd.payload || {}).claude_session_id || '').trim();
    const repo = String((cmd.payload || {}).repo || 'root').trim();
    if (!claudeId) throw new Error('no session id to resume');
    const dir = repoDir(repo);
    const { moved, mid_turn: midTurn } = cmd.payload || {};
    // A session the Pi moved mid-turn was closed with C-c, which Claude Code records as the user rejecting
    // the step it was on; mirrorDown rewrites those records so the session carries on here.
    const file = await mirrorDown(claudeId, dir, { movedMidTurn: !!(moved && midTurn) });
    // Nothing mirrored means the session never sat idle long enough to be uploaded. Resuming would
    // open an empty conversation wearing its id, which is worse than not resuming at all.
    if (!file) throw new Error(`nothing has been mirrored for ${claudeId}`);
    ensureTrusted(dir);
    if (repo !== 'root') await pull(dir);
    const sessionId = `mac-${cmd.id.slice(0, 8)}`;
    beginLaunch();   // until the row exists — see open_session below
    try {
      // A prompt rides along when the Pi hands over a resume it had no room for (pi-desk handResumeToMac),
      // and `moved` when it closed a session mid-turn for doing heavy work there (pi-desk onHeavy).
      const note = moved
        ? `${MOVED_NOTE.replace('{why}', String(moved))}\n\n${midTurn ? MOVED_MID_TURN : MOVED_BETWEEN_TURNS}` : null;
      const raw = await openFreshTab(dir, repo, (cmd.payload || {}).prompt || null, sessionId, claudeId, note,
                                     (cmd.payload || {}).label || null);
      const [how, tty] = String(raw).split('\t');
      await upsertDeskSession({
        id: sessionId, worker: 'desk', repo, status: 'resident',
        origin: (cmd.payload || {}).origin || 'dispatched', command_id: cmd.id, tty: tty || null,
        label: (cmd.payload || {}).label || null,
        claude_session_id: claudeId, transcript_path: file,
        // Everything already uploaded stays uploaded: this machine carries on appending from here.
        mirror_offset: fs.statSync(file).size,
        remote_inputs: (cmd.payload || {}).prompt ? [fingerprint((cmd.payload || {}).prompt)] : [],
        active_at: new Date().toISOString(),
      });
      return `resumed ${claudeId.slice(0, 8)} here (${how})`;
    } finally { endLaunch(); }
  }

  if (cmd.kind !== 'open_session') throw new Error(`unknown kind ${cmd.kind}`);
  const rawPath = String((cmd.payload || {}).path || '');
  const rawName = String((cmd.payload || {}).name || '').trim();
  if (!rawPath) throw new Error('no path in payload');

  // ROOT: a request that named no project, or one nothing matched. It opens in the home directory,
  // from which every repo is reachable, rather than being refused — a dispatched request that lands
  // somewhere useful beats one that does not land at all.
  //
  // It bypasses isKnownRepo deliberately, and safely: the home directory is not attacker-supplied,
  // it is this branch's own constant. The check exists so a crafted queue row cannot `cd` anywhere
  // on disk, and a fixed path chosen here cannot.
  const isRoot = rawName === 'root' || rawPath === '~';
  const target = isRoot ? os.homedir() : rawPath;
  if (!isRoot && !isKnownRepo(target)) throw new Error(`not a known project: ${target}`);
  const name = isRoot ? 'root' : (rawName || null);
  // Otherwise the session opens straight onto "Do you trust the files in this folder?" and waits
  // for a click nobody is there to make — the launch looks like it worked and is actually stuck.
  const trust = ensureTrusted(target);
  // Pull before opening. Another machine, or a GitHub Action, has very likely pushed since this
  // checkout was last touched, and a session that starts on a stale tree writes against the wrong
  // base. Non-fatal: a checkout that cannot fast-forward (local changes, diverged) still opens, and
  // the warning rides along in the prompt so the session knows rather than silently building on it.
  const staleWarning = isRoot ? null : await pull(target);
  const spoken = await withAttachments((cmd.payload || {}).prompt, cmd.payload);
  const wantsReport = (cmd.payload || {}).report === true;
  const prompt = [staleWarning, spoken, (wantsReport && spoken) ? REPORT_DIRECTIVE : null].filter(Boolean).join('\n\n') || null;
  const startedAt = Date.now() - 1000;   // slack: ps reports a start time to the whole second
  const sessionId = `mac-${cmd.id.slice(0, 8)}`;
  const agent = (cmd.payload || {}).agent === 'codex' ? 'codex' : 'claude';
  if (agent === 'codex' && !codexReady()) throw new Error('Codex is not signed in on the Mac');
  if (agent === 'codex' && isIncident((cmd.payload || {}).label)) throw new Error('an incident session runs only as Claude');
  // Held from before the tab opens until its row exists. In between, the new claude looks exactly
  // like a session nobody dispatched, and adopting it would give one session two rows.
  beginLaunch();
  let how, tty;
  try {
    const raw = agent === 'codex'
      ? await openCodexTab(target, name, prompt, sessionId)
      : await openFreshTab(target, name, prompt, sessionId, null, null, (cmd.payload || {}).label || null);
    // openFreshTab reports "<how>\t<tty>"; the tty is the handle a later follow-up addresses.
    [how, tty] = String(raw).split('\t');
    console.log(`[commands] opened session in ${target} (${how}${tty ? `, tty ${tty}` : ''}, trust: ${trust}`
      + `${prompt ? `, prompt: "${prompt.slice(0, 60)}"` : ''})`);

    // Publish it, as the most recently active session: it is the work the user just asked for.
    try {
      await upsertDeskSession({
        id: sessionId, worker: 'desk', repo: name || 'root', agent,
        status: 'resident', origin: 'dispatched', command_id: cmd.id, tty: tty || null,
        label: (cmd.payload || {}).label || null,
        prompt: prompt ? prompt.slice(0, 500) : null,
        remote_inputs: prompt ? [fingerprint(prompt)] : [],
        active_at: new Date().toISOString(),
      });
    } catch (e) { console.error('[commands] publish:', e.message); }
  } finally { endLaunch(); }

  // Which conversation it is, read from the process's own record once claude is up (discover.js).
  // Async on purpose: the launch has already happened and nothing should wait on it coming up. The
  // reconcile tick binds the same way, so a miss here costs one tick, not the session.
  if (tty) {
    (agent === 'codex' ? awaitCodexOn(tty, startedAt) : awaitSessionOn(tty, startedAt)).then((s) => {
      if (!s) return console.error(`[commands] no ${agent} came up on ${tty} for ${sessionId}`);
      console.log(`[commands] ${sessionId} -> ${agent} session ${s.claudeId}`);
      return patchDeskSession(sessionId, { pid: s.pid, claude_session_id: s.claudeId, transcript_path: s.transcriptPath });
    }).catch((e) => console.error('[commands] session:', e.message));
  }

  return how;
}

const followUpChains = new Map();

async function execute(cmd) {
  try {
    await finishCommand(cmd.id, 'done', await run(cmd));
  } catch (e) {
    console.error('[commands] failed:', e.message);
    await finishCommand(cmd.id, 'error', e.message).catch((err) => console.error('[commands] finish:', err.message));
    await deadLetterCommand(cmd, e.message).catch((err) => console.error('[commands] dead letter:', err.message));
  }
}

async function drain() {
  if (draining) return;
  draining = true;
  try {
    for (const cmd of await pendingCommands()) {
      const ageS = (Date.now() - new Date(cmd.created).getTime()) / 1000;
      if (ageS > config.COMMAND_TTL_S) {
        if (await claimCommand(cmd.id)) {
          await finishCommand(cmd.id, 'expired', `queued ${Math.round(ageS)}s ago`);
          // Expiring is right — a laptop that was asleep must not wake up and open every terminal
          // queued in the meantime — but the words are not disposable. They are kept for a manual
          // retry rather than dropped along with the command.
          await deadLetterCommand(cmd, `expired unrun — queued ${Math.round(ageS)}s before this machine saw it`)
            .catch((e) => console.error('[commands] dead letter:', e.message));
          console.log(`[commands] expired ${cmd.kind} (${Math.round(ageS)}s old)`);
        }
        continue;
      }
      // Not ours: another machine is meant to run this. Without this check the Mac claims every
      // pending row the instant it appears — including ones the hub explicitly routed to the Pi —
      // and the Pi's consumer never sees them. Absent target means "whoever is here", which is the
      // Mac while it is awake.
      const target = (cmd.payload || {}).target;
      if (target && target !== 'mac') continue;
      if (!(await claimCommand(cmd.id))) continue;   // someone else took it
      if (cmd.kind === 'follow_up') {
        // Not awaited: a follow-up can be held for minutes on a dialog (deliver), and that must not
        // stop other commands. Chained per session, so two follow-ups to one session keep their order.
        const key = String((cmd.payload || {}).session || '');
        const prev = followUpChains.get(key) || Promise.resolve();
        const next = prev.then(() => execute(cmd));
        followUpChains.set(key, next);
        next.finally(() => { if (followUpChains.get(key) === next) followUpChains.delete(key); });
        continue;
      }
      await execute(cmd);
    }
  } catch (e) {
    console.error('[commands] drain error:', e.message);
  } finally {
    draining = false;
  }
}

/**
 * Commands this machine claimed and never finished.
 *
 * A crash, a restart, or a sleep between claim and finish leaves a row 'running' for ever: no
 * consumer will look at it again, and nothing anywhere says it did not happen. Only the claimer can
 * judge this — hence claimed_by — and only well after the longest legitimate launch could still be
 * in flight.
 */
const CLAIM_STALE_S = parseInt(process.env.CLAIM_STALE_S || '900', 10);
async function sweepAbandonedClaims() {
  try {
    const cutoff = new Date(Date.now() - CLAIM_STALE_S * 1000).toISOString();
    for (const cmd of await abandonedClaims('desk', cutoff)) {
      await finishCommand(cmd.id, 'error', 'abandoned — claimed by desk and never finished');
      await deadLetterCommand(cmd, 'the Mac stopped before it finished this');
      console.log(`[commands] swept abandoned claim ${cmd.id}`);
    }
  } catch (e) { console.error('[commands] sweep claims:', e.message); }
}

export function startCommands() {
  drain();
  sweepAbandonedClaims();
  const sweeper = setInterval(sweepAbandonedClaims, 300000);
  const rt = watchTable({
    channel: 'orch-commands', table: 'desk_commands', event: 'INSERT', onChange: drain, tag: 'commands',
  });
  // Backstop only: 5min while realtime is healthy, COMMAND_POLL_MS when it isn't. A launch that has
  // to wait for the backstop has usually already expired — the point is to reach a terminal state
  // so the hub UI stops spinning, not to run it late.
  const loop = () => {
    drain();
    timer = setTimeout(loop, rt.isHealthy() ? 300000 : config.COMMAND_POLL_MS);
  };
  timer = setTimeout(loop, config.COMMAND_POLL_MS);
  return () => {
    if (timer) clearTimeout(timer);
    clearInterval(sweeper);
    rt.stop();
  };
}
