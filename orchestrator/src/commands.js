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
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { config } from './config.js';

const exec = promisify(execFile);
import { pendingCommands, claimCommand, finishCommand, upsertDeskSession, deskSession } from './supabase.js';
import { watchTable } from './realtime.js';
import { isKnownRepo } from './repos.js';
import { ensureTrusted } from './trust.js';
import { openFreshTab, sendToTty } from './applescript.js';

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

let timer = null;
let draining = false;

async function run(cmd) {
  // Type a follow-up into a session this machine already has open, addressed by its tab's tty.
  if (cmd.kind === 'follow_up') {
    const session = String((cmd.payload || {}).session || '').trim();
    const prompt = String((cmd.payload || {}).prompt || '').trim();
    if (!prompt) throw new Error('no prompt to send');
    const row = await deskSession(session);
    const tty = row && row.transcript_path;
    if (!tty) throw new Error(`no terminal recorded for ${session}`);
    await sendToTty(tty, prompt);
    return `sent ${prompt.length} chars to ${tty}`;
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
  let staleWarning = null;
  if (!isRoot) {
    try {
      await exec('git', ['-C', target, 'fetch', '--all', '--prune'], { timeout: 120000 });
      await exec('git', ['-C', target, 'pull', '--ff-only'], { timeout: 120000 });
    } catch (e) {
      const msg = String(e.stderr || e.message || '').split('\n')[0].slice(0, 200);
      console.warn(`[commands] pull failed in ${target}: ${msg}`);
      staleWarning = `NOTE: this checkout could not be fast-forwarded (${msg}). Check git status and reconcile with origin before making changes.`;
    }
  }
  const spoken = String((cmd.payload || {}).prompt || '').trim();
  const wantsReport = (cmd.payload || {}).report === true;
  const prompt = [staleWarning, spoken, (wantsReport && spoken) ? REPORT_DIRECTIVE : null].filter(Boolean).join('\n\n') || null;
  const raw = await openFreshTab(target, name, prompt, `mac-${cmd.id.slice(0, 8)}`);
  // openFreshTab reports "<how>\t<tty>"; the tty is the handle a later follow-up addresses.
  const [how, tty] = String(raw).split('\t');
  console.log(`[commands] opened session in ${target} (${how}${tty ? `, tty ${tty}` : ''}, trust: ${trust}`
    + `${prompt ? `, prompt: "${prompt.slice(0, 60)}"` : ''})`);

  // Publish it. Until now the Mac published nothing, so a Mac session was invisible to the sessions
  // panel and unreachable by follow_up — which resolves a dispatch through desk_sessions.command_id.
  try {
    await upsertDeskSession({
      id: `mac-${cmd.id.slice(0, 8)}`, worker: 'desk', repo: name || 'root',
      status: 'resident', command_id: cmd.id, transcript_path: tty || null,
      label: (cmd.payload || {}).label || null,
      prompt: prompt ? prompt.slice(0, 500) : null,
    });
  } catch (e) { console.error('[commands] publish:', e.message); }
  return how;
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
      try {
        await finishCommand(cmd.id, 'done', await run(cmd));
      } catch (e) {
        console.error('[commands] failed:', e.message);
        await finishCommand(cmd.id, 'error', e.message);
      }
    }
  } catch (e) {
    console.error('[commands] drain error:', e.message);
  } finally {
    draining = false;
  }
}

export function startCommands() {
  drain();
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
    rt.stop();
  };
}
