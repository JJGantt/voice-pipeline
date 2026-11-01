// Parking the Mac's sessions that the Mac only HOSTED.
//
// A session dispatched from the watch, the phone or the ops responder opens a Terminal tab here and
// is then driven entirely from elsewhere. Nobody at the Mac closes it, so without this they pile up
// (27 windows on 2026-10-31). A session Jared typed into at the Mac's own keyboard is his to close,
// and is never parked by this.
//
// TELLING THE TWO APART. The transcript records a prompt typed at the keyboard and one this
// orchestrator pasted into the tty identically (`promptSource: typed`, `origin: human`), so the
// difference has to come from the sender: every prompt this process puts into a session — the launch
// prompt and every follow-up — is fingerprinted onto the row's `remote_inputs` BEFORE it is sent.
// A human prompt in the transcript that matches none of them was typed here, and stamps `touched_at`,
// which is never cleared. Only prompts after the row's own start count: a resumed or moved session
// carries history written somewhere else.
//
// Anything else that reaches a session the same way the keyboard does is counted as the keyboard —
// Remote Control from the Claude app is one. That errs toward keeping a session, never toward
// closing one he was using.
//
// Parking is what `end_session` does: the tab goes, the row becomes `evicted`, and the conversation
// stays resumable — a follow-up to a parked session resumes it (the hub and data-layer both send
// `resume_session` for a row that is not live).
import fs from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { patchDeskSession } from './supabase.js';
import { closeTabForTty } from './applescript.js';
import { codexPrompt } from './codex.js';

const exec = promisify(execFile);

// How long a hosted session sits idle before it is parked. Resuming costs a launch, not the
// conversation, so this is short.
const IDLE_PARK_MS = parseInt(process.env.IDLE_PARK_MS || String(30 * 60 * 1000), 10);
// Prompts recorded within this of the row's start are the launch prompt arriving, and may land a
// moment before `started` is stamped.
const START_SLACK_MS = 30000;
const MAX_FINGERPRINTS = 100;

/** What identifies a prompt in the transcript: its first line, as handoff.js awaitSubmitted matches. */
export function fingerprint(text) {
  return String(text || '').trim().split(/\r\n|\r|\n/)[0].slice(0, 60);
}

/**
 * Record that this process is about to put `text` into session `row`. Written before the text is
 * sent, so the reaper can never read the prompt in the transcript ahead of its fingerprint.
 */
export async function noteRemoteInput(row, text) {
  const fp = fingerprint(text);
  if (!fp || !row) return;
  const list = [...(row.remote_inputs || []), fp].slice(-MAX_FINGERPRINTS);
  row.remote_inputs = list;
  await patchDeskSession(row.id, { remote_inputs: list });
}

// How far each row's transcript has been read for keyboard prompts: id -> { path, offset }. A restart
// reads each untouched session's transcript once from the start, which is the cost of not storing it.
const scanned = new Map();
// Rows found typed into, held here as well as in `touched_at`: the row a caller passes may have been
// read before that write landed, and this is what stops it being parked regardless.
const touchedIds = new Set();

/** The text of a human prompt record, or null when the record is not one. */
function humanPrompt(r) {
  // A Codex rollout records his words as a UserMessage item (codex.js), with no mark of where they came
  // from; the fingerprints decide that, as for Claude.
  const codex = codexPrompt(r);
  if (codex) return codex;
  if (r.type !== 'user' || r.isMeta || r.isSidechain || r.isCompactSummary) return null;
  const c = r.message && r.message.content;
  const text = typeof c === 'string' ? c
    : Array.isArray(c) ? c.filter((p) => p && p.type === 'text').map((p) => p.text).join('\n') : null;
  if (!text) return null;
  // A slash command is recorded as markup rather than as what was typed, so `/rename` sent from the
  // hub would never match its fingerprint. Left out either way; a session is touched by a prompt.
  if (text.startsWith('<command-') || text.startsWith('<local-command')) return null;
  // `typed` and `queued` are prompts submitted in the composer; `<bash-input>` is a `!` command,
  // which only the keyboard sends.
  if (r.promptSource === 'typed' || r.promptSource === 'queued') return text;
  if (text.startsWith('<bash-input>')) return text;
  return null;
}

