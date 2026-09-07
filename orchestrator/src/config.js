// Central config. Everything is env-overridable; defaults match the deployment
// described in the project notes. SUPABASE_KEY is the service_role key.
import os from 'node:os';
import path from 'node:path';

const HOME = os.homedir();

export const config = {
  SUPABASE_URL: process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co',
  // service_role key. Prefer env; falls back to empty so we fail loudly rather
  // than silently using a stale hardcoded key.
  SUPABASE_KEY: process.env.SUPABASE_KEY || '',

  PORT: parseInt(process.env.PORT || '7682', 10),
  MAC_HOST: process.env.MAC_HOST || 'Jareds-MacBook-Air.local',
  // Public base the TV/Pi uses to reach the terminal ws. MUST be wss:// — the /tv page is
  // HTTPS, so an insecure ws:// is blocked as mixed content. Tailscale Serve fronts the local
  // orchestrator (127.0.0.1:PORT) with a valid TLS cert on the tailnet FQDN at :443.
  TERM_BASE: process.env.TERM_BASE || 'wss://<the Mac>.<tailnet>.ts.net',
  ORCH_TOKEN: process.env.ORCH_TOKEN || 'REDACTED',
  // The status hub, for pushing live session activity (sessionWatch.js). Same base and same kiosk
  // token the voice worker already posts its tool calls to; both go to the wall the same way.
  HUB_URL: process.env.HUB_URL || 'https://jared-status-hub.fly.dev',
  HUB_TOKEN: process.env.HUB_TOKEN || 'REDACTED',
  CLAUDE_BIN: process.env.CLAUDE_BIN || path.join(HOME, '.local/bin/claude'),

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

  // pty default geometry.
  PTY_COLS: parseInt(process.env.PTY_COLS || '200', 10),
  PTY_ROWS: parseInt(process.env.PTY_ROWS || '50', 10),

  // How many sessions to surface in the registry.
  REGISTRY_LIMIT: parseInt(process.env.REGISTRY_LIMIT || '5', 10),

  // Roots scanned for git repos, ':'-separated. Only direct children are considered — deep scanning
  // would drag in vendored checkouts under node_modules.
  REPO_ROOTS: (process.env.REPO_ROOTS || path.join(HOME, 'repos')).split(':').filter(Boolean),
  // A queued launch is only wanted for about a minute. If the Mac was asleep when the hub enqueued
  // it, waking up to a burst of terminal windows is worse than dropping them — those expire.
  COMMAND_TTL_S: parseInt(process.env.COMMAND_TTL_S || '120', 10),
};

// terminal_url advertised to the TV/Pi when a real session is active. wss via Tailscale Serve
// (TLS-terminated at the tailnet FQDN) so the HTTPS /tv page can open it without mixed-content.
export function terminalUrl() {
  return `${config.TERM_BASE}/term?token=${config.ORCH_TOKEN}`;
}
