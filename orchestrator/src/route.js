// Where work goes when the machine it reached cannot run it now. Shared by both desk consumers
// (pi-desk on the Pi and on Fly, and this orchestrator on the Mac), because the three machines are
// fallbacks for each other and have to agree on the order.
//
// THE RULE: work never queues, holds, waits, expires or is dead-lettered on one machine while another
// could run it. Fly can always run it: `GET /wake` on its desk app starts the machine (Fly's proxy holds
// the request until the server is listening, so a 200 means it is up). So the order is
//
//   from the Pi:  the Mac if it is awake and signed in, else Fly
//   from the Mac: the Pi if it is beating and signed in, else Fly
//   on Fly:       nowhere further — Fly launches anyway, and a shortage there is reported as an error
//
// A dead letter is only for when no machine could take the work (Fly itself would not wake), and is
// always a console.error.
//
// A handoff is a NEW desk_commands row with `payload.target` set, never a re-targeted one: each
// consumer wakes on inserts. The command that was handed on answers `handed to <machine> as <id> — <why>`
// (handedVerdict), which data-layer followHandoff parses to follow the work to where it really ran.

export const FLY_WAKE_URL = process.env.FLY_WAKE_URL || 'https://jared-desk-fly.fly.dev/wake';
// Fly's proxy answers once the machine has booted and the server is listening, which a cold start
// takes tens of seconds to do.
const FLY_WAKE_TIMEOUT_MS = Number(process.env.FLY_WAKE_TIMEOUT_MS || 120000);

/** The desk worker each machine's consumer beats and claims as. */
export const DESK_WORKER = { pi: 'pi-desk', mac: 'desk', fly: 'fly-desk' };
export const MACHINE_OF_WORKER = { 'pi-desk': 'pi', desk: 'mac', 'fly-desk': 'fly' };
const NAME = { mac: 'the Mac', pi: 'the Pi', fly: 'Fly' };

/** Start the Fly desk machine and resolve once it answers. Throws when it does not. */
export async function wakeFly({ url = FLY_WAKE_URL, timeoutMs = FLY_WAKE_TIMEOUT_MS } = {}) {
  const r = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!r.ok) throw new Error(`Fly did not wake: ${url} answered ${r.status}`);
}

/**
 * Can the machine whose `worker_presence` row this is start a session right now: beating within
 * `staleS`, and signed in (Claude's login unexpired, or Codex signed in for a Codex session). A beat
 * without `login_expires_at` (an older consumer) counts as signed in. The same rule as data-layer
 * machinesReady.
 */
export function presenceReady(row, { staleS = 90, agent = 'claude', now = Date.now() } = {}) {
  const seen = row?.last_seen ? Date.parse(row.last_seen) : 0;
  if (!seen || (now - seen) / 1000 > staleS) return false;
  if (agent === 'codex') return row.info?.codex === true;
  const exp = row.info?.login_expires_at;
  return exp === undefined || !!(exp && Date.parse(exp) > now);
}

/** The next machine to try from `from`, given whether the other home machine can take it now. */
export function nextMachine(from, { macReady = false, piReady = false } = {}) {
  if (from === 'pi') return macReady ? 'mac' : 'fly';
  if (from === 'mac') return piReady ? 'pi' : 'fly';
  return null;   // Fly is the last resort
}

/** The detail a handed-on command answers with. */
export function handedVerdict(target, id, why) {
  return `handed to ${NAME[target]} as ${id}${why ? ` — ${why}` : ''}`;
}

/**
 * A failure another machine could get past: this one lacks the project, the incident launcher, a Codex
 * login. The consumer hands the command on (nextMachine) rather than dead-lettering it.
 */
export class Reroute extends Error {}

/**
 * The resume_mirrored payload that moves a session's conversation, from its desk_sessions row. Every
 * movable session (Claude, with a conversation bound) can be resumed this way on any machine, because
 * the conversation is in the mirror.
 */
export function mirroredPayload(row, { target, from, prompt = null, msg = null, moved = null, midTurn = false } = {}) {
  return {
    target, claude_session_id: row.claude_session_id, repo: row.repo, label: row.label || null,
    origin: row.origin || 'dispatched', from,
    ...(prompt ? { prompt } : {}),
    ...(msg ? { msg } : {}),
    ...(moved ? { moved, mid_turn: !!midTurn } : {}),
  };
}

/**
 * What an expired or abandoned command becomes when it is sent to Fly instead (contract §6), or null
 * when it is not a kind that moves. `row` is the desk_sessions row the command names, for the two kinds
 * that address a session: a resume or a follow-up for a session on a machine that is not there moves
 * the conversation from the mirror, with the words riding along.
 */
export function flyCommandFor(cmd, row, from) {
  const p = cmd.payload || {};
  if (cmd.kind === 'open_session' || cmd.kind === 'resume_mirrored') {
    return { kind: cmd.kind, payload: { ...p, target: 'fly' } };
  }
  if (cmd.kind === 'resume_session' || cmd.kind === 'follow_up') {
    if (!row?.claude_session_id || row.agent === 'codex') return null;
    return { kind: 'resume_mirrored',
             payload: mirroredPayload(row, { target: 'fly', from, prompt: p.prompt || null,
                                             msg: p.msg || (p.prompt ? cmd.id : null) }) };
  }
  return null;
}

/** The note a session evacuated from a dead machine is resumed with, when it was mid-turn there. */
export const MOVED_DEAD_MARKER = (machine) => `[Your machine (${machine}) stopped responding mid-session and this conversation was `
  + 'moved here from its last upload. Check what state the work is in (files, commits, deploys) before '
  + 'continuing, then carry on with the task.]';
/** The note a session that was mid-turn when its own machine restarted is resumed with. */
export const RESTARTED_MARKER = (machine) => `[This machine (${machine}) restarted mid-turn and this conversation was `
  + 'resumed from its transcript. Check what state the work is in (files, commits, deploys) before '
  + 'continuing, then carry on with the task.]';

/** The evacuate command's answer. */
export function evacuateVerdict(machine, moved, stuck) {
  return `evacuated ${moved.length} session(s) from ${machine}: ${moved.join(', ') || 'none'}`
    + `; could not move: ${stuck.join(', ') || 'none'}`;
}

/**
 * Where a session this machine still has a live process for has been moved, or null when it is still
 * this machine's (contract §5). A machine that was unreachable comes back to find its sessions resumed
 * elsewhere: the row says `transferred`, or another worker holds the conversation resident. Its local
 * copy must then be closed, never written back to resident: two processes on one conversation diverge.
 * `elsewhere` maps a conversation id to the resident row another worker holds for it.
 *
 * Only a process that STARTED BEFORE the move is fenced (`startedAt`, ms). One started after it is a
 * deliberate resume here — by hand at the keyboard, or a resume under the row's own name — and closing
 * it would throw away what was just asked for.
 */
export function movedTo(row, elsewhere = new Map(), startedAt = 0) {
  if (!row) return null;
  const before = (t) => !startedAt || !t || startedAt < Date.parse(t);
  const other = row.claude_session_id ? elsewhere.get(row.claude_session_id) : null;
  if (other && before(other.started)) return MACHINE_OF_WORKER[other.worker] || other.worker;
  if (row.status === 'transferred' && before(row.ended)) return 'another machine';
  return null;
}
