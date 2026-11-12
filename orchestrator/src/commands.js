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
//   - commands older than COMMAND_TTL_S are not run late here: they go to Fly (sendToFly), so a Mac
//     that was asleep doesn't wake up and open every terminal that was queued in the meantime, and
//     the work is still done somewhere.
//
// A command this Mac cannot run but another machine could (a project it does not have, no Codex
// login, no incident launcher) is handed on — to the Pi when it can take it, else to Fly (route.js)
// — rather than dead-lettered.
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
         patchDeskSession, deadLetterCommand, abandonedClaims, insertCommand, patchDeskSessionIf,
         deskSessionsFor, workerPresence } from './supabase.js';
import { wakeFly, presenceReady, nextMachine, handedVerdict, Reroute, mirroredPayload, flyCommandFor,
         MOVED_DEAD_MARKER, evacuateVerdict, DESK_WORKER } from './route.js';
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
import { mirrorUp, mirrorDown, mirrorHas } from './mirror.js';
import { fingerprint, noteRemoteInput } from './idle.js';
import { streak } from './streak.js';

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
// A command that lands while a drain is running would not be in the rows that drain already read, and its
// realtime event would be dropped by the guard below; without this it waits for the backstop poll.
let drainAgain = false;

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
  if (!isKnownRepo(dir)) throw new Reroute(`not a known project on the Mac: ${name}`);
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
    if (row.agent === 'codex' && !codexReady()) throw new Reroute('Codex is not signed in on the Mac');
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
    const to = ['pi', 'fly'].includes((cmd.payload || {}).to) ? cmd.payload.to : 'mac';
    const row = await deskSession(id);
    if (!row) throw new Error(`no such session: ${id}`);
    if (!row.claude_session_id) throw new Error(`${id} has no transcript recorded — nothing to hand over`);
    if (row.agent === 'codex') throw new Error(`${id} is a Codex session, and Codex sessions do not move between machines`);
    // Woken before the tab is closed: a Fly that will not answer leaves the session running here.
    if (to === 'fly') await wakeFly();
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
    return `handed ${id} to ${{ pi: 'the Pi', fly: 'Fly', mac: 'the Mac' }[to]}`;
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
    // A follow-up rides along when the session's machine was asleep and the sender moved the session
    // here to deliver it (data-layer deliverFollowUp, the hub's session followup).
    const prompt = await withAttachments((cmd.payload || {}).prompt, cmd.payload) || null;
    if (prompt && !moved) expectPrompt(sessionId, prompt, (cmd.payload || {}).msg || cmd.id);
    beginLaunch();   // until the row exists — see open_session below
    try {
      // A prompt rides along when the Pi hands over a resume it had no room for (pi-desk handResumeToMac),
      // and `moved` when it closed a session mid-turn for doing heavy work there (pi-desk onHeavy).
      const note = moved
        ? `${MOVED_NOTE.replace('{why}', String(moved))}\n\n${midTurn ? MOVED_MID_TURN : MOVED_BETWEEN_TURNS}` : null;
      const raw = await openFreshTab(dir, repo, prompt, sessionId, claudeId, note,
                                     (cmd.payload || {}).label || null);
      const [how, tty] = String(raw).split('\t');
      await upsertDeskSession({
        id: sessionId, worker: 'desk', repo, status: 'resident',
        origin: (cmd.payload || {}).origin || 'dispatched', command_id: cmd.id, tty: tty || null,
        label: (cmd.payload || {}).label || null,
        claude_session_id: claudeId, transcript_path: file,
        // Everything already uploaded stays uploaded: this machine carries on appending from here.
        mirror_offset: fs.statSync(file).size,
        remote_inputs: prompt ? [fingerprint(prompt)] : [],
        active_at: new Date().toISOString(),
      });
      return `resumed ${claudeId.slice(0, 8)} here (${how})`;
    } finally { endLaunch(); }
  }

  // A machine stopped answering (the ops responder's /live, health/responder): every session it held
  // that can move is taken over HERE, from its last upload. Marking each row transferred is the claim —
  // conditional on it still being resident, so two machines evacuating at once never both resume one,
  // and the machine itself, if it comes back, closes its copy (deskSessions.js / pi-desk fencing).
  if (cmd.kind === 'evacuate') {
    const machine = String((cmd.payload || {}).machine || '');
    const worker = DESK_WORKER[machine];
    if (!worker) throw new Error(`unknown machine to evacuate: ${JSON.stringify(machine)}`);
    if (worker === 'desk') return `${machine} is this machine, and it is up — nothing moved`;
    const moved = [], stuck = [];
    for (const row of await deskSessionsFor(worker, 'resident')) {
      if (row.agent === 'codex' || !row.claude_session_id) {
        stuck.push(`${row.id} (${row.agent === 'codex' ? 'a Codex session' : 'no conversation'})`);
        continue;
      }
      if (!(await mirrorHas(row.claude_session_id))) { stuck.push(`${row.id} (nothing mirrored)`); continue; }
      const won = await patchDeskSessionIf(row.id, 'resident',
        { status: 'transferred', ended: new Date().toISOString(), working: false });
      if (!won.length) continue;   // it moved or ended while this ran
      try {
        await run({ id: crypto.randomUUID(), kind: 'resume_mirrored',
                    payload: mirroredPayload(row, { target: 'mac', from: machine,
                                                    prompt: row.working ? MOVED_DEAD_MARKER(machine) : null }) });
        moved.push(row.id);
      } catch (e) {
        // Not resumed here, so it is handed back to its row as it was rather than left marked as moved.
        await patchDeskSessionIf(row.id, 'transferred', { status: 'resident', ended: row.ended || null, working: row.working === true })
          .catch((e2) => console.error(`[commands] restoring ${row.id} after a failed evacuation:`, e2.message));
        stuck.push(`${row.id} (${e.message})`);
      }
    }
    if (stuck.length) console.error(`[commands] evacuating ${machine}: could not move ${stuck.join(', ')}`);
    return evacuateVerdict(machine, moved, stuck);
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
  if (!isRoot && !isKnownRepo(target)) throw new Reroute(`not a known project on the Mac: ${target}`);
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
  if (agent === 'codex' && !codexReady()) throw new Reroute('Codex is not signed in on the Mac');
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
  // reconcile tick binds the same way, so a miss here costs one tick, not the session. A launch that
  // never started is reported by the launch's own check (applescript.js checkLaunch), so a miss here
  // is a plain line: reporting it too opened a second incident for the same failed launch.
  if (tty) {
    (agent === 'codex' ? awaitCodexOn(tty, startedAt) : awaitSessionOn(tty, startedAt)).then((s) => {
      if (!s) return console.log(`[commands] no ${agent} came up on ${tty} for ${sessionId} (checkLaunch reports it)`);
      console.log(`[commands] ${sessionId} -> ${agent} session ${s.claudeId}`);
      return patchDeskSession(sessionId, { pid: s.pid, claude_session_id: s.claudeId, transcript_path: s.transcriptPath });
    }).catch((e) => console.error('[commands] session:', e.message));
  }

  return how;
}

