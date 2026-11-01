#!/usr/bin/env node
// Voice orchestrator daemon.
//
// HTTP + WS server (bound 0.0.0.0:PORT so the Pi can reach it over LAN/Tailscale).
// Every request is token-gated (?token= or X-Token). Endpoints:
//   GET  /term     (websocket)  pipe the active pty <-> client bytes
//   POST /inject   ({text,submit?}) write text (and optional Enter) to active pty
//   GET  /health   ok
//
// Background loops: focus watcher (voice_focus id=1) + session registry + desk control
// (publish ~/repos to Supabase, drain the hub's launch queue) + desk session reconcile (is each
// dispatched session still up, and what did it last say).
import http from 'node:http';
import { createRequire } from 'node:module';
import { WebSocketServer } from 'ws';
import { config } from './config.js';
import { attachClient, handleClientMessage, inject, hasActive } from './pty.js';
import { startFocusWatcher } from './focus.js';
import { startRegistry } from './registry.js';
import { markTtyRead } from './deskSessions.js';
import { startRepos } from './repos.js';
import { startCommands } from './commands.js';
import { startDeskSessions } from './deskSessions.js';

// Errors go to the ops responder (health/responder) through a copy of its reporter. The orchestrator is
// event-driven, so the event loop is its work loop: a beat from a timer on it stops when the loop is
// blocked or the process is gone. The Mac sleeps with its lid; the responder knows not to chase that.
const ops = createRequire(import.meta.url)('./ops_report.cjs');
ops.install({ machine: 'mac', service: 'orchestrator' });
setInterval(() => ops.beat(60), 60e3);
ops.beat(60);

// ---- auth -----------------------------------------------------------------

function tokenOf(req, url) {
  const header = req.headers['x-token'];
  if (header) return header;
  return url.searchParams.get('token');
}

function authed(req, url) {
  return tokenOf(req, url) === config.ORCH_TOKEN;
}

// ---- HTTP -----------------------------------------------------------------

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);

  if (!authed(req, url)) {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'unauthorized' }));
    return;
  }

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, active: hasActive() }));
    return;
  }

  // WHICH SESSION TAB IS ON SCREEN, reported by Hammerspoon the moment it changes — Terminal being
  // activated, a window focused, a tab's title changing (session-read.lua). Looking at the tab is what
  // clears the terminal's unread bell, so it is also what marks the session's reply read everywhere
  // else: the phone drops its amber within a second, off the realtime push the write makes.
  //
  // Here rather than in the hub because the mapping from a tty to a session row is this machine's own
  // knowledge, and localhost needs no hub credential in a Hammerspoon config.
  if (req.method === 'POST' && url.pathname === '/session-read') {
    const tty = String(url.searchParams.get('tty') || '');
    markTtyRead(tty).catch((e) => console.error('[orchestrator] session-read:', e.message));
    res.writeHead(204).end();   // fire and forget: nothing on the other end waits on the answer
    return;
  }

  if (req.method === 'POST' && url.pathname === '/inject') {
    let body = '';
    req.on('data', (c) => {
      body += c;
      if (body.length > 1_000_000) req.destroy(); // guard
    });
    req.on('end', () => {
      let parsed;
      try {
        parsed = JSON.parse(body || '{}');
      } catch {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid json' }));
        return;
      }
      const text = typeof parsed.text === 'string' ? parsed.text : null;
      if (text == null) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'missing text' }));
        return;
      }
      if (!hasActive()) {
        res.writeHead(409, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'no active pty' }));
        return;
      }
      const ok = inject(text, parsed.submit === true);
      if (!ok) {
        res.writeHead(409, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'no active pty' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'not found' }));
});

// ---- WebSocket (/term) ----------------------------------------------------

const wss = new WebSocketServer({ noServer: true });

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (url.pathname !== '/term' || !authed(req, url)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit('connection', ws, req);
  });
});

wss.on('connection', (ws) => {
  attachClient(ws);
  ws.on('message', (data, isBinary) => {
    handleClientMessage(ws, isBinary ? data : data.toString('utf8'));
  });
});

// ---- boot -----------------------------------------------------------------

function preflight() {
  // An empty token would let a request with no token through authed()'s equality check.
  if (!config.ORCH_TOKEN) {
    console.error('[orchestrator] ORCH_TOKEN is not set (EnvironmentVariables in the launchd plist); refusing to start.');
    process.exit(1);
  }
  if (!config.HUB_TOKEN) console.error('[orchestrator] HUB_TOKEN is not set: session activity will not reach the hub.');
  if (!config.TERM_BASE) console.error('[orchestrator] TERM_BASE is not set: the terminal_url it advertises will not resolve.');
  if (!config.SUPABASE_KEY) {
    console.error(
      '[orchestrator] WARNING: SUPABASE_KEY is empty — focus watcher, registry & desk control will fail. Set it in the environment.'
    );
  }
}

server.listen(config.PORT, '0.0.0.0', () => {
  preflight();
  console.log(`[orchestrator] listening on 0.0.0.0:${config.PORT}`);
  startFocusWatcher();
  startRegistry();
  startRepos();
  startCommands();
  startDeskSessions();
});

// Graceful-ish shutdown for launchd.
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    console.log(`[orchestrator] ${sig} — exiting`);
    process.exit(0);
  });
}
