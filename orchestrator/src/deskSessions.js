// What the Mac's sessions are doing — published on a tick, the way the Pi publishes its own. Both
// the ones dispatched to it and the ones Jared opened himself (see `adopt` below).
//
// The Mac deliberately does NOT get the Pi's session management: residency caps, eviction and resume
// exist because a headless 4GB machine has to decide what to close, and here Jared can see the tabs
// and close them himself. What it does need is the facts every surface reads:
//
//   1. IS IT STILL UP. A row that claims to be running for ever keeps a closed session on the wall and
//      in the numbering, looking exactly like a live one.
//   2. WHICH CONVERSATION IT IS. Read from the process's own record (discover.js), so a row follows a
//      `/clear` exactly and can never report another session's words.
//   3. WHAT IT LAST SAID, AND WHEN IT LAST DID ANYTHING. The final message of a turn, and the turn
//      boundary (`active_at`) every surface orders and focuses by.
//
// A row belongs to a PROCESS (`pid`), not to a Terminal tab. A tab outlives the process inside it, and
// a later `claude` typed into the same tab is a different session. Keyed by tab, a row closed the day
// before was kept "running" by the next session opened in its tab, which then never got a row of its
// own.
import { config } from './config.js';
import { deskSessionsFor, patchDeskSession, upsertDeskSession } from './supabase.js';
import { sweepStashedPrompts } from './applescript.js';
import { liveClaudeSessions } from './discover.js';
import { mirrorUp } from './mirror.js';
import { readTail } from './transcripts.js';
import { syncSessionWatch, reportTurnEnd } from './sessionWatch.js';
import { relayFirstReply } from './relay.js';

// A tab is opened, the shell starts, then claude starts. Ending a session that is still coming up
// would delete a job the instant it was dispatched.
const LAUNCH_GRACE_S = 90;
// A dispatched row is written as its tab opens, and claude can come up a moment either side of that.
const START_SLACK_MS = 30000;

// Launches in flight (commands.js). Between a tab opening and its row being written, the new claude
// looks exactly like a session nobody dispatched, and adopting it then would give one session two rows.
let launching = 0;
export function beginLaunch() { launching++; }
export function endLaunch() { launching = Math.max(0, launching - 1); }

const ms = (t) => (t ? new Date(t).getTime() : 0);
const later = (a, b) => (!a ? b || null : !b ? a : ms(a) >= ms(b) ? a : b);

/**
 * Is `p` the process this row was started for? Its start has to fall where the row's does, which is
 * what separates the session a row was written for from a later `claude` in the same tab — and from a
 * pid the system has since handed to a new process.
 */
function startedWith(row, p) {
  const t0 = ms(row.started);
  return p.startedAt >= t0 - START_SLACK_MS && p.startedAt <= t0 + LAUNCH_GRACE_S * 1000;
}

/** The process a running row belongs to, or null when it has exited. */
function processOf(row, live, byPid, claimed) {
  if (row.pid) {
    const p = byPid.get(row.pid);
    return p && startedWith(row, p) ? p : null;
  }
  // Not recorded yet: a dispatch whose claude is still coming up, or a row from before rows carried a
  // pid. Its process is the claude on its tab that started with it.
  return live.find((p) => p.tty === row.tty && !claimed.has(p.pid) && startedWith(row, p)) || null;
}

/**
 * A session nothing dispatched — Jared opened it himself. It joins the same table and gets the same
 * treatment, and never touches `desk_focus`: a session appearing is not him choosing to talk to it.
 */
async function adopt(s, rows) {
  const tail = readTail(s.transcriptPath);
  const startedIso = new Date(s.startedAt).toISOString();
  const existing = rows.find((r) => r.claude_session_id === s.claudeId);
  if (existing) {
    // The same conversation running again — `claude --resume` by hand, in a new tab. Revive the row
    // rather than making a second one: its label, its origin and its history are still right.
    await patchDeskSession(existing.id, { status: 'resident', ended: null, tty: s.tty, pid: s.pid,
                                          started: startedIso, transcript_path: s.transcriptPath,
                                          last_seen: new Date().toISOString() });
    console.log(`[desk-sessions] ${existing.id} is running again (${s.tty})`);
    return;
  }
  // Named after the conversation, which a `/clear` elsewhere can leave recorded on another row — so a
  // taken id gets the pid on the end rather than overwriting whichever row already holds it.
  const base = `mac-${s.claudeId.slice(0, 8)}`;
  const id = rows.some((r) => r.id === base) ? `${base}-${s.pid}` : base;
  await upsertDeskSession({
    id, worker: 'desk', repo: s.repo, status: 'resident', origin: 'adopted',
    tty: s.tty, pid: s.pid, transcript_path: s.transcriptPath, claude_session_id: s.claudeId,
    // Claude Code's own name for the conversation. A dispatched session gets a label written by
    // the router; this is the only description one nobody dispatched ever has.
    label: tail.title ? tail.title.slice(0, 60) : null,
    started: startedIso,
    working: s.busy ?? tail.working, last_reply: tail.lastReply ? tail.lastReply.slice(0, 4000) : null,
    last_prompt: tail.lastPrompt ? tail.lastPrompt.slice(0, 4000) : null,
    reply_at: tail.replyAt || null,
    active_at: s.statusAt || tail.turnAt || startedIso,
  });
  console.log(`[desk-sessions] adopted ${s.repo} in ${s.tty} (${tail.title || 'untitled'})`);
}

