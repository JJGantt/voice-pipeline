// Putting words into a session that is already running — shared by the Mac (a Terminal tab's tty) and
// the Pi (a tmux pane), which differ only in how the characters physically arrive.
//
// THREE THINGS BOTH MACHINES MUST AGREE ON, which is why they live here and not in either consumer:
//
// 1. WHAT MAY BE TYPED. Claude Code decides whether input is a paste by its shape, and a paste reaches
//    the model wrapped in <pasted_content> tags — framed as material to look at, not as what the user asked.
//    Measured on 2.1.284, typing through tmux: 790 characters arrive as a plain message and 810 arrive
//    wrapped; three lines arrive plain and four arrive wrapped. The same limits hold for bracketed paste
//    and for plain keystrokes. A wrapped message is still acted on as the user's: every long dictation the user
//    pastes at the keyboard arrives as a bare <pasted_content> block and is answered as a request. So a
//    message of any length is simply pasted; transcripts.js unwraps the tags for every surface.
//
//    There is no length cap on this path. A running Claude holds its tty in raw mode, and 20,000
//    characters over 200 lines arrived intact through Terminal's `do script`. The Mac's 1024-byte limit
//    is the CANONICAL-mode line limit, which only binds a line typed to a shell — the launch command,
//    which is why a session's first prompt still travels in a file (applescript.js `openFreshTab`).
//
// 2. WHETHER IT WAS SUBMITTED. Typing reports nothing: text can land in the composer and sit there
//    while the command that typed it reports success. The session's own transcript is the receipt — a
//    submitted prompt is written as a `user` record at once when the session is idle, and as a
//    `queue-operation` enqueue with its text at once when a turn is running (`awaitSubmitted`).
//
// 3. PICTURES. They arrive as object names in the hub's attachment bucket; a session reads a file, so
//    the machine that owns the session fetches them here and the prompt names the local paths.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { config } from './config.js';

// Pictures are fetched here, and messages sent before they were pasted were stashed here as files whose
// stubs transcripts.js still resolves. Not /tmp: periodic cleaning there would take them early.
export const PROMPT_DIR = path.join(os.homedir(), '.claude-desk-prompts');

/** What to paste into a session for `text`, with bracketed-paste markers and then one Enter. */
export function typeable(text) {
  return String(text).replace(/\x1b/g, '').trim();   // an escape could end a bracketed paste early
}

/**
 * Drop stashed messages and pictures older than the window below.
 *
 * NOT a day: the stub typed into a session names its file, and `unstash` in transcripts.js reads it back
 * so every surface shows the words instead of the stub. Sweeping it while the session is still on
 * screen turns the user's message back into the stub wherever it is drawn.
 */
export function sweepStashedPrompts(maxAgeMs = 30 * 86400000) {
  let names;
  try { names = fs.readdirSync(PROMPT_DIR); } catch { return; }
  for (const n of names) {
    const f = path.join(PROMPT_DIR, n);
    try { if (Date.now() - fs.statSync(f).mtimeMs > maxAgeMs) fs.unlinkSync(f); } catch { /* gone already */ }
  }
}

/** Pull an attached picture down beside the stashed prompts, and return its local path. */
async function fetchAttachment(name) {
  if (!/^[\w.-]+$/.test(String(name || ''))) throw new Error(`unusable attachment name: ${name}`);
  fs.mkdirSync(PROMPT_DIR, { recursive: true, mode: 0o700 });
  // The kiosk token in the X-Hub-Token header, never the URL (status-hub accepts it there).
  const r = await fetch(`${config.HUB_URL}/api/attachment?path=${encodeURIComponent(name)}`,
                        { headers: { 'X-Hub-Token': config.HUB_TOKEN } });
  if (!r.ok) throw new Error(`attachment ${name}: ${r.status}`);
  const file = path.join(PROMPT_DIR, name);
  fs.writeFileSync(file, Buffer.from(await r.arrayBuffer()), { mode: 0o600 });
  return file;
}

/**
 * `prompt` with a line naming the pictures in a command's payload, fetched to this machine.
 *
 * A picture with no words is a whole message, so the line is what tells the session to look. It goes
 * on the SAME line as the words rather than above them: a blank line between the two is two more lines
 * toward the limit above, and pushed a short spoken follow-up with a picture into a file handoff.
 * A picture that fails to download is logged and left out; the words still go.
 */
export async function withAttachments(prompt, payload) {
  const names = Array.isArray(payload?.attachments) ? payload.attachments : [];
  const words = String(prompt || '').trim();
  if (!names.length) return words;
  const paths = [];
  for (const n of names) {
    try { paths.push(await fetchAttachment(n)); }
    catch (e) { console.error('[handoff] attachment:', e.message); }
  }
  if (!paths.length) return words;
  const note = `[Jared attached ${paths.length === 1 ? 'a picture' : paths.length + ' pictures'}: `
    + `${paths.join(', ')} — read ${paths.length === 1 ? 'it' : 'them'} now.]`;
  return words ? `${note} ${words}` : note;
}

/** Bytes in a transcript right now — where to start looking for what is typed next. 0 if not yet written. */
export function transcriptSize(transcriptPath) {
  try { return fs.statSync(transcriptPath).size; } catch { return 0; }
}

/**
 * Whether `expect` was written to the transcript after byte `from`, waiting up to `timeoutMs` for it.
 *
 * Woken by the file changing, not by a clock: the directory is watched (the file itself may not exist
 * yet on a session that has never had a turn), and the timeout only bounds how long a message that never
 * submits is waited for. It compares JSON-encoded text because that is how the record stores it.
 */
export function awaitSubmitted(transcriptPath, from, expect, timeoutMs = 8000) {
  const firstLine = String(expect).trim().split(/\r\n|\r|\n/)[0].slice(0, 60);
  const needle = JSON.stringify(firstLine).slice(1, -1);
  const seen = () => {
    try {
      const size = fs.statSync(transcriptPath).size;
      if (size <= from) return false;
      const fd = fs.openSync(transcriptPath, 'r');
      try {
        const buf = Buffer.alloc(size - from);
        fs.readSync(fd, buf, 0, buf.length, from);
        return buf.toString('utf8').includes(needle);
      } finally { fs.closeSync(fd); }
    } catch { return false; }
  };
  if (seen()) return Promise.resolve(true);
  return new Promise((resolve) => {
    let watcher = null;
    const done = (ok) => { clearTimeout(timer); try { watcher?.close(); } catch {} resolve(ok); };
    const timer = setTimeout(() => done(seen()), timeoutMs);
    const base = path.basename(transcriptPath);
    try {
      watcher = fs.watch(path.dirname(transcriptPath), (_ev, f) => {
        if ((!f || f === base) && seen()) done(true);
      });
    } catch { /* no directory yet — the timeout's own check is all there is */ }
  });
}
