// Desk command queue consumer.
//
// The hub (on Fly) can't reach this Mac, so it enqueues into `desk_commands` and this drains the
// queue: realtime fires on INSERT for an immediate launch, and a slow poll catches anything the
// subscription missed. Claiming is a compare-and-swap on status, so a duplicate event can't open
// the same terminal twice.
//
// Two guards, because this ends in "run a shell command on the desk":
//   - the target path must be a repo THIS process published (repos.js owns that list) — a bad or
//     stale queue row can't `cd` anywhere else on disk;
//   - commands older than COMMAND_TTL_S expire unrun, so a Mac that was asleep doesn't wake up and
//     open every terminal that was queued in the meantime.
import { config } from './config.js';
import { pendingCommands, claimCommand, finishCommand } from './supabase.js';
import { watchTable } from './realtime.js';
import { isKnownRepo } from './repos.js';
import { openFreshTab } from './applescript.js';

let timer = null;
let draining = false;

async function run(cmd) {
  if (cmd.kind !== 'open_session') throw new Error(`unknown kind ${cmd.kind}`);
  const target = String((cmd.payload || {}).path || '');
  if (!target) throw new Error('no path in payload');
  if (!isKnownRepo(target)) throw new Error(`not a known repo: ${target}`);
  const name = String((cmd.payload || {}).name || '').trim() || null;
  const result = await openFreshTab(target, name);
  console.log(`[commands] opened session in ${target} (${result})`);
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
