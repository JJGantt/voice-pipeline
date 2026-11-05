# Which machine runs the voice agent

The same `worker/worker.py` runs on three hosts. Whichever one claims a `voice_requests` row runs the
whole turn — Opus on the warm client, and every MCP tool the turn calls. This page is why Fly is the
primary, with the measurements behind it. The measurements were taken on 2026-10-15/16 (UTC), while the
Fly worker still ran inside `jared-voice` beside the transcriber and hub turns were split between a fast
Sonnet first responder and an Opus escalation; re-run them before acting on the numbers.

## Placement

**Fly is primary, the Pi second, the Mac last.** `WORKER_PRIORITY=fly,pi,mac` is set in one place,
the default in `worker/voice_worker/config.py`; no host overrides it, so changing it there changes it
everywhere.

| | Fly (`jared-voice-worker`) | Pi | Mac |
|---|---|---|---|
| launched by | `fly-worker/entrypoint.sh`, which restarts the worker whenever it exits | systemd `voice-worker` → `run.sh` | launchd `com.jaredgantt.voice-worker` → `run.sh` |
| hardware | `shared-cpu-1x`, 2GB, no swap, region `iad`, 1GB volume at `/data` | Raspberry Pi 5, 4 cores, 4GB + 2GB swap | M1, 8 cores |
| deploy | `fly-worker/deploy.sh` | `pull-repos` + service restart | `pull-repos` + launchd restart |

The worker has a Fly app of its own because inside `jared-voice` every deploy of either the worker or
the transcriber restarts both, and they share one CPU.

In the month measured with the Pi first (Pi 191 turns, Mac 1, Fly 20), Fly claimed only during two
Pi-side outages: the Pi's worker running but unable to reach Supabase (DNS failures, TLS EOFs, read
timeouts, so its heartbeat went stale), and the Pi's `/usr/local/bin/claude` link missing while a CLI
self-update replaced it, so its turns fell through.

## How Claude runs on each host

The code is the same everywhere; only the environment differs.

- **Hub turns:** one warm `ClaudeSDKClient` (`SdkRunner`), Opus at its default effort with the full
  toolset, driving the INSTALLED CLI (`cli_path=shutil.which("claude")`). It connects at worker start
  (`SDK.warm`) and again right after any recycle, then stays up for the life of the process (each turn
  is a `/clear` on it). Full-agent turns are a fresh `claude -p --model opus` (`run_claude`).
- **Billing:** the Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) on all three. `worker.py` pops
  `ANTHROPIC_API_KEY` at import, before any client or subprocess starts, so no turn inherits one; only
  the usage-limit fallback (`SDK_CREDITS`, see CLAUDE.md) passes it on.
- **Session transcripts:** on the Pi and Mac they land in `~/.claude/projects/` and persist. On Fly
  `CLAUDE_CONFIG_DIR=/data/claude`, on the volume, pruned to 30 days by the entrypoint's daily loop.
  `STATE_DIR` is `/data/voice-worker`, also on the volume.
- **Worker logs:** the Pi's are in `journalctl -u voice-worker` (retained back to 2026-10-02 when
  measured); the Mac's in `worker/worker.out.log` (back to June). Fly's are in `/data/worker.log`
  (everything the machine prints, trimmed daily to its last 50,000 lines); `fly logs -a
  jared-voice-worker` holds only the last 100. The Fly numbers below come from `voice_requests`
  rather than worker logs.
- **No session host on Fly.** The machine runs `entrypoint.sh` and `worker.py` — no orchestrator, no
  desk host, and `worker_presence` has no Fly desk row. Its failures go to the ops responder like every
  worker's, which runs the investigation elsewhere. `start_session` still works from Fly because it is a database write that the Pi's or
  the Mac's desk host picks up.

**Every host keeps Claude Code current.** The Mac and the Pi run the native install
(`~/.local/bin/claude` → `~/.local/share/claude/versions/<v>`, `/usr/local/bin/claude` linked to it),
which updates itself version by version. Fly's sits in a Docker layer and does not update itself (it
was still 2.1.197 while the others ran 2.1.301), so `fly-worker/deploy.sh` bakes the newest release
into each image and `fly-worker/entrypoint.sh` updates a running machine daily, into its own prefix,
moving the `claude` link only once the new one runs. The model aliases `sonnet` / `opus` are resolved
by the CLI, so a stale CLI is also a different model under the same name.

