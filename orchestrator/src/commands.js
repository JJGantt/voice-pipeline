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
import { config } from './config.js';
import { pendingCommands, claimCommand, finishCommand } from './supabase.js';
import { watchTable } from './realtime.js';
import { isKnownRepo } from './repos.js';
import { ensureTrusted } from './trust.js';
import { openFreshTab } from './applescript.js';

let timer = null;
let draining = false;

async function run(cmd) {
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
  const prompt = String((cmd.payload || {}).prompt || '').trim() || null;
  const result = await openFreshTab(target, name, prompt);
  console.log(`[commands] opened session in ${target} (${result}, trust: ${trust}`
    + `${prompt ? `, prompt: "${prompt.slice(0, 60)}"` : ''})`);
  return result;
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