async function reconcile(row, proc) {
  const alive = !!proc;
  const ageS = (Date.now() - ms(row.started)) / 1000;
  const patch = { last_seen: new Date().toISOString() };

  if (proc) {
    if (row.pid !== proc.pid) patch.pid = proc.pid;
    // The conversation the process is in NOW — `/clear` starts a new one inside the same process.
    if (proc.claudeId !== row.claude_session_id) {
      patch.claude_session_id = proc.claudeId;
      patch.transcript_path = proc.transcriptPath;
      // The mirror is keyed by conversation id, so a new one is a new upload from zero.
      patch.mirror_offset = 0;
      // What the old conversation last said is not what this one said: read afresh below, or blank.
      patch.last_reply = null;
      patch.last_prompt = null;
      patch.reply_at = null;
      Object.assign(row, { claude_session_id: proc.claudeId, transcript_path: proc.transcriptPath, mirror_offset: 0,
                           last_reply: null, last_prompt: null, reply_at: null });
      console.log(`[desk-sessions] ${row.id} is in conversation ${proc.claudeId.slice(0, 8)} now`);
    }
  }

  const tail = row.transcript_path ? readTail(row.transcript_path) : null;
  if (tail) {
    // Carry the old reply forward rather than blanking it: a transcript that momentarily reads as
    // empty (mid-write, or a rotated file) must not erase what the session already said.
    // The prompt goes with its reply: replaced only when the reply is, blank when it is not in the window.
    if (tail.lastReply) {
      patch.last_reply = tail.lastReply.slice(0, 4000);
      patch.last_prompt = tail.lastPrompt ? tail.lastPrompt.slice(0, 4000) : null;
      patch.reply_at = tail.replyAt || row.reply_at || null;
    }
    // What a session is CALLED, in order of authority:
    //   1. a name Jared typed himself (ctrl-R in the session picker, written to the transcript as a
    //      `custom-title` record) — an explicit act, so it outranks everything, dispatched or not;
    //   2. the label the router wrote at dispatch, which names the job that was asked for;
    //   3. Claude Code's own generated title, for a session nobody dispatched and nobody named.
    // Refreshed every tick rather than stamped once, because 1 and 3 both change as work goes on.
    const named = tail.customTitle || (row.origin === 'adopted' ? tail.title : null);
    if (named && named.slice(0, 60) !== row.label) patch.label = named.slice(0, 60);
    // Kept separately from `label` as well: a glanceable surface shows the repo unless he named
    // the session himself, and `label` cannot answer which of the three sources it came from.
    const chosen = tail.customTitle ? tail.customTitle.slice(0, 60) : null;
    if (chosen !== (row.custom_title ?? null)) patch.custom_title = chosen;
  }

  // Whether a turn is in flight, and the last turn boundary. The process's own record says both
  // directly; the transcript is only the fallback, and it can be wrong on a long turn whose prompt has
  // scrolled out of the window readTail reads. sessionWatch writes the boundary the moment one happens;
  // this catches one it missed, since fs.watch drops events.
  const busy = proc && proc.busy != null ? proc.busy : !!(tail && tail.working);
  patch.working = alive && busy;
  // The net under the watcher's relay, for a finished turn fs.watch dropped.
  if (alive && tail && !busy) relayFirstReply(row.id, tail).catch((e) => console.error('[desk-sessions] relay:', e.message));
  // The same net for the reply's notification. A finished turn newer than the one this row records is
  // one the watcher never reported, and without this the phone would never hear of it. Newer than the
  // row itself too: a resumed session's new row starts with no reply recorded, and the last turn of
  // the history it inherited is not news.
  // Reported only once the patch below is written, as the watcher does: a surface told `done` fetches
  // the reply at once, and asked first it gets the previous one.
  const unreported = tail && tail.replyAt && ms(tail.replyAt) > Math.max(ms(row.reply_at), ms(row.started));
  // The record's time is the truth and is written as-is, so a wrong value cannot stick. The
  // transcript's is a reconstruction and may only move the row forward.
  const turnAt = proc && proc.statusAt ? proc.statusAt : later(tail && tail.turnAt, row.active_at);
  if (turnAt && ms(turnAt) !== ms(row.active_at)) patch.active_at = turnAt;

  // Mirror at the end of a turn, which is the only moment the transcript is a complete thought.
  // Idle-only on purpose: uploading mid-turn would ship half an answer, and the tail is re-read on
  // the next tick anyway. This is what makes a session pickable up on the other machine later —
  // including when this one is asleep, which is exactly when it matters.
  if (tail && !busy && row.claude_session_id && tail.bytes > (row.mirror_offset || 0)) {
    try {
      const at = await mirrorUp(row.claude_session_id, row.transcript_path, row.mirror_offset || 0);
      if (at != null) { patch.mirror_offset = at; patch.mirror_at = new Date().toISOString(); }
    } catch (e) { console.error('[desk-sessions] mirror:', e.message); }
  }

  if (!alive && ageS > LAUNCH_GRACE_S) {
    // The two closed states say whether anything can be done about it, and nothing else:
    //   evicted — the transcript is known, so `claude --resume` brings the whole conversation back
    //             in a new tab. Exactly what the Pi means by it; a closed tab is not special.
    //   ended   — no conversation was ever bound (claude never came up), so there is nothing to
    //             resume and offering it would be a lie.
    patch.status = row.claude_session_id ? 'evicted' : 'ended';
    patch.ended = new Date().toISOString();
    patch.working = false;
    row.status = patch.status;   // stop watching it on this tick, not the next
    console.log(`[desk-sessions] ${row.id} ${patch.status} (its process has exited)`);
  }
  await patchDeskSession(row.id, patch);
  if (unreported) reportTurnEnd(row.id, tail);
}

