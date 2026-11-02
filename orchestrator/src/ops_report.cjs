// Error reporting and heartbeats for a Node service. COPIED into each service from
// health/responder/report/report.js — edit it there and copy it out again (health/responder/README.md).
//
//   const ops = require('./report');
//   ops.install({ machine: 'fly', service: 'status-hub' });   // uncaught errors + console.error
//   ops.report(err, { context });                              // an error caught and handled
//   ops.report(msg, { machine, service, stack });              // on behalf of a client that sent it here
//   ops.beat(60);                                              // from the loop that does the work
//
// Env: OPS_TOKEN (required to send), OPS_URLS (comma list, first that answers wins; default the Fly
// responder), OPS_SPOOL (a file that holds what could not be sent, so a crash or an outage loses
// nothing; default in the OS temp dir). OPS_TOKEN and OPS_URLS fall back to ~/.config/ops/env, which the
// Mac and the Pi both have.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const util = require('node:util');

// ~/.config/ops/env, written to the Mac and the Pi by bin/install-standby.sh, so a service there needs
// nothing in its own environment. A value set in the environment wins.
function machineEnv() {
  try {
    return Object.fromEntries(fs.readFileSync(path.join(os.homedir(), '.config/ops/env'), 'utf8').split('\n')
      .map((l) => l.trim()).filter((l) => l && !l.startsWith('#') && l.includes('='))
      .map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
  } catch { return {}; }
}
const ENV = { ...machineEnv(), ...process.env };
const URLS = (ENV.OPS_URLS || 'https://ops-responder.fly.dev').split(',').map((s) => s.trim()).filter(Boolean);
const TOKEN = ENV.OPS_TOKEN || '';
let MACHINE = process.env.OPS_MACHINE || os.hostname();
let SERVICE = process.env.OPS_SERVICE || path.basename(process.argv[1] || 'node');
let SPOOL = process.env.OPS_SPOOL || '';
const origError = console.error.bind(console);
// Set by install() when this process is not the service it names; see there.
let off = false;

// The same message repeated inside a short window is sent once, with its count. A service failing
// every few seconds is one problem; the responder counts it either way, this only spares the network.
const COALESCE_MS = 30e3;
const recent = new Map();

function spoolPath() { return SPOOL || path.join(os.tmpdir(), `ops-spool-${SERVICE.replace(/[^\w.-]/g, '_')}.jsonl`); }

function spoolAppend(kind, item) {
  try { fs.appendFileSync(spoolPath(), JSON.stringify({ kind, item }) + '\n'); } catch { /* nowhere left to put it */ }
}

async function post(kind, items) {
  if (off) return true;
  if (!TOKEN) return false;
  for (const u of URLS) {
    try {
      const r = await fetch(`${u}/${kind}`, {
        method: 'POST', signal: AbortSignal.timeout(8000),
        headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(items),
      });
      if (r.ok) return true;
    } catch { /* the next one */ }
  }
  return false;
}

let flushing = false;
// Whatever an outage left behind goes out with the next successful send.
async function flushSpool() {
  if (flushing) return;
  flushing = true;
  try {
    const p = spoolPath();
    if (!fs.existsSync(p)) return;
    const lines = fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
    const errors = lines.filter((l) => l.kind === 'error').map((l) => l.item);
    if (!errors.length || await post('error', errors)) fs.rmSync(p, { force: true });
  } finally { flushing = false; }
}

async function send(kind, item) {
  if (await post(kind, [item])) { flushSpool(); return true; }
  // A missed beat is not worth keeping: the next one replaces it, and its absence is the signal.
  if (kind === 'error') spoolAppend(kind, item);
  return false;
}

function report(err, extra = {}) {
  const message = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
  const fp = `${extra.service || ''}|${message.split('\n')[0].replace(/\d+/g, 'N').slice(0, 200)}`;
  const now = Date.now();
  const seen = recent.get(fp);
  if (seen && now - seen.at < COALESCE_MS) { seen.count += 1; return Promise.resolve(true); }
  if (seen && seen.count > 1) {
    send('error', { machine: seen.machine, service: seen.service, message: `${seen.message} (x${seen.count - 1} more in ${Math.round((now - seen.at) / 1000)}s)`, count: seen.count - 1 });
  }
  recent.set(fp, { at: now, count: 1, message, machine: extra.machine || MACHINE, service: extra.service || SERVICE });
  if (recent.size > 500) recent.delete(recent.keys().next().value);
  return send('error', { machine: extra.machine || MACHINE, service: extra.service || SERVICE, message, at: new Date(now).toISOString(),
    stack: err instanceof Error ? err.stack : extra.stack, context: extra.context });
}

function beat(everyS = 60, info) {
  return send('beat', { machine: MACHINE, service: SERVICE, every_s: everyS, info });
}

// Crash the way Node would have, after giving the report a moment to leave.
function fatal(err) {
  origError(err);
  const t = setTimeout(() => process.exit(1), 3000);
  report(err).finally(() => { clearTimeout(t); process.exit(1); });
}

function install(opts = {}) {
  MACHINE = opts.machine || MACHINE;
  SERVICE = opts.service || SERVICE;
  SPOOL = opts.spool || SPOOL;
  // 'fly' names the live service, and Fly sets FLY_APP_NAME on every machine. Without it this is the
  // same code loaded somewhere else (a script on the Mac requiring server.js), and it would file its
  // failures, and its beats, as the live service's. So it sends nothing.
  if (MACHINE === 'fly' && !process.env.FLY_APP_NAME) {
    off = true;
    origError(`[ops] ${SERVICE} names machine 'fly' but FLY_APP_NAME is unset: not reporting`);
  }
  process.on('uncaughtException', fatal);
  process.on('unhandledRejection', (e) => fatal(e instanceof Error ? e : new Error(String(e))));
  console.error = (...args) => {
    origError(...args);
    const err = args.find((a) => a instanceof Error);
    const text = util.format(...args);
    report(err ? Object.assign(new Error(text), { name: err.name, stack: err.stack }) : text);
  };
  flushSpool();
}

module.exports = { install, report, beat };
