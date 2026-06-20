#!/usr/bin/env node
// Voice orchestrator daemon.
//
// HTTP + WS server (bound 0.0.0.0:PORT so the Pi can reach it over LAN/Tailscale).
// Every request is token-gated (?token= or X-Token). Endpoints:
//   GET  /term     (websocket)  pipe the active pty <-> client bytes
//   POST /inject   ({text,submit?}) write text (and optional Enter) to active pty
//   GET  /health   ok
//
// Background loops: focus watcher (voice_focus id=1) + session registry.
import http from 'node:http';
import { WebSocketServer } from 'ws';
import { config } from './config.js';
import { attachClient, handleClientMessage, inject, hasActive } from './pty.js';
import { startFocusWatcher } from './focus.js';
import { startRegistry } from './registry.js';

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
  if (!config.SUPABASE_KEY) {
    console.error(
      '[orchestrator] WARNING: SUPABASE_KEY is empty — focus watcher & registry will fail. Set it in the environment.'
    );
  }
}

server.listen(config.PORT, '0.0.0.0', () => {
  preflight();
  console.log(`[orchestrator] listening on 0.0.0.0:${config.PORT}`);
  startFocusWatcher();
  startRegistry();
});

// Graceful-ish shutdown for launchd.
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    console.log(`[orchestrator] ${sig} — exiting`);
    process.exit(0);
  });
}
