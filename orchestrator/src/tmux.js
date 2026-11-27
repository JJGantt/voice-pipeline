// Every session the Mac launches runs headless, in a tmux session on a private server, the way the Pi
// and Fly run theirs: nothing opens on the screen. Showing one (`show_session`, data-layer
// `session_window`) opens a Terminal window ATTACHED to that tmux session, so the session itself never
// restarts to come to the front or to go back: hiding it detaches the window, and the window closes
// itself because its shell runs `exit 0` after the attach. A session Jared opens by hand in Terminal is
// not one of these and is left as it is.
//
// A tmux pane has a tty of its own, and the tty is the handle the rest of the orchestrator keeps
// (`desk_sessions.tty`, discover.js, checkLaunch). So a headless session is the same row as a tab
// session, and only the things done TO a tty differ: typing into it, closing it, clearing its husk,
// reading its screen. Each of those in applescript.js asks `tmuxNameForTty` first.
//
// A private server (`-L desk`) with its own config (../tmux.conf) keeps these apart from any tmux
// Jared runs by hand and from his ~/.tmux.conf. From a shell: `tmux -L desk attach -t <mac-…>`.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileP = promisify(execFile);
export const SOCKET = 'desk';

// launchd gives the orchestrator no locale, and without a UTF-8 one tmux prints a tab in a format as
// `_` and mangles non-ASCII text pasted into a session. `-u` and LANG fix both; the server hands LANG
// on to every pane, so claude there runs in UTF-8 as it does in a Terminal tab.
const env = { ...process.env, LANG: process.env.LANG || 'en_US.UTF-8' };
const CONF = new URL('../tmux.conf', import.meta.url).pathname;
const base = ['-u', '-L', SOCKET, '-f', CONF];
const tmux = (args) => execFileP('tmux', [...base, ...args], { timeout: 10000, env })
  .then((r) => r.stdout.trim());

/** The tmux session whose pane is on `tty`, or null (also when no server is running). */
export async function tmuxNameForTty(tty) {
  if (!tty) return null;
  let out;
  try { out = await tmux(['list-panes', '-a', '-F', '#{pane_tty}\t#{session_name}']); }
  catch (e) {
    // No server running is the normal case when no headless session is open.
    if (!/no server running|error connecting/i.test(String(e.stderr || ''))) console.error('[tmux] list-panes failed:', e.message);
    return null;
  }
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

/** Press `keys` (tmux key names: `Escape`, `C-c`) in the session's pane. */
export const keysTmux = (name, ...keys) => tmux(['send-keys', '-t', `=${name}:`, ...keys]);

/** The pane's visible screen as plain text. */
export const screenTmux = (name) => tmux(['capture-pane', '-p', '-t', `=${name}:`]);

/**
 * End the session: `/exit` first, which flushes the transcript, then `pidOnTty`'s process is
 * terminated if it ignored that, then the tmux session goes.
 */
export async function closeTmux(name, tty, pidOnTty) {
  await tmux(['send-keys', '-t', `=${name}:`, '/exit', 'Enter'])
    .catch((e) => console.warn('[tmux] /exit into %s failed, falling back to SIGTERM:', name, e.message));
  await new Promise((r) => setTimeout(r, 3000));
  const pid = await pidOnTty(tty);
  if (pid) {
    try { process.kill(pid, 'SIGTERM'); } catch { /* already gone */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  await killTmux(name);
}

/** The shell command a Terminal window runs to show session `name`: attach, and close once detached. */
export const attachCommand = (name) => `tmux ${base.map((a) => `'${a}'`).join(' ')} attach -t '=${name}'; exit 0`;

/** How many Terminal windows (tmux clients) are showing session `name`. */
export async function clientsOf(name) {
  const out = await tmux(['list-clients', '-t', `=${name}`, '-F', '#{client_tty}']).catch(() => '');
  return out.split('\n').filter(Boolean).length;
}

/** Detach every window showing session `name`; the session keeps running. */
export const detachAll = (name) => tmux(['detach-client', '-s', `=${name}`]);

export const killTmux = (name) => tmux(['kill-session', '-t', `=${name}`]).catch(() => {});
