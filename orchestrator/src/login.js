// This machine's interactive Claude login (`/login`), as the heartbeat publishes it.
//
// Every dispatched session runs on that login — the launchers strip the year-long token on purpose,
// because only the real login carries Remote Control and Claude in Chrome. Its refresh token dies on a
// fixed date about four weeks after sign-in, and a session started after that answers "Login expired"
// and does nothing. So the date rides in each desk heartbeat (`info.login_expires_at`) and the
// dispatcher (data-layer desk.ts) routes around a machine whose date has passed, with no extra call.
//
// The date only changes when someone signs in again, so it is read once and re-read when the store it
// lives in changes: the credentials file on the Pi, the login keychain on the Mac. The keychain file
// also changes for unrelated reasons; a re-read is a local read, so that costs nothing worth gating.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const MAC = process.platform === 'darwin';
const STORE_DIR = MAC ? path.join(os.homedir(), 'Library/Keychains') : path.join(os.homedir(), '.claude');

function read() {
  try {
    const raw = MAC
      ? execFileSync('security', ['find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                     { encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'] })
      : fs.readFileSync(path.join(STORE_DIR, '.credentials.json'), 'utf8');
    const o = JSON.parse(raw).claudeAiOauth || {};
    if (!o.refreshToken || !o.refreshTokenExpiresAt) return null;
    return new Date(o.refreshTokenExpiresAt).toISOString();
  } catch { return null; }   // no login at all reads the same as an expired one: neither can run a session
}

let current = read();
try {
  fs.watch(STORE_DIR, () => { current = read(); });
} catch (e) { console.error(`[login] cannot watch ${STORE_DIR}: ${e.message}`); }

/** ISO expiry of this machine's interactive login, or null when there is none. */
export const loginExpiresAt = () => current;
