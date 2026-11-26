// Stopping a running turn from somewhere other than the keyboard: the Stop button in the hub's session
// view (`interrupt_session`). Shared by the Mac (orchestrator commands.js) and the Pi and Fly (pi-desk).
//
// It is exactly the Esc key, because that already does the whole job, measured against Claude Code
// 2.1.332 in tmux: the running turn ends ("Interrupted · What should Claude do instead?"), and a message
// that was queued behind it runs next on its own; with nothing queued the session sits idle. So nothing
// here manages a queue.
//
// One side effect has to be undone. A turn interrupted before it wrote any answer puts its prompt back in
// the composer for editing, and the next follow-up from a phone would be pasted onto the end of it and
// submitted as one message. So once the interrupt has landed, the screen's composer is read and cleared
// with Ctrl-C (which clears a non-empty composer and nothing else).
//
// The keys differ by terminal. A tmux pane takes a bare Escape. A Terminal tab can only be written to with
// AppleScript `do script`, which always appends a return, and ESC followed by a return in one chunk is
// read as Alt-Return, not Esc. Ctrl-C then return is the same interrupt (measured: queue runs, or idle,
// as with Esc), and the return lands on an empty composer, where it does nothing. Ctrl-C on an EMPTY,
// idle composer arms "press again to exit", which is why nothing is sent to a session that is not busy.
import { transcriptSize, awaitSubmitted } from './handoff.js';

// What Claude Code writes to the transcript when a turn is interrupted: "[Request interrupted by user]",
// or "... for tool use]" when it was cut off mid-tool.
export const INTERRUPTED = '[Request interrupted by user';

// Text Claude Code draws in an empty composer: the hint while messages are queued, and the dimmed
// suggestion an idle one shows (`Try "fix typecheck errors"`). Neither is anything anyone typed.
const HINTS = [/^Press up to edit queued messages$/, /^Try ".*"$/];

/**
 * The composer's text in a plain capture of a Claude Code screen: what sits between the last two full
 * horizontal rules, without the `❯` prompt. '' when empty, null when the screen has no composer (an
 * alternate screen, a dialog drawn in its place).
 */
export function composerText(screen) {
  const lines = String(screen || '').split('\n');
  const rules = [];
  lines.forEach((l, i) => { if (/^\s*─{20,}\s*$/.test(l)) rules.push(i); });
  if (rules.length < 2) return null;
  const [a, b] = rules.slice(-2);
  const body = lines.slice(a + 1, b).map((l) => l.replace(/\s+$/, '')).join('\n')
    .replace(/^\s*❯\s?/, '').trim();
  return HINTS.some((re) => re.test(body)) ? '' : body;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Interrupt the turn `id` is running and report what it did next.
 *
 *   session  the process's record now (discover.js sessionOf): `busy`, `waiting`, `transcriptPath`
 *   state()  re-reads that record, to tell whether a queued message took over
 *   press()  sends the interrupt keys
 *   screen() a plain-text capture of the screen; clear() clears the composer
 *
 * The receipt is the turn ending, read two ways, because Claude Code records it two ways: a turn cut off
 * after it began answering writes INTERRUPTED to the transcript, while one cut off before any answer
 * writes nothing at all (its prompt goes back to the composer) and only its process record turns idle.
 * Neither seen within RECEIPT_MS, the interrupt did not happen, whatever the keystroke returned.
 */
const RECEIPT_MS = 8000;

async function turnEnded(tpath, from, state) {
  const deadline = Date.now() + RECEIPT_MS;
  const marked = tpath ? awaitSubmitted(tpath, from, INTERRUPTED, RECEIPT_MS) : Promise.resolve(false);
  let done = false;
  marked.then((ok) => { if (ok) done = true; });
  // The record file is rewritten in place; a bounded few seconds of re-reading it is the wait for this
  // one keypress to land, not a watch on anything.
  while (!done && Date.now() < deadline) {
    const s = state();
    if (s && s.busy === false && !s.waiting) return true;
    await sleep(250);
  }
  return done || (await marked);
}

export async function stopTurn(id, { session, state, press, screen, clear }) {
  if (!session) throw new Error(`${id} has no Claude Code session record`);
  if (!session.busy && !session.waiting) return `nothing to stop: ${id} is not running a turn`;
  const tpath = session.transcriptPath;
  const from = tpath ? transcriptSize(tpath) : 0;
  await press();
  if (!(await turnEnded(tpath, from, state))) {
    throw new Error(`sent the interrupt to ${id} but its turn did not stop`);
  }
  // Long enough for a queued message to start, or for a cut-off prompt to be put back in the composer.
  await sleep(1500);
  const now = state();
  if (now?.busy) return `stopped ${id}; it went on to the message queued behind it`;
  const left = composerText(await screen().catch((e) => {
    console.error(`[interrupt] reading ${id}'s screen: ${e.message}`);
    return null;
  }));
  if (left) {
    await clear();
    return `stopped ${id}; cleared the interrupted prompt from its composer; waiting for the next message`;
  }
  return `stopped ${id}; waiting for the next message`;
}
