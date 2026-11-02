// Who may talk to the orchestrator: anything presenting ORCH_TOKEN in an `X-Token` header. Never the
// query string: a URL is what proxies, logs, shell history and the voice_focus row keep, and a token in
// one outlives the request. Callers: the voice worker's /inject, Hammerspoon's /session-read, and any
// /term websocket client (Node's `ws` sets headers on an upgrade; a browser WebSocket cannot, and no
// browser connects to /term).
import crypto from 'node:crypto';

const digest = (s) => crypto.createHash('sha256').update(String(s)).digest();

export function tokenOf(req) {
  const h = req.headers['x-token'];
  return Array.isArray(h) ? h[0] : h || '';
}

/** Constant-time; an unset `want` admits nobody. */
export function authed(req, want) {
  return !!want && crypto.timingSafeEqual(digest(tokenOf(req)), digest(want));
}
