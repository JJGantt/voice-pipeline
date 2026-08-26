// AppleScript / lsof glue for finding and manipulating Terminal.app desk tabs.
//
// Three behaviors the orchestrator drives:
//   1. closeTabForTty(tty): close the single Terminal tab bound to a tty.
//   2. reopenDeskTab(cwd, id): re-open a desk tab and `claude --resume <id>`
//      "right where it was" — Cmd-T into the front window if Terminal already
//      has one, otherwise launch Terminal and run there.
//   3. openFreshTab(cwd, name): the same tab-opening, but starting a NEW
//      remote-controlled session — what the hub's repo picker fires.
//
// Read-only helpers (enumerateTtys, ttyForSession) are SAFE to call anytime.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { config } from './config.js';
import { findSessionFile } from './sessions.js';

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

// Close the single Terminal tab whose tty matches `tty`. No-op if not found.
// Returns true if a tab was closed.
export async function closeTabForTty(tty) {
  if (!tty) return false;
  const running = await osascript(
    'tell application "System Events" to (exists process "Terminal")'
  );
  if (running !== 'true') return false;
  // We close the *tab* by closing it directly. `close t` closes that tab;
  // if it's the last tab in a window the window closes too, which is fine.
  const result = await osascript(
    `tell application "Terminal"
       set target to "${tty}"
       repeat with w in windows
         repeat with t in tabs of w
           if (tty of t) is target then
             close t
             return "closed"
           end if
         end repeat
       end repeat
       return "notfound"
     end tell`
  );
  return result === 'closed';
}

// Run a shell command in a new Terminal tab, falling back to a new window.
//
// Terminal.app has no scriptable "new tab" — the only way is a Cmd-T keystroke through System
// Events, which needs ACCESSIBILITY permission. The interactive shell inherits Terminal.app's
// grant, but this daemon is spawned by launchd and has none, so from here the keystroke fails with
// "osascript is not allowed to send keystrokes (1002)". A plain `do script` with no `in` clause
// opens a new WINDOW and needs only Automation permission, which the daemon does have.
//
// So: try the tab, and on any failure open a window instead. Either way the caller gets a live
// shell running the command, which is all that matters.
/**
 * Type text into an already-running session, addressed by the tty of its tab.
 *
 * The tty is the only stable handle a Terminal tab has: tab indexes shift as tabs open and close, and
 * "front window" is whatever Jared last clicked — which is exactly the mistake to avoid, since a
 * dictated follow-up landing in the wrong terminal is worse than one that fails.
 */
export async function sendToTty(tty, text) {
  const escaped = String(text).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  return osascript(`tell application "Terminal"
      set found to false
      repeat with w in windows
        repeat with t in tabs of w
          if tty of t is "${tty}" then
            do script "${escaped}" in t
            set found to true
          end if
        end repeat
      end repeat
      if not found then error "no tab with tty ${tty}"
    end tell
    return "sent"`);
}

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

  try {
    const tty = await osascript(`tell application "Terminal" to activate
      delay 0.2
      tell application "System Events" to keystroke "t" using command down
      delay 0.4
      tell application "Terminal"
        set t to do script "${escaped}" in front window
        return tty of t
      end tell`);
    return `newtab\t${String(tty).trim()}`;
  } catch (e) {
    console.warn('[applescript] tab failed (%s) — opening a window instead', e.message.trim().split('\n').pop());
    return newWindow();
  }
}

// Re-open a desk tab and resume the session "right where it was".
export async function reopenDeskTab(cwd, id) {
  return runInNewTerminal(`cd ${shQuote(cwd)}; ${shQuote(config.CLAUDE_BIN)} --resume ${shQuote(id)}`);
}

// Start a FRESH session in `cwd` with Remote Control on, named after the repo (plus a unique
// suffix) so it's identifiable in the Claude app's session list. This is what the hub's repo picker fires: the Mac opens a
// terminal, the session registers itself for remote control, and the phone picks it up from there.
// `prompt` (optional) becomes the session's FIRST message: `claude` takes a positional prompt, so the
// dictated text is handed over at launch rather than typed in afterwards. shQuote is what makes that
// safe — the prompt is arbitrary dictated speech and never gets to be shell syntax.
export async function openFreshTab(cwd, name, prompt) {
  // The Remote Control name is an IDENTITY in the Claude app, so it has to be unique per SESSION,
  // not per repo. Named after the repo alone, two sessions on status-hub both register as
  // "status-hub" and the app lists two entries claiming to be the same session. Bare
  // `--remote-control` already auto-generates a unique name, so only the named path needs a suffix.
  const rcName = name ? `${name}-${Date.now().toString(36)}` : '';
  const rc = rcName ? ` --remote-control ${shQuote(rcName)}` : ' --remote-control';
  const first = prompt ? ` ${shQuote(prompt)}` : '';
  return runInNewTerminal(`cd ${shQuote(cwd)}; ${shQuote(config.CLAUDE_BIN)}${rc}${first}`);
}

// ---- util -----------------------------------------------------------------

// Minimal POSIX single-quote escaping for embedding a value in a shell cmd.
export function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}
