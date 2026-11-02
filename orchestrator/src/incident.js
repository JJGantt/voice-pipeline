// An ops-responder incident session reads text anyone can write (error messages, logs, overheard
// speech), so it never runs as an ordinary session here: every launch and resume of one goes through
// the health repo's launcher, which starts claude with that agent's narrow permissions
// (health/responder/README.md § The incident agent's permissions). pi-desk/index.js does the same.
//
// The label is the marker because it is the one field every relaunch carries (dispatch, resume,
// hand-over between machines). A launch that cannot find the launcher is refused, never started with
// this machine's own permissions.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { config } from './config.js';
import { labelForClaudeSession } from './supabase.js';

const LAUNCHER = path.join(os.homedir(), 'repos', 'health', 'responder', 'bin', 'incident-claude');

export const isIncident = (label) => /^Incident: /.test(String(label || ''));

/** The program to run claude with for a session labelled `label`. */
export function claudeBinFor(label) {
  if (!isIncident(label)) return config.CLAUDE_BIN;
  if (!fs.existsSync(LAUNCHER)) throw new Error(`incident session refused: no launcher at ${LAUNCHER}`);
  return LAUNCHER;
}

/** claudeBinFor, for a session known only by its conversation id. */
export async function claudeBinForSession(claudeId) {
  return claudeBinFor(await labelForClaudeSession(claudeId));
}