## What each host can and cannot do

MCP servers a turn gets come from `write_mcp_config()`. Measured by spawning each server's command
with the worker's environment and timing `initialize` and `tools/list` over stdio (read-only; no tool
called). Seconds are to `initialize`, one cold run then warm runs on Fly, one run elsewhere:

| server | Pi | Mac | Fly |
|---|---|---|---|
| data (65 tools) | 0.86s | 0.20s | 2.20s cold, 0.52–0.59s warm |
| voice (4) | 0.59s | 0.17s | 0.34–0.44s |
| govee-lights (5) | 1.16s | 0.43s | 0.75–0.95s |
| petlibro-feeder (4) | 3.70s | 0.48s | **absent** — registered only where its files exist |
| gmail (8) | 2.10s | present | in the image (own venv, mcp 2.x); registered only while the three `GOOGLE_*` secrets are set |
| messages (HTTP) | via `http://Jareds-MacBook-Air.local:27187/mcp` | loopback | over Tailscale to the Mac's tailnet address (the `MESSAGES_TAILNET_URL` secret), only while `TAILSCALE_AUTHKEY` is set and the tailnet came up at boot |

Everything else that differs, from the code (`grep -rn WORKER_ID worker/voice_worker`):

- **iMessage:** only reachable while the Mac is awake, from any host. `.local` does not resolve in
  Fly and the Mac's port is not on the internet, so Fly joins the tailnet (`entrypoint.sh`, node
  `jared-voice-worker`, state on the volume) and talks to the Mac's tailnet address. Without the
  `TAILSCALE_AUTHKEY` secret, or if `tailscale up` fails at boot, the server is simply not registered
  until the next restart.
- **Feeder:** the PetLibro server is LAN-only (tinytuya), so even with the right path Fly could not
  reach the feeder without a route into the home network.
- **Lights:** on Fly every light command goes to the Govee cloud API. The Dockerfile sets
  `GOVEE_LAN=0` so `govee_mcp.py` skips its LAN multicast scan, which can never answer there and
  otherwise costs ~1.2s of every command before the cloud is tried.
- **Full-agent (coding) mode:** off on Fly by design — `session_agent = ag["mode"] and WORKER_ID != "fly"`.
- **Inject into a terminal session:** goes to the orchestrator at `ORCH_URL` (default
  `localhost:7682`), which only the Mac runs; any other worker hands the request off on failure.
- **Mac-only housekeeping:** `publish_repo_dirs()` (the agent cwd picker) and the Mac mic's listener
  supervisor.
- **Photos:** work everywhere — attachments are fetched from Supabase storage into a temp dir.
- **Clock:** Fly runs with `TZ=America/New_York` (`fly-worker/fly.toml`); without it, naive local
  timestamps are written four hours late.

## Speed

Claim latency is `claimed_at − created_at`; run time is `updated_at − claimed_at` on rows that ended
`done`, from `voice_requests` 2026-09-15 → 2026-10-16. Turn internals are the worker's `timing:` and
`warm client up` log lines.

| | Pi | Mac | Fly |
|---|---|---|---|
| claim latency, p50 | 1.2s (n=191, p90 2.7s) | 1.2s (n=1) | 0.5–1.4s on ordinary turns; three re-queued rows waited 86–157s behind the Pi's failures and are not a claim cost |
| run, all done turns, p50 | 10.0s (n=188, p90 31.1s) | — | 24.8s (n=20, p90 53.4s) |
| run, set-logging turns (fast path) | p50 9.2s (n=36) | — | p50 10.7s (n=8; 7.4–11.7s warm, 28.2s on the first turn after a deploy) |
| run, nutrition turns (Opus escalation) | p50 23.9s (n=54) | — | 23.8, 37.8, 46.0, 53.4, 58.1s (n=5) |
| warm SDK client connect | p50 2.5s (n=11, 1.5–8.2s) | p50 4.5s (n=6) | 5.2s (n=1) |
| Opus spawn → MCP ready | p50 3.5s (n=106) | p50 3.7s (n=332, Aug) | no log retained |
| Opus first token after ready | p50 3.2s | p50 2.9s | no log retained |
| Opus spawn total | p50 17.0s | p50 9.9s (Aug; turns differed then) | no log retained |
| new-connection GET to Supabase | 0.20–0.28s | 0.19–0.27s | 0.16–0.21s |
| GET `api.anthropic.com` | 0.10–0.16s | 0.10–0.20s | 0.08s |

