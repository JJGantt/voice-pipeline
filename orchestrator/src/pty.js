// Active-pty manager. At most one pty is "active" at a time. Its output is
// broadcast to every connected /term websocket client; client bytes are
// written back into the pty. Clients can resize it.
import { spawn } from 'node-pty';
import { config } from './config.js';

let active = null; // { pty, clients:Set<ws>, id, cwd }

export function hasActive() {
  return active != null;
}

export function activeInfo() {
  return active ? { id: active.id, cwd: active.cwd } : null;
}

// Spawn `claude --resume <id>` in cwd and make it the active pty, replacing
// (and killing) any previous one.
export function startSession(id, cwd) {
  killActive();
  const pty = spawn(config.CLAUDE_BIN, ['--resume', id], {
    name: 'xterm-256color',
    cols: config.PTY_COLS,
    rows: config.PTY_ROWS,
    cwd: cwd || process.env.HOME,
    env: process.env,
  });

  const clients = new Set();
  active = { pty, clients, id, cwd };

  pty.onData((data) => {
    for (const ws of clients) {
      if (ws.readyState === ws.OPEN) {
        try {
          ws.send(data);
        } catch {
          /* drop */
        }
      }
    }
  });

  pty.onExit(() => {
    if (active && active.pty === pty) {
      for (const ws of clients) {
        try {
          ws.send('\r\n[session ended]\r\n');
        } catch {
          /* ignore */
        }
      }
      active = null;
    }
  });

  return active;
}

export function killActive() {
  if (!active) return;
  try {
    active.pty.kill();
  } catch {
    /* already gone */
  }
  active = null;
}

// Attach a websocket client to the active pty (or register it so it gets a
// notice + future output if a pty starts later).
export function attachClient(ws) {
  if (!active) {
    // No pty: hold the connection open with a notice. It will NOT receive
    // output until a session starts and a fresh /term connection is made,
    // but keeping it open lets the Pi reconnect logic stay simple.
    try {
      ws.send('\r\n[orchestrator: no active session — focus a session to begin]\r\n');
    } catch {
      /* ignore */
    }
    return;
  }
  active.clients.add(ws);
  ws.on('close', () => {
    if (active) active.clients.delete(ws);
  });
}

// Handle an inbound message from a /term client. Resize messages are JSON
// {type:'resize', cols, rows}; everything else is raw keystroke bytes.
export function handleClientMessage(ws, data) {
  if (!active) return;
  // Try to parse a control frame; if it's not one, treat as raw input.
  if (typeof data === 'string' && data.startsWith('{')) {
    try {
      const msg = JSON.parse(data);
      if (msg && msg.type === 'resize') {
        const cols = Math.max(1, parseInt(msg.cols, 10) || config.PTY_COLS);
        const rows = Math.max(1, parseInt(msg.rows, 10) || config.PTY_ROWS);
        active.pty.resize(cols, rows);
        return;
      }
    } catch {
      /* fall through to raw write */
    }
  }
  active.pty.write(typeof data === 'string' ? data : data.toString('utf8'));
}

// Inject text (and optionally Enter) into the active pty. Returns true if
// there was an active pty to inject into.
export function inject(text, submit) {
  if (!active) return false;
  active.pty.write(text);
  if (submit) active.pty.write('\r');
  return true;
}
