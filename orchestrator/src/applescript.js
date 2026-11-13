// AppleScript / lsof glue for finding and manipulating Terminal.app desk tabs.
//
// Three behaviors the orchestrator drives:
//   1. closeTabForTty(tty): close the single Terminal tab bound to a tty.
//   2. reopenDeskTab(cwd, id): re-open a desk tab and `claude --resume <id>`
//      "right where it was" — Cmd-T into the front window if Terminal already
//      has one, otherwise launch Terminal and run there.
//   3. openFreshTab(cwd, name): the same tab-opening, but starting a NEW
//      remote-controlled session — what the hub's repo picker fires.
//      A freshly launched incident session runs headless in tmux instead
//      (tmux.js); a resumed one opens a tab like any other. The
//      tty-addressed calls here route a tmux pane's tty there.
//
// Read-only helpers (enumerateTtys, ttyForSession) are SAFE to call anytime.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { config } from './config.js';
import { findSessionFile } from './sessions.js';
import { typeable } from './handoff.js';
import { claudeBinFor, claudeBinForSession, isIncident } from './incident.js';
import { tmuxNameForTty, runInTmux, sendToTmux, closeTmux, killTmux } from './tmux.js';

const execFileP = promisify(execFile);

function osascript(script) {
  return execFileP('osascript', ['-e', script]).then((r) => r.stdout.trim());
}

// ---- READ-ONLY ------------------------------------------------------------

// Resolve the controlling tty (e.g. /dev/ttys008) of the process currently
// holding <id>.jsonl open. Returns null if no live holder is found.
//
// Note: Claude Code appends-and-closes its transcript, so a holder may not
// always exist at the instant we look. Callers must treat null as "no live
// desk tab found -> skip closing" (the documented safe fallback).
async function ttyOf(pid) {
  try {
    const { stdout } = await execFileP('ps', ['-o', 'tty=', '-p', pid]);
    const t = stdout.trim();
    return t && t !== '??' && t !== '?' ? `/dev/${t}` : null;
  } catch { return null; }
}
async function cwdOf(pid) {
  try {
    const { stdout } = await execFileP('lsof', ['-a', '-p', pid, '-d', 'cwd', '-Fn']);
    const line = stdout.split('\n').find((l) => l.startsWith('n'));
    return line ? line.slice(1) : null;
  } catch { return null; }
}

export async function ttyForSession(id, cwd) {
  // 1) Definitive but rare: a process actually holding <id>.jsonl open. Claude appends-and-closes
  //    the transcript, so this usually finds nothing — hence the fallbacks below.
  const file = findSessionFile(id);
  if (file) {
    try {
      const { stdout } = await execFileP('lsof', ['-t', '--', file]);
      for (const pid of stdout.split('\n').map((s) => s.trim()).filter(Boolean)) {
        const t = await ttyOf(pid); if (t) return t;
      }
    } catch { /* no live holder */ }
  }
  // 2) Find the live `claude` desk process for this session. argv match (a tab started via
  //    `claude --resume <id>`) is definitive; otherwise match the session's cwd. Headless claude
  //    (the worker's `claude -p`, our own pty) has no controlling tty, so it's filtered out.
  let pids = [];
  try {
    const { stdout } = await execFileP('pgrep', ['-f', 'claude']);
    pids = stdout.split('\n').map((s) => s.trim()).filter(Boolean);
  } catch { return null; }
  const cwdMatches = [];
  for (const pid of pids) {
    const t = await ttyOf(pid);
    if (!t) continue; // no Terminal tab
    try {
      const { stdout: args } = await execFileP('ps', ['-p', pid, '-o', 'args=']);
      if (args.includes(id)) return t; // resumed with this id → definitive
    } catch { /* */ }
    if (cwd) { const c = await cwdOf(pid); if (c && c === cwd) cwdMatches.push(t); }
  }
  // Trust a cwd match only when unambiguous (exactly one claude in that directory).
  return cwdMatches.length === 1 ? cwdMatches[0] : null;
}

// Enumerate the ttys of every open Terminal tab. Returns [] if Terminal isn't
// running. SAFE — does not touch any tab.
export async function enumerateTtys() {
  const running = await osascript(
    'tell application "System Events" to (exists process "Terminal")'
  );
  if (running !== 'true') return [];
  const out = await osascript(
    `tell application "Terminal"
       set ttys to ""
       repeat with w in windows
         repeat with t in tabs of w
           set ttys to ttys & (tty of t) & linefeed
         end repeat
       end repeat
       return ttys
     end tell`
  );
  return out.split('\n').map((s) => s.trim()).filter(Boolean);
}

