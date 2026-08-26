# mcp-voice

A small stdio MCP server, **`voice`**, that lets any Claude session see the
focusable Claude Code sessions on this Mac and switch what the Pi/TV wall is
focused on. It is modeled on the `data` MCP in `data-layer` (same
`@modelcontextprotocol/sdk` stdio pattern), but runs directly with `node` — no
build step.

## Tools

### `say` (`{ text: string }`)
Speak ONE short line aloud to Jared — the only thing in the voice pipeline that gets played as audio.
Inserts a row into Supabase `voice_speech`; every speaker-equipped surface (the Mac listener today)
subscribes via realtime and plays it the instant it lands (Kokoro). The model calls this once per voice
turn with a single present-tense line ("changing the floor lamp to white") fired *alongside* the action
so synthesis overlaps execution; its final written reply is shown on screen but NOT spoken. Separate
from `voice_messages` (the display thread) and from the activity bar. See `../ARCHITECTURE.md`.

### `list_sessions` (no args)
Scans `~/.claude/projects/*/*.jsonl` and returns the **5 most recently active**
sessions (by file mtime), newest first:

```json
[{ "id": "...", "title": "...", "cwd": "...", "project": "...", "last_active": "ISO" }]
```

- `title` = the latest `ai-title` record's `aiTitle` in the file. Fallback: the
  first user-message snippet, else the session id.
- `cwd` = the last `cwd` field seen in the file.
- `project` = the project directory name under `~/.claude/projects/`.

Files are read via a bounded tail (256 KB) with a bounded head fallback, so it
stays fast even on multi-thousand-line transcripts.

### `set_focus` (`{ target: string }`)
`target` is either the literal `"hub"` or a fuzzy reference to one of the 5
recent sessions:

- `"hub"` (also matches "wall"/"home"/"dashboard"/"tv") → focus `'hub'`.
- `"most recent"` / `"latest"` / `"last"` → the newest session.
- otherwise → keyword/substring match against each session's title and project;
  best single match wins (it tells you when it picked among ties).

It then `PATCH`es the `voice_focus` singleton (`id=eq.1`) over Supabase REST:

```json
{ "focus": "hub | <session-id>", "label": "<title or 'Hub'>",
  "session_cwd": "<cwd or null>", "updated_by": "mcp", "updated_at": "<now>" }
```

`terminal_url` is intentionally left untouched — the orchestrator sets/clears it.
Returns a human-readable confirmation describing what got focused.

## Environment

Both come from the process env at runtime (no secrets committed):

- `SUPABASE_URL` — `https://felyggqjjhltwokdfhop.supabase.co`
- `SUPABASE_KEY` — the `life-data` project **service_role** key

## Running

```bash
npm install
SUPABASE_URL=... SUPABASE_KEY=... node src/index.js   # stdio MCP server
npm run test:list                                     # prints the 5 recent sessions
```

## Registration

Already registered for interactive Claude sessions in `~/.claude.json` under
`mcpServers.voice` (stdio, `command: node`, args pointing at
`src/index.js`, env `{SUPABASE_URL, SUPABASE_KEY}`), alongside the existing
`data` server.

The worker also attaches it to every Claude run it spawns, from `VOICE_MCP_JS`
(`worker/worker.py` writes the `--mcp-config` file), so a voice turn can speak
and switch focus. On Fly it is copied into the image by `server/assemble.sh`.