async function tick() {
  // Messages handed to a session by file. A day is far longer than any session takes to read one,
  // and they are dictated speech — not something to leave lying in a home directory for ever.
  sweepStashedPrompts();

  // Retention lives on the Pi (`sweepRetention` in pi-desk), which is always on — one rule in one
  // file, covering every kind of row rather than only the adopted ones this machine happens to make.

  let rows;
  try { rows = await deskSessionsFor('desk'); }
  catch (e) { return console.error('[desk-sessions] list:', e.message); }
  const live = await liveClaudeSessions();
  const byPid = new Map(live.map((p) => [p.pid, p]));
  const claimed = new Set();

  // Every running row finds its process first, so what is left over is exactly what nobody tracks.
  const residents = rows.filter((r) => r.status === 'resident');
  for (const row of residents) {
    const proc = processOf(row, live, byPid, claimed);
    if (proc) claimed.add(proc.pid);
    try { await reconcile(row, proc); }
    catch (e) { console.error('[desk-sessions] patch:', e.message); }
  }
  if (!launching) {
    for (const s of live) {
      if (claimed.has(s.pid)) continue;
      try { await adopt(s, rows); }
      catch (e) { console.error('[desk-sessions] adopt:', e.message); }
    }
  }

  // Watch what is running. The tick's job here is only to NOTICE a session — everything the session
  // then does reaches the wall the instant it happens, without waiting for the next one.
  syncSessionWatch(residents,
    (id, tail) => {
      relayFirstReply(id, tail).catch((e) => console.error('[desk-sessions] relay:', e.message));
      // Returned: the watcher sends `done` only once the row holds the reply.
      return patchDeskSession(id, {
        working: false,
        last_reply: tail.lastReply ? tail.lastReply.slice(0, 4000) : null,
        last_prompt: tail.lastPrompt ? tail.lastPrompt.slice(0, 4000) : null,
        reply_at: tail.replyAt || null,
        active_at: tail.turnAt || new Date().toISOString(),
        last_seen: new Date().toISOString(),
      }).catch((e) => console.error('[desk-sessions] turn-end patch:', e.message));
    },
    (id, at) => {
      patchDeskSession(id, { working: true, active_at: at })
        .catch((e) => console.error('[desk-sessions] turn-start patch:', e.message));
    });
}

export function startDeskSessions() {
  tick();
  const timer = setInterval(tick, config.PRESENCE_MS);
  return () => clearInterval(timer);
}
