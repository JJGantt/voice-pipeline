# voice-orchestrator

Mac daemon for the **Voice → Claude → Pi-wall** system. It watches a Supabase
"focus" row and makes the Pi/TV show either the lightweight **hub** session or a
real Mac `claude` session piped into a web terminal (xterm.js on the Pi). It also
publishes the list of focusable sessions.

```
voice_focus(id=1).focus ──realtime──▶ orchestrator   (60s poll = backstop; 10s while the socket is down)
   = 'hub'        → kill pty, re-open the desk Terminal tab, terminal_url = null
   = <session id> → close that desk tab, spawn `claude --resume <id>` in a pty,
                    terminal_url = ws://MAC_HOST:PORT/term?token=ORCH_TOKEN

xterm.js on Pi ──ws /term?token=──▶ active pty (bytes both ways, resizable)
caller         ──POST /inject?token=── inject text (+optional Enter) into pty
```

## Run

```sh
npm install            # builds node-pty (native)
SUPABASE_KEY=<service_role> node src/server.js
```

Or install the sample launchd agent (parent does this):

```sh
cp com.jaredgantt.voice-orchestrator.plist ~/Library/LaunchAgents/
# edit SUPABASE_KEY in the plist first
launchctl load ~/Library/LaunchAgents/com.jaredgantt.voice-orchestrator.plist
```

## Config (env, with defaults)

| Var               | Default                                            |
| ----------------- | -------------------------------------------------- |
| `SUPABASE_URL`    | `https://felyggqjjhltwokdfhop.supabase.co`         |
| `SUPABASE_KEY`    | _(empty — must set; service_role key)_             |
| `PORT`            | `7682`                                             |
| `MAC_HOST`        | `Jareds-MacBook-Air.local`                         |
| `ORCH_TOKEN`      | `REDACTED`             |
| `CLAUDE_BIN`      | `/Users/jaredgantt/.local/bin/claude`              |
| `FOCUS_POLL_MS`   | `10000` (realtime backstop; 60s while it's healthy) |
| `REGISTRY_POLL_MS`| `15000`                                            |
| `REPO_ROOTS`      | `~/repos` (':'-separated)                          |
| `REPO_POLL_MS`    | `60000`                                            |
| `COMMAND_POLL_MS` | `30000` (realtime backstop)                        |
| `COMMAND_TTL_S`   | `120`                                              |
| `PRESENCE_MS`     | `30000`                                            |
| `PRESENCE_FAIL_EXIT_MS` | `300000` — presence failing this long exits the process for launchd to restart |
| `HUB_URL`         | `https://jared-status-hub.fly.dev`                 |
| `HUB_TOKEN`       | the kiosk token (live session activity → the wall) |
| `PTY_COLS`/`PTY_ROWS` | `200` / `50`                                   |
| `REGISTRY_LIMIT`  | `5`                                                |

## HTTP / WS contract

Server binds `0.0.0.0:PORT` so the Pi can reach it over LAN/Tailscale.
**Every request requires the token** — `?token=<ORCH_TOKEN>` or `X-Token` header.
Missing/wrong token → `401`.

- `GET /term` (websocket) — pipes the **current active pty** ↔ client bytes.
  If there's no active pty the socket stays open and gets a one-line notice.
  Client → server control frame: JSON `{"type":"resize","cols":N,"rows":N}`;
  any other message is treated as raw keystroke bytes.
- `POST /inject` — body JSON `{ "text": "...", "submit": true|false }`. Writes
  `text` to the active pty; if `submit` is true, also writes `\r`. `409` if no
  active pty; `400` on bad/missing body.
- `GET /health` — `{ ok: true, active: <bool> }`.

## Focus behavior (the AppleScript bits)

**focus = a session id**
1. Find the desk tab: `lsof <projects>/*/<id>.jsonl` → pid → controlling tty
   (`ps -o tty= -p <pid>`, prefixed with `/dev/`). Enumerate Terminal tabs via
   AppleScript and **close only the tab whose `tty` matches**. If no live tab is
   found (Claude Code appends-and-closes its transcript, so a holder may not
   exist at that instant), the close is **skipped** — never guesses.
2. Spawn a `node-pty` running `claude --resume <id>` with cwd =
   `voice_focus.session_cwd` (fallback: the `cwd` recorded inside the session
   file). It replaces/kills any previous active pty and streams to `/term`
   clients (default 200×50, resizable).
3. PATCH `voice_focus` id=1: `terminal_url = ws://MAC_HOST:PORT/term?token=...`.

**focus = 'hub'**
1. Kill the active pty.
2. **Re-open the desk tab right where it was** for the session that *was*
   focused (tracked as `lastFocused {id, cwd}`): if Terminal is running with a
   window, Cmd-T a new tab into the front window (System Events keystroke) and
   `do script "cd <cwd>; claude --resume <id>" in front window`; if Terminal has
   no window/isn't running, launch it and run there. The launchd daemon has no
   Accessibility grant, so the keystroke fails and this falls back to a new
   window (`applescript.js` `runInNewTerminal`).
3. PATCH `voice_focus` id=1: `terminal_url = null`.

## Session registry

Every `REGISTRY_POLL_MS`, scan `~/.claude/projects/*/*.jsonl`, take the **5 most
recent by mtime**, extract the latest `aiTitle` (→ `title`) and `cwd` plus the
project dir, and UPSERT into `voice_sessions` (PK `id`, `on_conflict=id`,
`Prefer: resolution=merge-duplicates`): `{id, title, cwd, project, last_active}`.
Rows no longer in the top-5 are DELETEd so the table always equals the current 5.

## Security

The server is **token-gated** on every HTTP and WS request and is intended for
**LAN / Tailscale only** — do not expose `PORT` to the public internet. The
token grants full keystroke injection into a live `claude` session and a raw
terminal pipe, so treat it like a credential. `SUPABASE_KEY` is the
**service_role** key (bypasses RLS) — keep it in the environment / plist, never
commit it.

## Files

- `src/server.js` — HTTP + WS server, auth, boots the loops.
- `src/focus.js` — focus watcher / reconciler.
- `src/pty.js` — single active-pty manager + client fan-out.
- `src/applescript.js` — lsof→tty, Terminal tab enumerate/close/reopen.
- `src/sessions.js` — session-file parsing + registry scan (pure helpers + FS).
- `src/registry.js` — registry publish loop.
- `src/repos.js` — the `~/repos` scan that publishes the `repos` table.
- `src/commands.js` — the `desk_commands` consumer: opens a Terminal in a project.
- `src/trust.js` — pre-accepts the workspace-trust prompt (shared with `pi-desk`).
- `src/realtime.js` — the self-healing Supabase subscription (shared with `pi-desk`).
- `src/supabase.js` — PostgREST helpers (get/patch focus, upsert/prune sessions).
- `src/config.js` — env config.
- `com.jaredgantt.voice-orchestrator.plist` — sample launchd agent.
```
