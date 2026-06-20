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
  ORCH_TOKEN: process.env.ORCH_TOKEN || 'REDACTED',
  CLAUDE_BIN: process.env.CLAUDE_BIN || path.join(HOME, '.local/bin/claude'),

  // Where Claude Code stores per-session transcripts.
  PROJECTS_DIR: process.env.CLAUDE_PROJECTS_DIR || path.join(HOME, '.claude/projects'),

  // Polling intervals (ms).
  FOCUS_POLL_MS: parseInt(process.env.FOCUS_POLL_MS || '2000', 10),
  REGISTRY_POLL_MS: parseInt(process.env.REGISTRY_POLL_MS || '15000', 10),

  // pty default geometry.
  PTY_COLS: parseInt(process.env.PTY_COLS || '200', 10),
  PTY_ROWS: parseInt(process.env.PTY_ROWS || '50', 10),

  // How many sessions to surface in the registry.
  REGISTRY_LIMIT: parseInt(process.env.REGISTRY_LIMIT || '5', 10),
};

// terminal_url advertised to the TV/Pi when a real session is active.
export function terminalUrl() {
  return `ws://${config.MAC_HOST}:${config.PORT}/term?token=${config.ORCH_TOKEN}`;
}
