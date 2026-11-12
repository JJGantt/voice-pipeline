// The fast liveness beat to the ops responder: POST /live every 5s, from pi-desk on the Pi and from the
// Mac's orchestrator (health/responder/README.md). The responder holds the last beat per machine in
// memory and declares a machine DOWN after 15s of silence, then moves its sessions somewhere that is
// up (`evacuate`, contract §3-4). That is how a machine that dies mid-turn loses seconds rather than
// the minutes worker_presence staleness takes.
//
// A heartbeat is the one kind of timer that has to exist: a machine that is gone sends nothing, so its
// absence can only be read from silence. It writes nothing to the database. On the Mac, sleeping
// suspends the timer with everything else, which is correct: a sleeping Mac's sessions are frozen and
// have to move too.
//
// Fire-and-forget: a beat never waits on the one before, and a miss is logged at most once a minute,
// as a plain line. The responder notices a machine that cannot reach it; flooding it with an error per
// missed beat would only repeat what the silence already says.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const LIVE_MS = Number(process.env.LIVE_MS || 5000);
const LIVE_TIMEOUT_MS = 5000;
const MISS_LOG_MS = 60000;

// OPS_TOKEN as the ops reporter finds it (ops_report.cjs): the environment, else ~/.config/ops/env,
// which both machines have.
function opsToken() {
  if (process.env.OPS_TOKEN) return process.env.OPS_TOKEN;
  try {
    const line = fs.readFileSync(path.join(os.homedir(), '.config/ops/env'), 'utf8').split('\n')
      .map((l) => l.trim()).find((l) => l.startsWith('OPS_TOKEN='));
    return line ? line.slice('OPS_TOKEN='.length) : '';
  } catch { return ''; }
}

export function startLive(machine, { url = process.env.OPS_LIVE_URL || 'https://ops-responder.fly.dev/live' } = {}) {
  const token = opsToken();
  if (!token) {
    console.error(`[live] no OPS_TOKEN (environment or ~/.config/ops/env) — ${machine} sends no /live beat`);
    return () => {};
  }
  let lastMissLog = 0;
  const body = JSON.stringify({ machine });
  const miss = (why) => {
    if (Date.now() - lastMissLog < MISS_LOG_MS) return;
    lastMissLog = Date.now();
    console.log(`[live] /live beat missed: ${why}`);
  };
  const beat = () => {
    fetch(url, {
      method: 'POST', signal: AbortSignal.timeout(LIVE_TIMEOUT_MS),
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body,
    }).then((r) => { if (!r.ok) miss(`answered ${r.status}`); }, (e) => miss(e.message));
  };
  beat();
  const timer = setInterval(beat, LIVE_MS);
  return () => clearInterval(timer);
}
