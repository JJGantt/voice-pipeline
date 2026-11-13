// Headless sessions on the Mac: a tmux session instead of a Terminal tab, so nothing opens on the
// screen. A freshly launched incident session runs this way (applescript.js openFreshTab); everything
// else opens a tab, because those are sessions Jared may want in front of him. That includes an
// incident session resumed or moved to the Mac: bringing a session here is asking to see it.
//
// A tmux pane has a tty of its own, and the tty is the handle the rest of the orchestrator keeps
// (`desk_sessions.tty`, discover.js, checkLaunch). So a headless session is the same row as a tab
// session, and only the three things done TO a tty differ: typing into it, closing it, clearing its
// husk. Each of those in applescript.js asks `tmuxNameForTty` first.
//
// A private server (`-L desk`, no config file) keeps these apart from any tmux Jared runs by hand and
// from his ~/.tmux.conf. To watch one: `tmux -L desk attach -t <mac-…>` (detach with C-b d).
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileP = promisify(execFile);
export const SOCKET = 'desk';

// launchd gives the orchestrator no locale, and without a UTF-8 one tmux prints a tab in a format as
// `_` and mangles non-ASCII text pasted into a session. `-u` and LANG fix both; the server hands LANG
// on to every pane, so claude there runs in UTF-8 as it does in a Terminal tab.
const env = { ...process.env, LANG: process.env.LANG || 'en_US.UTF-8' };
const tmux = (args) => execFileP('tmux', ['-u', '-L', SOCKET, '-f', '/dev/null', ...args], { timeout: 10000, env })
  .then((r) => r.stdout.trim());

/** The tmux session whose pane is on `tty`, or null (also when no server is running). */
export async function tmuxNameForTty(tty) {
  if (!tty) return null;
  let out;
  try { out = await tmux(['list-panes', '-a', '-F', '#{pane_tty}\t#{session_name}']); }
  catch { return null; }
  const hit = out.split('\n').map((l) => l.split('\t')).find(([t]) => t === tty);
  return hit ? hit[1] : null;
}

/**
 * Run `shellCmd` in a new detached tmux session called `name`. Returns "tmux\t<tty>", the shape
 * runInNewTerminal returns, so callers record the tty the same way.
 *
 * An interactive login zsh, as a Terminal tab is, so the session sees the same PATH and profile. The
 * session ends when the command does: there is no shell left behind to clear.
 */
export async function runInTmux(name, shellCmd) {
  await tmux(['new-session', '-d', '-s', name, '-x', '200', '-y', '50', '--', '/bin/zsh', '-lic', shellCmd]);
  const tty = await tmux(['display-message', '-p', '-t', `=${name}:`, '#{pane_tty}']);
  return `tmux\t${tty}`;
}

/**
 * Type `line` into the session and press Enter. Bracketed paste (`-p`), so a long or multi-line
 * message lands in the composer whole and the Enter after it submits; the same as pi-desk `typeInto`.
 * Whether it was submitted is the caller's to confirm from the transcript.
 */
export async function sendToTmux(name, line) {
  const buf = `od-${name}`;
  await tmux(['set-buffer', '-b', buf, '--', line]);
  await tmux(['paste-buffer', '-p', '-r', '-d', '-b', buf, '-t', `=${name}:`]);
  await new Promise((r) => setTimeout(r, 300));
  await tmux(['send-keys', '-t', `=${name}:`, 'Enter']);
}

/**
 * End the session: `/exit` first, which flushes the transcript, then `pidOnTty`'s process is
 * terminated if it ignored that, then the tmux session goes.
 */
export async function closeTmux(name, tty, pidOnTty) {
  await tmux(['send-keys', '-t', `=${name}:`, '/exit', 'Enter']).catch(() => {});
  await new Promise((r) => setTimeout(r, 3000));
  const pid = await pidOnTty(tty);
  if (pid) {
    try { process.kill(pid, 'SIGTERM'); } catch { /* already gone */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  await killTmux(name);
}

export const killTmux = (name) => tmux(['kill-session', '-t', `=${name}`]).catch(() => {});
