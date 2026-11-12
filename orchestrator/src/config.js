// Central config. Everything is env-overridable; defaults match the deployment
// described in the project notes. SUPABASE_KEY is the service_role key.
import os from 'node:os';
import path from 'node:path';

const HOME = os.homedir();

// The two tokens come only from the environment (the installed launchd plist), never from this file.
export const config = {
  SUPABASE_URL: process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co',
  // service_role key. Prefer env; falls back to empty so we fail loudly rather
  // than silently using a stale hardcoded key.
  SUPABASE_KEY: process.env.SUPABASE_KEY || '',

  PORT: parseInt(process.env.PORT || '7682', 10),
  MAC_HOST: process.env.MAC_HOST || 'Jareds-MacBook-Air.local',
  // Public base the TV/Pi uses to reach the terminal ws. MUST be wss:// — the /tv page is
  // HTTPS, so an insecure ws:// is blocked as mixed content. Tailscale Serve fronts the local
  // orchestrator (127.0.0.1:PORT) with a valid TLS cert on the tailnet FQDN at :443, so this is
  // wss://<the Mac's tailnet FQDN>, set in the launchd plist.
  TERM_BASE: process.env.TERM_BASE || '',
  ORCH_TOKEN: process.env.ORCH_TOKEN || '',
  // The status hub, for pushing live session activity (sessionWatch.js). Same base and same kiosk
  // token the voice worker posts its tool calls to; both go to the wall the same way.
  HUB_URL: process.env.HUB_URL || 'https://jared-status-hub.fly.dev',
  HUB_TOKEN: process.env.HUB_TOKEN || '',
  CLAUDE_BIN: process.env.CLAUDE_BIN || path.join(HOME, '.local/bin/claude'),
  // Model for every session this orchestrator opens, named explicitly rather than left to settings.
  // A session started here is unattended, and the machine default is whatever `/model` last wrote to
  // ~/.claude/settings.json. That default is invisible from an interactive session in ~, because
  // ~/.claude/settings.local.json doubles as the PROJECT settings for the project rooted at the home
  // directory and outranks it — so the default can be wrong for months while every session the user
  // opens by hand looks right. Desk sessions run in a repo, where nothing shadows it. (pi-desk pins
  // the same way, in launch.sh.)
  DESK_MODEL: process.env.DESK_MODEL || 'opus',

  // Where Claude Code stores per-session transcripts.
  PROJECTS_DIR: process.env.CLAUDE_PROJECTS_DIR || path.join(HOME, '.claude/projects'),

  // Polling intervals (ms). FOCUS_POLL_MS is the DEGRADED focus cadence used only while the realtime
  // subscription is down; while it's healthy focus.js backstops at 60s. A focus change is a user picking
  // a session on the wall — fine to catch within ~10s when the instant realtime path is unavailable.
  FOCUS_POLL_MS: parseInt(process.env.FOCUS_POLL_MS || '10000', 10),
  REGISTRY_POLL_MS: parseInt(process.env.REGISTRY_POLL_MS || '15000', 10),
  // Desk control (hub repo picker → new Claude session here). Repo scan is local git work, so 60s
  // is plenty; the command queue is realtime-driven with a slow poll backstop.
  REPO_POLL_MS: parseInt(process.env.REPO_POLL_MS || '60000', 10),
  COMMAND_POLL_MS: parseInt(process.env.COMMAND_POLL_MS || '30000', 10),
  PRESENCE_MS: parseInt(process.env.PRESENCE_MS || '30000', 10),
  PRESENCE_FAIL_EXIT_MS: parseInt(process.env.PRESENCE_FAIL_EXIT_MS || '300000', 10),

  // pty default geometry.
  PTY_COLS: parseInt(process.env.PTY_COLS || '200', 10),
  PTY_ROWS: parseInt(process.env.PTY_ROWS || '50', 10),

  // How many sessions to surface in the registry.
  REGISTRY_LIMIT: parseInt(process.env.REGISTRY_LIMIT || '5', 10),

  // Roots scanned for git repos, ':'-separated. Only direct children are considered — deep scanning
  // would drag in vendored checkouts under node_modules.
  REPO_ROOTS: (process.env.REPO_ROOTS || path.join(HOME, 'repos')).split(':').filter(Boolean),
  // A command older than this when the Mac sees it is not run late here: waking up to a burst of
  // terminal windows is wrong, and dropping the work is too, so it goes to Fly (commands.js sendToFly).
  COMMAND_TTL_S: parseInt(process.env.COMMAND_TTL_S || '120', 10),
};

// terminal_url advertised in voice_focus when a real session is active: wss via Tailscale Serve
// (TLS-terminated at the tailnet FQDN). Without the token, which a client sends as X-Token (src/auth.js):
// voice_focus is a shared row in a realtime publication, not a place for a secret.
export function terminalUrl() {
  return `${config.TERM_BASE}/term`;
}