const followUpChains = new Map();

/** Insert a command for another machine, waking Fly first when it is for Fly. Returns its id. */
async function sendCommand(kind, payload) {
  if (payload.target === 'fly') await wakeFly();
  return insertCommand(kind, payload);
}

/** Where work this Mac cannot run goes: the Pi when it is beating and signed in, else Fly. */
async function elsewhere(agent = 'claude') {
  let pi = null;
  try { pi = await workerPresence(DESK_WORKER.pi); } catch (e) { console.warn('[commands] reading the Pi\'s presence:', e.message); }
  return nextMachine('mac', { piReady: presenceReady(pi, { agent }) });
}

/**
 * Resume one of this Mac's parked sessions on `target` from the mirror: flushed, marked transferred so
 * only one machine holds it, then asked for there. Fly is woken before anything is marked.
 */
async function handResume(row, target, { prompt = null, msg = null } = {}) {
  if (target === 'fly') await wakeFly();
  const at = row.transcript_path ? await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0) : null;
  const now = new Date().toISOString();
  await patchDeskSession(row.id, { status: 'transferred', ended: now, working: false,
                                   ...(at != null ? { mirror_offset: at, mirror_at: now } : {}) });
  return insertCommand('resume_mirrored', mirroredPayload(row, { target, from: 'mac', prompt, msg }));
}

/** Hand a command this Mac cannot run (a Reroute) to the next machine. Returns the verdict. */
async function reroute(cmd, why) {
  const p = cmd.payload || {};
  console.log(`[commands] ${cmd.id} (${cmd.kind}) cannot run here: ${why} — handing it on`);
  if (cmd.kind === 'open_session') {
    const target = await elsewhere(p.agent === 'codex' ? 'codex' : 'claude');
    // The other machines launch by project name; the Mac's path means nothing there.
    const name = String(p.name || '').trim() || (p.path === '~' ? 'root' : path.basename(String(p.path || '')) || 'root');
    return handedVerdict(target, await sendCommand('open_session', { ...p, name, target }), why);
  }
  if (cmd.kind === 'resume_mirrored') {
    const target = await elsewhere();
    return handedVerdict(target, await sendCommand('resume_mirrored', { ...p, target }), why);
  }
  if (cmd.kind === 'resume_session') {
    const row = await deskSession(String(p.session || '').trim());
    if (!row?.claude_session_id || row.agent === 'codex') throw new Error(why);   // a Codex conversation cannot move
    const target = await elsewhere();
    return handedVerdict(target, await handResume(row, target, { prompt: p.prompt || null, msg: p.msg || cmd.id }), why);
  }
  throw new Error(why);
}

/**
 * A command this Mac claimed and cannot run in time — it expired before the Mac saw it (asleep), or the
 * Mac claimed it and stopped before finishing. It goes to Fly (route.js flyCommandFor): a resume or a
 * follow-up moves its conversation there from the mirror, words included. Dead-lettered only when it
 * cannot move or Fly will not wake. Every case is a console.error: the work reached Fly this way because
 * the machine it was meant for did not take it.
 */
