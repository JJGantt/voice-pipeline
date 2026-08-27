// What the Mac's sessions are doing — published on a tick, the way the Pi publishes its own. Both
// the ones dispatched to it and the ones Jared opened himself (see `adopt` below).
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
import { deskSessionsFor, patchDeskSession, upsertDeskSession, pruneAdoptedSessions } from './supabase.js';
import { sweepStashedPrompts } from './applescript.js';
import { discoverSessions } from './discover.js';
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

/**
 * Sessions running here that nothing dispatched — Jared opened them himself.
 *
 * They join the same table and get the same treatment, with one hard exception: adoption writes
 * `desk_sessions` and NEVER `desk_focus`. A dictated follow-up is aimed at work handed over, and the
 * session he is typing in at that moment is exactly what it must never land in.
 */
async function adopt(rows) {
  // A tracked session is a live claude process too. Skipping its tab keeps it from being adopted a
  // second time under another identity, and reserving its transcript keeps that file from being
  // handed to the process in the next tab along.
  const knownTtys = new Set(rows.filter((r) => r.status === 'resident' && r.tty).map((r) => r.tty));
  const takenIds = new Set(rows.filter((r) => r.status === 'resident' && r.claude_session_id)
                               .map((r) => r.claude_session_id));
  const byClaudeId = new Map(rows.filter((r) => r.claude_session_id).map((r) => [r.claude_session_id, r]));

  for (const s of await discoverSessions({ knownTtys, takenIds })) {
    const existing = byClaudeId.get(s.claudeId);
    if (existing) {
      // The same conversation running again — `claude --resume` by hand, in a new tab. Revive the
      // row rather than making a second one: its label, its origin and its history are still right.
      await patchDeskSession(existing.id, { status: 'resident', ended: null, tty: s.tty,
                                            transcript_path: s.transcriptPath,
                                            last_seen: new Date().toISOString() });
      console.log(`[desk-sessions] ${existing.id} is running again (${s.tty})`);
      continue;
    }
    const tail = readTail(s.transcriptPath);
    await upsertDeskSession({
      id: `mac-${s.claudeId.slice(0, 8)}`, worker: 'desk', repo: s.repo, status: 'resident',
      origin: 'adopted', tty: s.tty, transcript_path: s.transcriptPath, claude_session_id: s.claudeId,
      // Claude Code's own name for the conversation. A dispatched session gets a label written by
      // the router; this is the only description one nobody dispatched ever has.
      label: tail.title ? tail.title.slice(0, 60) : null,
      started: new Date(s.startedAt).toISOString(),
      working: tail.working, last_reply: tail.lastReply ? tail.lastReply.slice(0, 4000) : null,
      reply_at: tail.replyAt || null,
    });
    console.log(`[desk-sessions] adopted ${s.repo} in ${s.tty} (${tail.title || 'untitled'})`);
  }
}

async function tick() {
  // Messages handed to a session by file. A day is far longer than any session takes to read one,
  // and they are dictated speech — not something to leave lying in a home directory for ever.
  sweepStashedPrompts();

  // Every terminal Jared opens becomes a row, so unlike dispatched work — which is a record of what
  // was asked for and stays — adopted rows are pruned once they are long dead. A week keeps the
  // recent past readable without the table growing for ever.
  try { await pruneAdoptedSessions(new Date(Date.now() - 7 * 86400000).toISOString()); }
  catch (e) { console.error('[desk-sessions] prune:', e.message); }

  let all;
  try { all = await deskSessionsFor('desk'); }
  catch (e) { return console.error('[desk-sessions] list:', e.message); }
  try { await adopt(all); }
  catch (e) { console.error('[desk-sessions] adopt:', e.message); }

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
