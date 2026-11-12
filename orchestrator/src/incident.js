// An ops-responder incident session starts through the health repo's launcher on every launch and
// resume, which runs claude with permission checks off so it can fix what it finds
// (health/responder/README.md § The incident agent's permissions). pi-desk/index.js does the same.
//
// The label is the marker because it is the one field every relaunch carries (dispatch, resume,
// hand-over between machines). A launch that cannot find the launcher is refused here and handed on.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { config } from './config.js';
import { labelForClaudeSession } from './supabase.js';
import { Reroute } from './route.js';

const LAUNCHER = path.join(os.homedir(), 'repos', 'health', 'responder', 'bin', 'incident-claude');

export const isIncident = (label) => /^Incident: /.test(String(label || ''));

/** The program to run claude with for a session labelled `label`. */
export function claudeBinFor(label) {
  if (!isIncident(label)) return config.CLAUDE_BIN;
  // Another machine's launcher may be there, so the command is handed on (commands.js) rather than lost.
  if (!fs.existsSync(LAUNCHER)) {
    console.error(`[incident] no launcher at ${LAUNCHER}`);
    throw new Reroute(`incident session refused here: no launcher at ${LAUNCHER}`);
  }
  return LAUNCHER;
}

/** claudeBinFor, for a session known only by its conversation id. */
export async function claudeBinForSession(claudeId) {
  return claudeBinFor(await labelForClaudeSession(claudeId));
}