// ---- MUTATING (do NOT run against the dev session while testing) ----------

// End the Claude session in the tab whose tty matches `tty`. No-op if not found.
//
// A Terminal TAB does not understand `close` — AppleScript answers -1708 — so the old version of
// this never closed anything and reported failure into whatever asked. What works is the same
// sequence the Pi uses on tmux: ask Claude Code to exit, then terminate the process if it ignores
// that. The shell is left alive in its tab, exactly as tmux leaves a husk; ending the SESSION is
// what matters, because a transcript being resumed elsewhere must have exactly one writer.
//
// The window is closed only when that tab is the last one in it: closing a window with other tabs
// takes unrelated sessions down with it.
// The agent's process on a tty: `claude`, or a Codex session's native `codex`.
async function claudePidOnTty(tty) {
  try {
    const { stdout } = await execFileP('ps', ['-t', tty.replace('/dev/', ''), '-o', 'pid=,comm='], { timeout: 10000 });
    const hit = stdout.split('\n').map((l) => l.trim().split(/\s+/))
      .find(([, comm]) => comm && ['claude', 'codex'].includes(comm.split('/').pop()));
    return hit ? Number(hit[0]) : null;
  } catch { return null; }
}

export async function closeTabForTty(tty) {
  if (!tty) return false;
  const headless = await tmuxNameForTty(tty);
  if (headless) {
    if (!(await claudePidOnTty(tty))) { await killTmux(headless); return false; }
    await closeTmux(headless, tty, claudePidOnTty);
    return true;
  }
  const running = await osascript(
    'tell application "System Events" to (exists process "Terminal")'
  );
  if (running !== 'true') return false;
  if (!(await claudePidOnTty(tty))) return false;   // nothing running there

  // `/exit` is Claude Code's own way out (and Codex's): it flushes the transcript and leaves the repo
  // alone. A signal would do neither, which is why it is only the fallback.
  await osascript(
    `tell application "Terminal"
       repeat with w in windows
         repeat with t in tabs of w
           if (tty of t) is "${tty}" then do script "/exit" in t
         end repeat
       end repeat
     end tell`
  ).catch(() => {});
  await new Promise((r) => setTimeout(r, 3000));

  const pid = await claudePidOnTty(tty);
  if (pid) {
    try { process.kill(pid, 'SIGTERM'); } catch { /* already gone */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  // Only when nothing else is in that window.
  await osascript(
    `tell application "Terminal"
       repeat with w in windows
         if (count of tabs of w) is 1 and (tty of item 1 of tabs of w) is "${tty}" then close w
       end repeat
     end tell`
  ).catch(() => {});
  return true;
}

/**
 * Close the window left behind by a dispatched session that has exited — only when nothing at all is
 * running on its tty any more (the launch command's `exit 0` ended the shell) and it is the window's
 * only tab. A tty is reused by the next tab opened, so a live process there means the tty is someone
 * else's now, and that window is left alone.
 */
export async function closeHuskForTty(tty) {
  if (!tty) return false;
  // A headless session's tmux session ends with its command, so there is normally no husk; one still
  // there with nothing on its tty is cleared all the same.
  const headless = await tmuxNameForTty(tty);
  if (headless) {
    if (await claudePidOnTty(tty)) return false;
    await killTmux(headless);
    return true;
  }
  try {
    const { stdout } = await execFileP('ps', ['-t', tty.replace('/dev/', ''), '-o', 'pid='], { timeout: 10000 });
    if (stdout.trim()) return false;
  } catch (e) {
    // ps exits 1 when the tty has no processes, which is the case being looked for.
    if (String(e.stdout || '').trim()) return false;
  }
  const out = await osascript(
    `tell application "Terminal"
       repeat with w in windows
         if (count of tabs of w) is 1 and (tty of item 1 of tabs of w) is "${tty}" then
           close w
           return "closed"
         end if
       end repeat
       return ""
     end tell`
  ).catch(() => '');
  return out === 'closed';
}

/**
 * Type text into an already-running session, addressed by the tty of its tab.
 *
 * The tty is the only stable handle a Terminal tab has: tab indexes shift as tabs open and close, and
 * "front window" is whatever the user last clicked — which is exactly the mistake to avoid, since a
 * dictated follow-up landing in the wrong terminal is worse than one that fails.
 *
 * What gets typed is `typeable` (handoff.js), shared with the Pi. A running session's tty is raw, so no
 * length limit applies here. Whether it then SUBMITTED is the caller's to confirm, from the transcript.
 *
 * The alternatives for a long message all fail on this end. `do script` always appends a return, so
 * chunking submits each piece as a separate message. System Events can type without one, but needs
 * Accessibility permission a launchd daemon does not have. And `claude --resume <id> "$(cat file)"` in a
 * fresh tab — the trick the LAUNCH path uses, since that goes through a shell — forks a live session:
 * resume starts a second process replaying the same transcript, so the conversation becomes two copies
 * with one id and a message typed into one is invisible in the other.
 */
// `do script` writes the text and its return into the tty in ONE chunk, and Claude Code reads a long
// chunk as a paste: the return lands inside it and the message sits in the composer unsent. Measured
// against an idle session: 8 characters submit, 105 do not. Bracketed-paste markers say exactly where
// the text ends, so the return after them is a keypress whatever the length. (The Pi pastes through
// tmux's own bracketed paste for the same reason.)
const ESC = 'character id 27';

export async function sendToTty(tty, text) {
  const line = typeable(text);
  const headless = await tmuxNameForTty(tty);
  if (headless) { await sendToTmux(headless, line); return line; }
  // An AppleScript string literal cannot hold a raw line break, so each is written as `linefeed` joined
  // into the string; inside the bracketed paste it lands in the composer as a line break, not a submit.
  const escaped = line.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\r\n|\r|\n/g, '" & linefeed & "');
  await osascript(`tell application "Terminal"
      set found to false
      repeat with w in windows
        repeat with t in tabs of w
          if tty of t is "${tty}" then
            do script (${ESC}) & "[200~${escaped}" & (${ESC}) & "[201~" in t
            set found to true
          end if
        end repeat
      end repeat
      if not found then error "no tab with tty ${tty}"
    end tell
    return "sent"`);
  return line;
}

// Run a shell command in a new Terminal tab, falling back to a new window.
//
// Terminal.app has no scriptable "new tab" — the only way is a Cmd-T keystroke through System
// Events, which needs ACCESSIBILITY permission. The interactive shell inherits Terminal.app's
// grant, but this daemon is spawned by launchd and has none, so from here the keystroke fails with
// "osascript is not allowed to send keystrokes (1002)". A plain `do script` with no `in` clause
// needs only Automation permission, which the daemon does have — but it opens a new WINDOW, every
// time: `AppleWindowTabbingMode always` (set for com.apple.Terminal on this Mac) governs windows a
// person opens, not ones AppleScript creates. So until the keystroke has a process with the
// Accessibility grant to send it, every dispatched session is its own window (the out log's
// `newwindow` / `tab failed` lines).
//
// So: try the tab, and on any failure fall back to the plain `do script`. Either way the caller
// gets a live shell running the command.
async function runInNewTerminal(shellCmd) {
  // AppleScript string-escape: backslash + quote.
  const escaped = shellCmd.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  // The tty comes back with the result so the session can be addressed later for follow-ups.
  const newWindow = () => osascript(`tell application "Terminal"
      activate
      set t to do script "${escaped}"
      return tty of t
    end tell`).then((tty) => `newwindow\t${String(tty).trim()}`);

  // Nothing to make a tab in — one call, one window.
  const running = await osascript('tell application "System Events" to (exists process "Terminal")')
    .catch(() => 'false');
  if (running !== 'true') return newWindow();
  const windows = await osascript('tell application "Terminal" to count windows').catch(() => '0');
  if (parseInt(windows, 10) === 0) return newWindow();

  // The keystroke is blind: it goes to whatever app is frontmost, and `do script … in front window`
  // runs in whatever tab is selected there. So a Cmd-T sent before Terminal is in front (activating
  // into a full-screen Space is slow) opens a tab in some other app, and the launch line is then typed
  // into the session already in Terminal's front tab — where it sits as an unsent paste until someone
  // presses Enter there. Hence three checks, each falling back to the window path: Terminal is
  // frontmost before the keystroke, the selected tab afterwards has a tty that NO tab had before it,
  // and that tab is idle (a fresh shell; anything running makes it `busy`). A tab Cmd-T has just made
  // reports its tty as `missing value` for a moment, so the wait reads that as not ready yet: coercing
  // it to text throws. Comparing only against the previously selected tab is not enough: when the front window changes instead of a tab opening, the
  // other window's tab passes, and `busy` is false for a tab running tmux or screen (Terminal's
  // close-without-asking list), so the launch line lands in a Claude session's input inside tmux.
  try {
    const tty = await osascript(`tell application "Terminal" to activate
      repeat 30 times
        tell application "System Events" to if frontmost of process "Terminal" then exit repeat
        delay 0.1
      end repeat
      tell application "System Events" to if not (frontmost of process "Terminal") then error "Terminal never came to the front"
      set was to {}
      tell application "Terminal" to repeat with w in windows
        repeat with x in tabs of w
          set y to tty of x
          if y is not missing value then set end of was to y as text
        end repeat
      end repeat
      tell application "System Events" to keystroke "t" using command down
      tell application "Terminal"
        repeat 20 times
          set t to selected tab of front window
          set y to tty of t
          if y is not missing value then
            if was does not contain (y as text) and not (busy of t) then exit repeat
          end if
          delay 0.1
        end repeat
        set y to tty of t
        if y is missing value then error "the new tab never got a tty"
        if was contains (y as text) then error "Cmd-T made no new tab"
        if busy of t then error "the new tab is running something"
        set t to do script "${escaped}" in t
        return tty of t
      end tell`);
    return `newtab\t${String(tty).trim()}`;
  } catch (e) {
    console.warn('[applescript] tab failed (%s) — opening a window instead', e.message.trim().split('\n').pop());
    return newWindow();
  }
}

// A launch is "done" the moment its command has been typed, which says nothing about whether anything
// started: a launch line typed into a live session's input sat there unsent for 26 minutes while the
// command reported success. So, once per launch, LAUNCH_CHECK_MS later, the tty it was typed into must
// be running a process whose command line carries `marker` — something unique to this launch where
// there is one — and an error goes to the ops responder when it is not.
const LAUNCH_CHECK_MS = 30_000;
function checkLaunch(raw, marker, what) {
  const tty = String(raw).split('\t')[1] || '';
  if (!tty) {
    console.error(`[launch] ${what}: Terminal reported no tty, so whether it started cannot be checked`);
    return raw;
  }
  setTimeout(() => {
    // `ps -t` exits 1 when nothing is on the tty at all, which is the same answer as no match.
    execFile('ps', ['-t', tty.replace(/^\/dev\//, ''), '-o', 'command='], (_err, out) => {
      if (String(out || '').includes(marker)) return;
      console.error(`[launch] ${what} did not start: ${LAUNCH_CHECK_MS / 1000}s after its command was typed into ${tty}, nothing there is running ${marker}`);
    });
  }, LAUNCH_CHECK_MS).unref();
  return raw;
}

// Re-open a desk tab and resume the session "right where it was".
export async function reopenDeskTab(cwd, id) {
  const bin = await claudeBinForSession(id);
  const raw = await runInNewTerminal(`cd ${shQuote(cwd)}; ${shQuote(bin)} --model ${shQuote(config.DESK_MODEL)} --resume ${shQuote(id)}`);
  return checkLaunch(raw, id, `resume of ${id}`);
}

// Start a FRESH session in `cwd` with Remote Control on, named after the repo (plus a unique
// suffix) so it's identifiable in the Claude app's session list. This is what the hub's repo picker fires: the Mac opens a
// terminal, the session registers itself for remote control, and the phone picks it up from there.
//
// THE PROMPT GOES THROUGH A FILE, NOT THE TYPED LINE. `do script` writes the command into the tab's
// tty, and a tty accepts 1024 bytes of input and silently discards the rest — so a dictated prompt
// longer than that was cut off mid-word, leaving the shell holding an unterminated quote and the
// session never starting. Measured exactly: a 2500-character prompt reached the terminal as 1024
// characters. The command line now stays short whatever the prompt is, and the shell reads the text
// itself, where no such limit applies. (This is the same reason pi-desk hands tmux a file path.)
const DISPATCHED = fs.readFileSync(new URL('../dispatched-session.txt', import.meta.url), 'utf8').trim();

// `label` is the session's dispatch label: an incident session starts through incident.js's launcher.
export async function openFreshTab(cwd, name, prompt, sessionId = null, resumeId = null, note = null, label = null) {
  const bin = claudeBinFor(label);
  // The Remote Control name is an IDENTITY in the Claude app, so it has to be unique per SESSION,
  // not per repo. Named after the repo alone, two sessions on status-hub both register as
  // "status-hub" and the app lists two entries claiming to be the same session. Bare
  // `--remote-control` already auto-generates a unique name, so only the named path needs a suffix.
  const rcName = name ? `${name}-${Date.now().toString(36)}` : '';
  const rc = rcName ? ` --remote-control ${shQuote(rcName)}` : ' --remote-control';
  const pf = prompt ? promptFile(prompt) : null;
  const first = pf ? ` "$(cat ${shQuote(pf)}; rm -f ${shQuote(pf)})"` : '';
  // The session's own id, exported for the command so the data MCP — a child of claude, which
  // inherits this environment — can attribute a published report to the session that wrote it.
  const env = sessionId ? `HUB_SESSION_ID=${shQuote(sessionId)} ` : '';
  // Resuming a transcript that was mirrored from the other machine: same flags as a fresh session,
  // so a handed-over session is reachable and identifiable exactly like any other.
  const resume = resumeId ? ` --resume ${shQuote(resumeId)}` : '';
  const model = ` --model ${shQuote(config.DESK_MODEL)}`;
  // A fresh session with a prompt is a dispatch, and has to know it IS the session the request was
  // for — otherwise "start a session in X and have it..." left in the words reads as an instruction to
  // it, and it dispatches the same request a second time. A system prompt, not text in the prompt,
  // so every surface still shows only the user's words. pi-desk/launch.sh reads the same file.
  // `note` is anything else the session must know before its first turn — a session moved here from
  // the Pi mid-turn is told why and what was cut off (commands.js resume_mirrored).
  const sysText = [(pf && !resumeId) ? DISPATCHED : '', note || ''].filter(Boolean).join('\n\n');
  const sys = sysText ? ` --append-system-prompt ${shQuote(sysText)}` : '';
  // `exit 0` after claude: a dispatched tab is the session's and nothing else's, so when the session
  // ends, however it ends, the shell goes too. With the profile's "When the shell exits: Close if the
  // shell exited cleanly" the tab then closes itself. Without that setting it is left as "[Process
  // completed]", which closeHuskForTty clears.
  const cmd = `cd ${shQuote(cwd)}; ${env}${shQuote(bin)}${model}${resume}${rc}${sys}${first}; exit 0`;
  // A fresh incident session is nobody's to watch, so it runs headless instead of opening a tab
  // (tmux.js). A resume is a session brought to the Mac on purpose, so it always opens a tab.
  const raw = isIncident(label) && !resumeId
    ? await runInTmux(sessionId || `desk-${Date.now().toString(36)}`, cmd)
    : await runInNewTerminal(cmd);
  // The Remote Control name is unique to this launch; without one, the resumed id; failing both, claude.
  return checkLaunch(raw, rcName || resumeId || bin, `session ${sessionId || rcName || cwd}`);
}

// A Codex session is started by the same launcher the Pi uses (pi-desk/launch-codex.sh): the flags that
// make an unattended Codex session work are one list, kept there. It reads and deletes the prompt file.
const CODEX_LAUNCHER = new URL('../../pi-desk/launch-codex.sh', import.meta.url).pathname;

/** openFreshTab for a Codex session: a new tab running `codex` in `cwd`, or `codex resume <id>`. */
export async function openCodexTab(cwd, name, prompt, sessionId, resumeId = null) {
  const pf = prompt ? promptFile(prompt) : '';
  // `exit 0` after it, for the reason given in openFreshTab.
  const raw = await runInNewTerminal(`cd ${shQuote(cwd)}; DESK_MACHINE=mac ${shQuote(CODEX_LAUNCHER)} ${shQuote(cwd)} `
    + `${shQuote(name || 'root')} ${shQuote(pf)} ${shQuote(resumeId || '')} ${shQuote(sessionId || '')}; exit 0`);
  return checkLaunch(raw, 'codex', `Codex session ${sessionId || cwd}`);
}

/**
 * Park a prompt on disk for the launch command to read.
 *
 * Deleted by the launcher itself the moment it has been read (`rm` runs inside the same command
 * substitution as the `cat`), so dictated speech does not sit in /tmp for the life of the session.
 * 0600 for the same reason.
 */
function promptFile(text) {
  const f = path.join(os.tmpdir(), `desk-prompt-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6)}.txt`);
  fs.writeFileSync(f, String(text), { mode: 0o600 });
  return f;
}

// ---- util -----------------------------------------------------------------

// Minimal POSIX single-quote escaping for embedding a value in a shell cmd.
export function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}
