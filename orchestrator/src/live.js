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
// Fire-and-forget: a beat never waits on the one before. A miss is an OCCURRENCE (console.warn, which the
// ops reporter counts), at most one a minute per kind, and each miss is sorted by where it failed, so a
// machine's own network blinking (a weak Wi-Fi link) is counted apart from the responder failing to answer:
//   network down  - the OS refused the connection itself (no route, no DNS, connect timed out), or a
//                   second, independent host was unreachable at the same moment. The awake twin of a
//                   closed lid: nothing to fix in code, and the 15s DOWN deadline still covers an outage.
//   responder unreachable - this machine's network reached the internet but not the responder.
//   answered <status> - the responder answered, with an error.
// The fingerprint is the first line only (responder lib/store.js), so the cause and the probe's result
// ride on a second line without splitting the count.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const LIVE_MS = Number(process.env.LIVE_MS || 5000);
const LIVE_TIMEOUT_MS = 5000;
const MISS_LOG_MS = 60000;
const PROBE_URL = 'https://1.1.1.1/cdn-cgi/trace';
const PROBE_TIMEOUT_MS = 3000;
// Error codes only this machine's own network stack produces: the connection never left it.
const LOCAL_CODES = new Set(['ENETUNREACH', 'EHOSTUNREACH', 'ENETDOWN', 'EADDRNOTAVAIL', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT']);

// Where a failed beat failed: 'network down' or 'responder unreachable'. `probeOk` is whether the
// independent host answered just after; it is only consulted when the code alone does not decide.
export function classifyMiss(code, probeOk) {
  if (LOCAL_CODES.has(code)) return 'network down';
  return probeOk ? 'responder unreachable' : 'network down';
}

function causeOf(e) {
  if (e?.name === 'TimeoutError') return 'TimeoutError';
  return e?.cause?.code || e?.cause?.name || e?.name || 'unknown';
}

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
  const lastMissLog = new Map();
  const body = JSON.stringify({ machine });
  const miss = (kind, detail) => {
    if (Date.now() - (lastMissLog.get(kind) || 0) < MISS_LOG_MS) return;
    lastMissLog.set(kind, Date.now());
    console.warn(`[live] /live beat missed: ${kind}\n${detail}`);
  };
  const failed = (e) => {
    const code = causeOf(e);
    const detail = `${e.message}; cause ${code}${e?.cause?.message ? ` (${e.cause.message})` : ''}`;
    if (LOCAL_CODES.has(code)) return miss(classifyMiss(code), detail);
    fetch(PROBE_URL, { signal: AbortSignal.timeout(PROBE_TIMEOUT_MS) }).then((r) => r.ok, () => false)
      .then((ok) => miss(classifyMiss(code, ok), `${detail}; independent host ${ok ? 'reachable' : 'unreachable'}`));
  };
  const beat = () => {
    fetch(url, {
      method: 'POST', signal: AbortSignal.timeout(LIVE_TIMEOUT_MS),
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body,
    }).then((r) => { if (!r.ok) miss(`answered ${r.status}`, url); }, failed);
  };
  beat();
  const timer = setInterval(beat, LIVE_MS);
  return () => clearInterval(timer);
}
