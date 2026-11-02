# Which machine runs the voice agent

The same `worker/worker.py` runs on three hosts. Whichever one claims a `voice_requests` row runs the
whole turn — Opus on the warm client, and every MCP tool the turn calls. This page is why Fly is the
primary, with the measurements behind it. The measurements were taken on 2026-10-15/16 (UTC), while the
Fly worker still ran inside `jared-voice` beside the transcriber and hub turns were split between a fast
Sonnet first responder and an Opus escalation; re-run them before acting on the numbers.

## Placement

**Fly is primary, the Pi second, the Mac last.** `WORKER_PRIORITY=fly,pi,mac` is `worker/run.sh`'s
default (the Pi and the Mac launch through it), the Pi's `worker/.env` value, `voice_worker/config.py`'s in-file
default, and the `fly-worker/Dockerfile` env. Change all four together.

| | Fly (`jared-voice-worker`) | Pi | Mac |
|---|---|---|---|
| launched by | `fly-worker/entrypoint.sh`, which restarts the worker whenever it exits | systemd `voice-worker` → `run.sh` | launchd `com.jaredgantt.voice-worker` → `run.sh` |
| hardware | `shared-cpu-1x`, 2GB, no swap, region `iad`, 1GB volume at `/data` | Raspberry Pi 5, 4 cores, 4GB + 2GB swap | M1, 8 cores |
| deploy | `fly-worker/deploy.sh` | `pull-repos` + service restart | `pull-repos` + launchd restart |

The worker has a Fly app of its own because inside `jared-voice` every deploy of either the worker or
the transcriber restarted both, and they shared one CPU.

Turns claimed 2026-09-15 → 10-16, when the order was Pi first: Pi 191 (188 done, 3 error), Mac 1,
Fly 20. Fly took its 20 turns in two incidents, never otherwise:

- **2026-10-14 22:39Z → 10-15 01:28Z.** The Pi's worker was running but could not reach Supabase:
  its journal shows `UPSERT worker_presence failed` with DNS failures (`Temporary failure in name
  resolution`), TLS EOFs and read timeouts from 22:55Z on. Its heartbeat went stale and Fly claimed.
- **2026-10-16 ~02:02Z → 02:51Z.** The Pi's CLI was gone: from 01:55Z every Opus escalation failed
  with `No such file or directory: '/usr/local/bin/claude'` (the `~/.npm-global/bin/claude` link the
  wrapper points at was re-created at 02:03:19Z, consistent with a self-update replacing it). Those
  turns fell through to Fly. Part of this window was also failover testing by another session.

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
  jared-voice-worker` holds only the last 100. Before the volume, a Fly restart erased its history,
  which is why the Fly numbers below come from `voice_requests` rather than worker logs.
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
- **Lights:** `govee_mcp.py` tries a LAN multicast scan first (1.2s timeout) and falls back to the
  Govee cloud API. On Fly the scan can never answer, so every light command is cloud-only and pays
  the scan timeout first (from the code; not timed on Fly).
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
  home machines are in Florida, and all three pay about 0.2s per new connection to it.
- **These Fly numbers were taken on a vCPU shared with transcription**: `server.py` held ~225MB and ran
  Whisper / ECAPA on the same core (idle load average 0.2–0.3, ~1.5GB of the 2GB available). The worker
  now has its own machine; it has not been re-measured there.

## The missing tools on 2026-10-14/15

Two turns claimed by Fly answered that a tool did not exist:

- 23:49:55Z, "Make the light, the floor lamp white and 30% brightness" → "I checked my available tools
  and I don't actually have a working lighting-control function…"
- 00:44:35Z, "114 grams of Honey Nut Cheerios…" → "I don't have a working nutrition-logging tool…"

They had two separate causes. Both are recorded in commit `3d881b2` (2026-10-15 01:15Z), the fix the
investigation session made:

1. **No lights:** the image installed the `mcp` Python package unpinned, the rebuild for release v112
   (2026-10-14 22:37Z) pulled 2.x, and 2.x removed `mcp.server.fastmcp`, so `govee_mcp.py` died on
   import. The fix pins `mcp<2` in the Dockerfile. Fly has `mcp 1.30.0` now, and govee starts there
   (table above).
2. **No nutrition tool:** Claude Code under `-p` connects `--mcp-config` servers in the background, so
   the model's first request carried only the servers that had finished starting. log_nutrition is
   gated, so the turn ran on a fresh Opus spawn, and on Fly the data server (2.2s cold, measured above)
   lost that race while the set-logging turns earlier that evening, on the warm client, had it. The
   fix sets `alwaysLoad: true` on every server in `write_mcp_config()`, which makes the CLI connect
   them before the first turn. Fly's 2.1.197 binary contains `alwaysLoad`.

Both shipped to Fly as releases v113/v114 (2026-10-15 01:17Z / 01:21Z), after both failures. Since
then Fly has run stage_meal, edit_staged and log_nutrition on escalated spawns (2026-10-16 02:02Z →
02:24Z) without a missing tool. That is the evidence it is fixed; it is a handful of turns.

**Still latent, not fixed:**

- Nothing runs a tool check on Fly, so the next server that breaks there will again be found by Jared
  asking for something. `worker/tool-check.py` calls one read-only tool on every server
  `write_mcp_config()` registers, but it is not in the Fly image; shipping it and running it at worker
  start, with an alert on a failure, would catch this class.
- `claude-agent-sdk` (the Python package) is unpinned in the image, so it is whatever the cached pip
  layer holds.

## Why Fly is primary

Jared's weighting (2026-10-16): iMessage, Gmail and the feeder are rare enough that they do not count
in this decision, so Fly lacking them does not count against it.

- **It answers the common turn faster**: 2.8s against the Pi's 4.4s on the same command, and Opus
  turns cost the same on both.
- **It is independent of the house.** Both incidents in the month measured were home-side: the Pi
  losing its path to Supabase for ~1.5h, and the Pi's CLI disappearing mid-update.
- **It costs nothing extra**: every turn is on the subscription wherever it runs.

The Pi and then the Mac are the fallbacks: a Fly worker that stops heartbeating, reports itself
degraded or fails a turn without acting hands the command to the next healthy worker
(`worker/README.md` § Handing a failed turn on). A command that needs the feeder is answered on Fly without it;
Gmail and iMessage reach Fly's Opus as described above.
