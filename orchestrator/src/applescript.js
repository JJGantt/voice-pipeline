// AppleScript / lsof glue for finding and manipulating Terminal.app desk tabs.
//
// Two behaviors the orchestrator drives:
//   1. closeTabForTty(tty): close the single Terminal tab bound to a tty.
//   2. reopenDeskTab(cwd, id): re-open a desk tab and `claude --resume <id>`
//      "right where it was" — Cmd-T into the front window if Terminal already
//      has one, otherwise launch Terminal and run there.
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

// Re-open a desk tab and resume the session "right where it was".
// If Terminal has a front window: Cmd-T to make a new tab there, then run
// `cd <cwd>; claude --resume <id>` in that front window. Otherwise launch
// Terminal and run the command in a fresh window.
export async function reopenDeskTab(cwd, id) {
  const claude = config.CLAUDE_BIN;
  // Build the shell command. AppleScript string-escape: backslash + quote.
  const shellCmd = `cd ${shQuote(cwd)}; ${shQuote(claude)} --resume ${shQuote(id)}`;
  const escaped = shellCmd.replace(/\\/g, '\\\\').replace(/"/g, '\\"');

  const script = `
    set didLaunch to false
    tell application "System Events"
      set termRunning to (exists process "Terminal")
    end tell
    if not termRunning then
      tell application "Terminal" to activate
      delay 0.6
      set didLaunch to true
    end if
    tell application "Terminal"
      activate
      if (count of windows) is 0 then
        -- no window: a plain "do script" opens a new window and runs there.
        do script "${escaped}"
        return "newwindow"
      end if
    end tell
    if didLaunch then
      -- Terminal was just launched and already has a window with a shell;
      -- run in that front window rather than spawning a second one.
      tell application "Terminal" to do script "${escaped}" in front window
      return "launched"
    else
      -- Terminal already had a window: open a fresh tab via Cmd-T, then run
      -- in the (now front) window.
      tell application "Terminal" to activate
      tell application "System Events" to keystroke "t" using command down
      delay 0.4
      tell application "Terminal" to do script "${escaped}" in front window
      return "newtab"
    end if
  `;
  return osascript(script);
}

// ---- util -----------------------------------------------------------------

// Minimal POSIX single-quote escaping for embedding a value in a shell cmd.
export function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}
