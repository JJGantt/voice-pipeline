# mcp-voice

A small stdio MCP server, **`voice`**, for the voice system's live modes. It runs directly with
`node` (no build step), on the same `@modelcontextprotocol/sdk` stdio pattern as the `data` MCP in
`data-layer`.

## Tools

All four read or write Supabase `app_settings` (`src/routing.js`):

| tool | does |
|---|---|
| `get_routing` | reports that every turn runs on Opus with the full toolset, and whether always-on (`wakefree_enabled`) and voice-inject (`voice_inject_mode`) are on |
| `set_always_on` (`on`) | writes `wakefree_enabled` |
| `set_inject_mode` (`on`) | writes `voice_inject_mode` |
| `set_jaredbot` (`on`) | writes `jaredbot_enabled`, which messages-mcp's JaredBot reads before every decision |

A tool that fails answers `isError: true` with `Error: <message>`.

## Errors

It reports to the ops responder (health/responder) as machine `OPS_MACHINE`, else `fly` when
`FLY_APP_NAME` is set, else `mac` / `pi` by platform, service `voice-mcp`. Every tool failure is a
`console.error`, which the reporter sends. `src/ops_report.cjs` is a copy of the responder's
`report/report.js` (edit it there and run its `bin/sync-reporters.sh`). It never beats: it lives only as
long as the session that started it. The reporter writes only to stderr, because stdout is the MCP
transport.

## Environment

- `SUPABASE_URL` — `https://felyggqjjhltwokdfhop.supabase.co`
- `SUPABASE_KEY` — the `servers` secret key
- `OPS_TOKEN`, `OPS_URLS` — from the environment or `~/.config/ops/env`; on Fly the worker's secrets,
  which the MCP subprocess inherits

## Running

```bash
npm install
SUPABASE_URL=... SUPABASE_KEY=... node src/index.js   # stdio MCP server
```

## Registration

Registered for interactive Claude sessions in `~/.claude.json` under `mcpServers.voice` (stdio,
`command: node`, args pointing at `src/index.js`, env `{SUPABASE_URL, SUPABASE_KEY}`).

The worker also attaches it to every Claude run it spawns, from `VOICE_MCP_JS` (the worker's
`voice_worker/runners.py` writes the `--mcp-config` file). On Fly it is copied into the
`jared-voice-worker` image by `fly-worker/deploy.sh`.