**Controlled comparison, 2026-10-16** — the same command sent to one host at a time (routed by
pre-filling `failed_on` with the others), both hosts on CLI 2.1.301 with the client connected at
startup. Run time as above:

| | Pi | Fly |
|---|---|---|
| "how many items are on my todo list" (Sonnet fast path, one read tool), 5 runs each, alternating | 6.1 4.2 4.3 4.7 4.4 → **p50 4.4s** | 2.8 2.8 2.6 3.4 2.7 → **p50 2.8s** |
| "stage a banana" (Opus escalation), clean runs only | 14.9, 15.1, 15.6s | 14.3, 15.7, 16.5, 17.1s |
| first fast-path turn after a restart | 9.9s (client connecting on the turn) | 19.7s connecting on the turn; 15.0s connected at startup but straight after a CLI update |

Three Opus runs in a later batch are excluded: the earlier test turns were in the replayed
conversation, and only three of six staged anything, so some answered from context without
escalating. The 15.0s first turn on Fly came straight after its CLI moved from 2.1.197 to 2.1.301; a
new CLI version changes the system prompt, so nothing was cached for it yet (inferred, not isolated).

What these say, and how far:

- **On the fast path Fly is faster than the Pi**: 2.8s against 4.4s for the same command, five runs
  each. The historical gap (24.8s against 10.0s) was cold clients after deploys, and turns that
  waited behind the Pi's failures — not the machine.
- **Escalated (Opus) turns cost the same on both**, about 15s. The CLI's own start to ready-for-tools
  was 3.9–4.0s on Fly against 2.9–6.8s on the Pi; what varies most is Anthropic's time to first
  token (2.0s and 10.5s on two otherwise identical Fly runs).
- **Region does not favour any host.** Supabase is `West US (North California)`; Fly is `iad`, the
  home machines are on the US east coast, and all three pay about 0.2s per new connection to it.
- **These Fly numbers were taken on a vCPU shared with transcription**: `server.py` held ~225MB and ran
  Whisper / ECAPA on the same core (idle load average 0.2–0.3, ~1.5GB of the 2GB available). The worker
  now has its own machine; it has not been re-measured there.

## Two constraints that keep MCP tools present on Fly

A turn whose MCP server is missing answers that the tool does not exist. Two things prevent it
(commit `3d881b2`):

1. **`mcp<2` is pinned in the Dockerfile.** `mcp` 2.x removed `mcp.server.fastmcp`, so `govee_mcp.py`
   dies on import under it.
2. **`alwaysLoad: true` on every server in `write_mcp_config()`.** Claude Code under `-p` connects
   `--mcp-config` servers in the background, so the model's first request carries only the servers that
   have finished starting; a slow one (the data server is 2.2s cold on Fly) loses that race. `alwaysLoad`
   makes the CLI connect them before the first turn.

**Still latent, not fixed:**

- Nothing runs a tool check on Fly, so the next server that breaks there will again be found by a user
  asking for something. `worker/tool-check.py` calls one read-only tool on every server
  `write_mcp_config()` registers, but it is not in the Fly image; shipping it and running it at worker
  start, with an alert on a failure, would catch this class.
- `claude-agent-sdk` (the Python package) is unpinned in the image, so it is whatever the cached pip
  layer holds.

## Why Fly is primary

Decision: iMessage, Gmail and the feeder are rare enough that they do not count in this decision, so
Fly lacking them does not count against it.

- **It answers the common turn faster**: 2.8s against the Pi's 4.4s on the same command, and Opus
  turns cost the same on both.
- **It is independent of the house.** Both incidents in the month measured were home-side: the Pi
  losing its path to Supabase for ~1.5h, and the Pi's CLI disappearing mid-update.
- **It costs nothing extra**: every turn is on the subscription wherever it runs.

The Pi and then the Mac are the fallbacks: a Fly worker that stops heartbeating, reports itself
degraded or fails a turn without acting hands the command to the next healthy worker
(`worker/README.md` § Handing a failed turn on). A command that needs the feeder is answered on Fly without it;
Gmail and iMessage reach Fly's Opus as described above.