/**
 * The first prompt in `row`'s transcript, since it started, that this process did not send — its
 * timestamp, or null. Reads only what was appended since the last look.
 */
function keyboardPromptAt(row) {
  const file = row.transcript_path;
  if (!file) return null;
  let s = scanned.get(row.id);
  if (!s || s.path !== file) { s = { path: file, offset: 0 }; scanned.set(row.id, s); }
  let size;
  try { size = fs.statSync(file).size; } catch { return null; }
  if (size <= s.offset) return null;
  const fd = fs.openSync(file, 'r');
  let chunk;
  try {
    const buf = Buffer.alloc(size - s.offset);
    fs.readSync(fd, buf, 0, buf.length, s.offset);
    chunk = buf.toString('utf8');
  } finally { fs.closeSync(fd); }
  // Only whole lines: a record mid-write is read on the next look.
  const end = chunk.lastIndexOf('\n');
  if (end < 0) return null;
  s.offset += Buffer.byteLength(chunk.slice(0, end + 1));
  const since = Date.parse(row.started) - START_SLACK_MS;
  const sent = (row.remote_inputs || []).filter(Boolean);
  for (const line of chunk.slice(0, end).split('\n')) {
    let r;
    try { r = JSON.parse(line); } catch { continue; }
    const text = humanPrompt(r);
    if (!text) continue;
    const at = Date.parse(r.timestamp);
    if (!(at >= since)) continue;
    if (sent.some((fp) => text.includes(fp))) continue;
    return r.timestamp;
  }
  return null;
}

/** Whether `pid` has a shell child — a Bash tool call still running, often one sent to the background. */
async function runningCommand(pid) {
  try {
    const { stdout } = await exec('ps', ['-Ao', 'ppid=,comm='], { timeout: 10000 });
    return stdout.split('\n').some((l) => {
      const [ppid, comm] = l.trim().split(/\s+/, 2);
      return Number(ppid) === pid && /(^|\/)-?(zsh|bash|sh)$/.test(comm || '');
    });
  } catch { return true; }   // unknown is treated as busy: never park on a guess
}

/**
 * One look at a resident row whose process is `proc`. Records a keyboard touch, and parks the
 * session when it is hosted-only and has been idle long enough. Returns true when it parked it.
 */
export async function reapIdle(row, proc, transcriptBytes) {
  if (!proc || row.status !== 'resident') return false;
  // A session he opened himself at the Mac is his by definition.
  if (row.origin === 'adopted') return false;
  if (row.touched_at || touchedIds.has(row.id)) return false;

  const touched = keyboardPromptAt(row);
  if (touched) {
    touchedIds.add(row.id);
    row.touched_at = touched;
    await patchDeskSession(row.id, { touched_at: touched });
    console.log(`[idle] ${row.id} was typed into at the Mac — kept until he closes it`);
    return false;
  }

  if (proc.busy !== false || proc.waiting) return false;
  const idleFor = Date.now() - Date.parse(row.active_at || row.started);
  if (!(idleFor >= IDLE_PARK_MS)) return false;
  // The mirror has to hold the whole conversation, or a resume on the other machine starts short.
  // (A Codex rollout is never mirrored, and resumes only here.)
  if (row.claude_session_id && row.agent !== 'codex' && (row.mirror_offset || 0) < transcriptBytes) return false;
  if (await runningCommand(proc.pid)) return false;

  await closeTabForTty(row.tty);
  const status = row.claude_session_id ? 'evicted' : 'ended';
  await patchDeskSession(row.id, { status, ended: new Date().toISOString(), working: false });
  row.status = status;
  scanned.delete(row.id);
  console.log(`[idle] parked ${row.id} (${row.label || row.repo}) — idle ${Math.round(idleFor / 60000)}m, never used at the Mac`);
  return true;
}
