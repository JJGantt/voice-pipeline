// What the Mac's own sessions are doing — published on a tick, the way the Pi publishes its own.
//
// The Mac deliberately does NOT get the Pi's session management: residency caps, eviction and resume
// exist because a headless 4GB machine has to decide what to close, and here Jared can see the tabs
// and close them himself. What it does need is the two facts every surface reads:
//
//   1. IS IT STILL UP. The follow-up pointer on the wall names a session, and a dispatch row that
//      claims to be resident for ever means the pointer keeps naming a Terminal tab that was closed
//      hours ago — pointing at nothing, while looking exactly like pointing at something.
//   2. WHAT IT LAST SAID. The final message of a turn, read from the transcript, so a dispatched
//      job's answer is available off the machine that ran it.
//
// Liveness is `ps` on the tab's tty, not AppleScript: a Terminal tab outlives the process inside it,
// so "the tab is open" is the wrong question — `claude` on that tty is the right one. It also needs
// no Automation permission and cannot block on a modal.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { config } from './config.js';
import { deskSessionsFor, patchDeskSession } from './supabase.js';
import { readTail } from './transcripts.js';

const exec = promisify(execFile);

// A tab is opened, the shell starts, then claude starts. Ending a session that is still coming up
// would delete a job the instant it was dispatched.
const LAUNCH_GRACE_S = 90;

/** Is there a claude process on this tab's tty? */
async function claudeOnTty(tty) {
  if (!tty) return false;
  try {
    const { stdout } = await exec('ps', ['-t', tty.replace('/dev/', ''), '-o', 'comm='], { timeout: 10000 });
    return stdout.split('\n').some((c) => c.trim().split('/').pop() === 'claude');
  } catch { return false; }   // no processes on that tty at all — ps exits non-zero
}

async function tick() {
  let rows;
  try { rows = await deskSessionsFor('desk', 'resident'); }
  catch (e) { return console.error('[desk-sessions] list:', e.message); }

  for (const row of rows || []) {
    const alive = await claudeOnTty(row.tty);
    const ageS = (Date.now() - new Date(row.started).getTime()) / 1000;
    const tail = row.transcript_path ? readTail(row.transcript_path) : null;

    const patch = { last_seen: new Date().toISOString() };
    if (tail) {
      // Carry the old reply forward rather than blanking it: a transcript that momentarily reads as
      // empty (mid-write, or a rotated file) must not erase what the session already said.
      if (tail.lastReply) { patch.last_reply = tail.lastReply.slice(0, 4000); patch.reply_at = tail.replyAt || row.reply_at || null; }
      patch.working = alive && tail.working;
    }
    if (!alive && ageS > LAUNCH_GRACE_S) {
      // ENDED, not evicted. 'evicted' means parked-and-resumable, which is a Pi concept: a closed
      // Terminal tab cannot be brought back, and offering to resume it would be a lie.
      patch.status = 'ended';
      patch.ended = new Date().toISOString();
      patch.working = false;
      console.log(`[desk-sessions] ${row.id} ended (no claude on ${row.tty || 'no tty'})`);
    }
    try { await patchDeskSession(row.id, patch); }
    catch (e) { console.error('[desk-sessions] patch:', e.message); }
  }
}

export function startDeskSessions() {
  tick();
  const timer = setInterval(tick, config.PRESENCE_MS);
  return () => clearInterval(timer);
}