async function sendToFly(cmd, why, status) {
  const p = cmd.payload || {};
  const row = ['resume_session', 'follow_up'].includes(cmd.kind) ? await deskSession(String(p.session || '').trim()) : null;
  const fly = flyCommandFor(cmd, row, 'mac');
  if (!fly) {
    console.error(`[commands] ${cmd.id} (${cmd.kind}) ${why}, and it cannot move to Fly — dead-lettered`);
    await finishCommand(cmd.id, status, why).catch((e) => console.error('[commands] finish:', e.message));
    return deadLetterCommand(cmd, why).catch((e) => console.error('[commands] dead letter:', e.message));
  }
  try {
    if (row && fly.kind === 'resume_mirrored') {
      // The conversation moves: the row is marked first, so the machine it was on closes its copy.
      if (row.worker === 'desk' && row.transcript_path) {
        await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0).catch(() => null);
      }
      await patchDeskSession(row.id, { status: 'transferred', ended: new Date().toISOString(), working: false });
    }
    const id = await sendCommand(fly.kind, fly.payload);
    await finishCommand(cmd.id, 'done', handedVerdict('fly', id, why));
    console.error(`[commands] ${cmd.id} (${cmd.kind}) ${why} — sent to Fly as ${id}`);
  } catch (e) {
    console.error(`[commands] ${cmd.id} (${cmd.kind}) ${why}, and sending it to Fly failed (${e.message}) — dead-lettered`);
    await finishCommand(cmd.id, status, `${why}; Fly: ${e.message}`).catch((err) => console.error('[commands] finish:', err.message));
    await deadLetterCommand(cmd, `${why}; sending it to Fly failed: ${e.message}`)
      .catch((err) => console.error('[commands] dead letter:', err.message));
  }
}

async function execute(cmd) {
  try {
    let detail;
    try { detail = await run(cmd); }
    catch (e) { if (!(e instanceof Reroute)) throw e; detail = await reroute(cmd, e.message); }
    await finishCommand(cmd.id, 'done', detail);
  } catch (e) {
    console.error('[commands] failed:', e.message);
    await finishCommand(cmd.id, 'error', e.message).catch((err) => console.error('[commands] finish:', err.message));
    await deadLetterCommand(cmd, e.message).catch((err) => console.error('[commands] dead letter:', err.message));
  }
}

/** A follow-up for a session open on this Mac: late or not, it can be delivered now. */
async function deliverableHere(cmd) {
  if (cmd.kind !== 'follow_up') return false;
  const target = (cmd.payload || {}).target;
  if (target && target !== 'mac') return false;
  const row = await deskSession(String((cmd.payload || {}).session || '').trim());
  return row?.worker === 'desk' && row.status === 'resident';
}

const drainStreak = streak('[commands] drain');
async function drain() {
  if (draining) { drainAgain = true; return; }
  draining = true;
  drainAgain = false;
  try {
    for (const cmd of await pendingCommands()) {
      const ageS = (Date.now() - new Date(cmd.created).getTime()) / 1000;
      // Late: a laptop that was asleep must not wake up and open every terminal queued in the
      // meantime, and the work must still happen, so it goes to Fly. Not a command already for Fly,
      // which runs whatever reaches it; and not a follow-up for a session open here, which the Mac can
      // deliver now.
      const target0 = (cmd.payload || {}).target;
      if (ageS > config.COMMAND_TTL_S && target0 !== 'fly' && !(await deliverableHere(cmd))) {
        if (await claimCommand(cmd.id)) {
          await sendToFly(cmd, `expired unrun — queued ${Math.round(ageS)}s before this machine saw it`, 'expired');
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
    drainStreak.ok();
  } catch (e) {
    drainStreak.fail(e);
  } finally {
    draining = false;
    if (drainAgain) drain().catch((e) => console.error(`[commands] drain re-run failed: ${e.message || e}`));
  }
}

/**
 * Commands this machine claimed and never finished.
 *
 * A crash, a restart, or a sleep between claim and finish leaves a row 'running' for ever: no
 * consumer will look at it again, and nothing anywhere says it did not happen. Only the claimer can
 * judge this — hence claimed_by — and only well after the longest legitimate launch could still be
 * in flight. What it finds goes to Fly (sendToFly) rather than being dropped.
 */
const CLAIM_STALE_S = parseInt(process.env.CLAIM_STALE_S || '900', 10);
const sweepStreak = streak('[commands] sweep claims');
async function sweepAbandonedClaims() {
  try {
    const cutoff = new Date(Date.now() - CLAIM_STALE_S * 1000).toISOString();
    for (const cmd of await abandonedClaims('desk', cutoff)) {
      await sendToFly(cmd, 'abandoned — claimed by desk and never finished', 'error');
    }
    sweepStreak.ok();
  } catch (e) { sweepStreak.fail(e); }
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
