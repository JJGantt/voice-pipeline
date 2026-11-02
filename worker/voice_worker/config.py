"""Configuration: everything the worker reads from its environment, and the fixed timings and tool lists."""

import os
import re
from pathlib import Path




SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co"
).rstrip("/")


# REQUIRED: the service_role key (bypasses RLS — needs full read/write on the
# voice_* tables and worker_presence). There is intentionally no hardcoded
# default; see README.
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


WORKER_ID = os.environ.get("WORKER_ID", "mac")


# Ordered highest-priority first. A worker only claims if no HIGHER-priority
# worker has a fresh heartbeat.
WORKER_PRIORITY = [
    w.strip() for w in os.environ.get("WORKER_PRIORITY", "fly,pi,mac").split(",") if w.strip()
]


# The one worker that claims status `fallback`: commands the watch sent through Supabase's
# `voice-fallback` edge function (data-layer) because the hub on Fly did not answer. The Pi, because it is
# the worker that does not run on Fly and does not sleep.
FALLBACK_WORKER = os.environ.get("FALLBACK_WORKER", "pi")


# Within this long of an earlier fallback command, it is the same outage: no second troubleshooting session.
FALLBACK_OUTAGE_S = 30 * 60


STATE_DIR = Path(os.path.expanduser(os.environ.get("STATE_DIR", "~/.voice-worker")))


CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


# Every turn runs on the Claude subscription. The Console key is used for exactly one case: a hub turn
# from the watch, phone or web (never a room mic) refused because the subscription's usage limit is hit,
# which is re-run on the key so nutrition and lists keep working until the window resets (owner's
# decision). The Max plan's $100/month of Console credits pays for it. An overloaded or erroring
# subscription is retried and surfaced, never moved onto the key: a 529 there becomes a Console-billed
# Opus turn, and noise judging on the key drains the Console balance.
#
# The key is popped from the environment at import, so no subprocess inherits it and quietly bills it;
# only the credits runner (SDK_CREDITS) passes it on, explicitly.
CREDITS_API_KEY = os.environ.pop("ANTHROPIC_API_KEY", None) or ""


# Every hub turn runs on ONE model with the full toolset: Opus at its default effort, which is adaptive —
# it thinks as hard as the turn needs. Quality over speed, by decision: there is no fast
# first responder and no escalation. Low effort was measurably worse on macro estimates and `max` bought
# nothing over the default (docs/ROUTING.md). Override with HUB_MODEL / HUB_EFFORT (empty = the default).
HUB_MODEL = os.environ.get("HUB_MODEL", "opus")


HUB_EFFORT = os.environ.get("HUB_EFFORT", "").strip() or None


# Path to the built data MCP server (node entrypoint).
DATA_MCP_JS = os.environ.get(
    "DATA_MCP_JS", "/Users/jaredgantt/repos/data-layer/dist/mcp.js"
)


# Path to the voice-control MCP (set_focus / list_sessions) so the hub session can
# switch what the wall is focused on ("switch to the nutrition session").
VOICE_MCP_JS = os.environ.get(
    "VOICE_MCP_JS", "/Users/jaredgantt/repos/voice-pipeline/mcp-voice/src/index.js"
)


# Govee lights MCP so voice can control the lights ("turn the lights blue").
GOVEE_PY = os.environ.get("GOVEE_PY", "/opt/homebrew/bin/python3.11")


GOVEE_MCP_PY = os.environ.get("GOVEE_MCP_PY", "/Users/jaredgantt/repos/govee-mcp/govee_mcp.py")


# PetLibro cat-feeder MCP (local LAN control: feed the cats / status / food level / schedule).
# Uses the project venv python (has tinytuya); the feeder is LAN-only, so this only works from a
# host on the home network (Mac primary, Pi fallback — Pi overrides the paths via env).
PETLIBRO_PY = os.environ.get("PETLIBRO_PY", "/Users/jaredgantt/repos/petlibro-mcp/.venv/bin/python")


PETLIBRO_MCP_PY = os.environ.get("PETLIBRO_MCP_PY", "/Users/jaredgantt/repos/petlibro-mcp/petlibro_mcp.py")


# Gmail MCP (search, read, draft, send). Plain HTTPS against the Gmail API with the refresh token in
# its own .env, so it runs anywhere its checkout exists — the Pi overrides the paths via env.
GMAIL_PY = os.environ.get("GMAIL_PY", "/Users/jaredgantt/repos/gmail-mcp/.venv/bin/python")


GMAIL_MCP_PY = os.environ.get("GMAIL_MCP_PY", "/Users/jaredgantt/repos/gmail-mcp/gmail_mcp.py")


# iMessage MCP, always over HTTP. It reads chat.db, which needs Full Disk Access, and only the Mac's
# messages-http launchd job holds that — a stdio copy spawned by this worker would start fine and fail
# every read. So the Mac talks to its own job on loopback and the Pi sets the Mac's address in .env.
# Fly reaches it over Tailscale: entrypoint.sh sets the URL only once the tailnet is up.
# Empty (Fly without Tailscale, or a Pi without the setting) means no iMessage tools rather than a server
# that can't connect.
MESSAGES_MCP_URL = os.environ.get(
    "MESSAGES_MCP_URL", "http://127.0.0.1:27187/mcp" if WORKER_ID == "mac" else ""
)


# The endpoint refuses anything without its bearer token (messages-mcp serve_http.py). Same value as
# MESSAGES_MCP_TOKEN in messages-mcp/.env; it lives in this worker's .env on each machine.
MESSAGES_MCP_TOKEN = os.environ.get("MESSAGES_MCP_TOKEN", "")


MESSAGES_HEADERS = {"Authorization": f"Bearer {MESSAGES_MCP_TOKEN}"}


HAS_MESSAGES = bool(MESSAGES_MCP_URL and MESSAGES_MCP_TOKEN)


# Low-latency activity push to the status-hub server: transient "what the assistant is doing" events go
# straight to the SSE clients (bypassing Supabase realtime) so the on-screen activity bar tracks
# tool calls in real time. Authenticated by the hub's kiosk token, HUB_TOKEN, which comes from the
# environment (worker/.env, a Fly secret on jared-voice-worker) and is never written in code. It rides
# the X-Hub-Token header, never the URL, where it would land in proxy logs.
HUB_URL = os.environ.get("HUB_URL", "https://jared-status-hub.fly.dev").rstrip("/")


HUB_TOKEN = os.environ.get("HUB_TOKEN", "")


HUB_PUSH_URL = f"{HUB_URL}/api/voice-activity" if HUB_TOKEN else ""


HUB_HEADERS = {"X-Hub-Token": HUB_TOKEN}


# Drive the ESP light to reflect EVERY command's lifecycle (universal indicator, not just ESP-sourced):
# /led-blink while the worker is working, /led-off when done. The server holds the ESP socket; we hit its
# HTTP routes. Fire-and-forget in a thread so it never adds latency to the command path.
LED_TOKEN = os.environ.get("LED_TOKEN", "")


LED_BASE_URL = os.environ.get("LED_BASE_URL", "https://jared-voice.fly.dev").rstrip("/")


# Orchestrator (Mac) — when focus is a specific Mac session, the transcript is
# injected into that session's wall terminal via the orchestrator instead of
# running a fresh `claude -p`.
ORCH_URL = os.environ.get("ORCH_URL", "http://localhost:7682")


ORCH_TOKEN = os.environ.get("ORCH_TOKEN", "")


# Timing. Realtime (the voice_requests INSERT subscription) is the instant pickup path; every poll here
# is ONLY a backstop for when realtime misses or drops an event. So while the subscription is healthy the
# cadence is deliberately slow, tightening (IDLE_POLL_DEGRADED_S) only when realtime is actually down.
# Both the Mac primary and the Pi fallback run this same slow cadence on purpose: which worker ends up
# claiming a request isn't worth polling hard for — realtime wakes whichever one should take it.
HEARTBEAT_INTERVAL_S = 30.0


# Idle-claim cadence. Realtime is the wake signal (INSERT → instant claim); the poll is only a
# safety net, so while the subscription is healthy it can be slow — at 1s the three workers were
# hammering Supabase with ~280k reads/day of empty [] (a real chunk of the free-plan egress quota).
# When realtime is down/degraded the poll IS the pickup path, so it tightens to stay snappy.
IDLE_POLL_S = 60.0            # realtime subscribed — pure backstop; the socket recycles every 300s, so this only has to catch an event lost between recycles


# An idle cycle asks ONE question: is there anything pending? The two other reads it used to make —
# "should I defer?" and "are there orphaned claims?" — answer nothing while there is nothing to claim,
# so they run on their own slow clocks instead of on every cycle. That is what took an idle worker from
# ~3 reads a cycle (~20k/day) to one.
STANDBY_CHECK_S = 60.0        # re-evaluate standby while idle this often (it only sets the cadence)


RECLAIM_SWEEP_S = 60.0        # orphan-claim sweep: a backstop for a worker that died mid-command


IDLE_POLL_DEGRADED_S = 2.0    # realtime down — polling is the only pickup path, stay snappy


HEARTBEAT_FRESH_S = 90.0      # a higher-priority worker counts as "present" within this (≥3× the beat)


# Standby stand-down: a worker that's deferring to a healthy higher-priority worker (normally the Mac)
# does no useful work, so it drops to a much slower heartbeat + idle poll. Realtime still wakes it on any
# real command, and it ramps straight back to the active cadence the instant the primary goes silent — so
# the only cost is a slightly slower failover, which is fine (which worker claims doesn't matter).
# STANDBY_HEARTBEAT_S MUST stay < HEARTBEAT_FRESH_S so a standing-by worker never looks dead to the workers
# below it (which would let two fallbacks claim at once when the primary dies).
STANDBY_HEARTBEAT_S = 60.0   # deferring → beat at 60s (still < 90s fresh) instead of 30s


STANDBY_POLL_S = 120.0       # deferring → idle-poll every 120s (realtime carries real commands)


# Worker affinity: a LIVE hub context window stays pinned to the worker that owns it (the last worker to
# claim), overriding priority, so the conversation + its machine-local context aren't lost to a needless
# switch. Affinity releases only when the window resets (idle > session_reset_s / new day) or the owner goes
# silent past this grace — longer than HEARTBEAT_FRESH_S so a brief owner hiccup can't cause a context-losing
# switch, short enough that a genuinely-offline owner hands off promptly (then the new owner starts fresh).
OWNER_GRACE_S = 150.0


# A worker whose own Supabase calls failed (network, not a 4xx) within this window is DEGRADED: it says so
# in its heartbeat, stops claiming while a healthy worker exists, and the others stop deferring to it. A
# flaky link lets just enough heartbeats through to look alive while every claim and reply takes minutes,
# and liveness alone cannot see that.
NET_DEGRADED_S = 180.0


# How long a pending request may sit unclaimed while its preferred worker is IDLE before the next worker
# takes it anyway. A healthy worker claims in about a second; an idle one that has not claimed in this long
# is not going to. A preferred worker that is busy keeps its follow-ups, which queue behind its run.
FAILOVER_S = 8.0


BUSY_WAIT_S = 120.0   # longest a request waits behind the run of the worker it is left to (failover_wait)


DEDUP_WINDOW_S = 10.0         # fallback only: 2 mics' enqueue times for rows that predate capture-window stamping


DEDUP_TOL_S = 0.75            # pad each recording window by this (Fly↔Mac clock skew + ESP→Fly stream latency)


# A mic still hearing the speaker means the utterance is NOT FINISHED, whoever else has already enqueued a
# row for it. Claiming during that window is how a mid-sentence fragment gets acted on while the mic
# that has the whole thing is still recording.
MIC_QUIET_S = float(os.environ.get("MIC_QUIET_S", "2.5"))


# Never hold for ever. A mic wedged stamping, or a genuinely long pause, must not park a command.
MAX_HOLD_S = float(os.environ.get("MAX_HOLD_S", "25"))


HOLD_RETRY_S = 1.0            # how soon to look again while holding — not the idle backstop


SESSION_IDLE_RESET_S = 5 * 60       # default reset budget (5min idle); hub setting session_reset_s overrides


HTTP_TIMEOUT_S = 30.0


CLAUDE_TIMEOUT_S = float(os.environ.get("CLAUDE_TIMEOUT_S", "300"))


AGENT_TIMEOUT_S = float(os.environ.get("AGENT_TIMEOUT_S", "900"))  # full-agent (coding) runs get a longer leash


HUB_SESSION_FILE = STATE_DIR / "hub_session.json"       # hub mode: idle/day reset, or manual


AGENT_SESSION_FILE = STATE_DIR / "agent_session.json"   # full-agent mode: a SEPARATE, persistent context that


# Sentinel the hub UI enqueues to force a fresh Claude session (manual "Reset context").
# Must match VOICE_RESET_SENTINEL in the status-hub server.
RESET_SENTINEL = "__RESET_CONTEXT__"


_NOTHING_SAID = re.compile(r"^\W*thank\s+you\W*$", re.IGNORECASE)


WEB_TOOLS = ["WebSearch", "WebFetch"]


# The user's email and texts, named tool by tool so the two sends can be held apart below.
MAIL_TOOLS = [
    "mcp__gmail__search_email", "mcp__gmail__get_email", "mcp__gmail__get_email_thread",
    "mcp__gmail__list_labels", "mcp__gmail__find_email_contact", "mcp__gmail__create_draft",
    "mcp__gmail__modify_email",
    "mcp__messages__list_chats", "mcp__messages__get_thread", "mcp__messages__search_messages",
    "mcp__messages__list_contacts",
]


# Sending a text or an email reaches another person, so it is decided per TURN, not per list: allowed
# only on a command the user pressed for or typed (is_manual), never on one a microphone heard by itself, wake
# word included, which could send a sentence that was never meant for the assistant. See SdkRunner.gate,
# which also holds back switching JaredBot (the group-chat auto-replier) ON, since that sends texts too.
SEND_TOOLS = ["mcp__messages__send_message", "mcp__gmail__send_email"]
JAREDBOT_TOOL = "mcp__voice__set_jaredbot"


# Tools that only mean something next to the user's signed-in browser, which this worker does not have: a cart
# URL has to be opened there, a cart recording is what its /cart page showed, and an item id is read off
# the retailer's site. Here they could only be called with made-up values, so they are REMOVED from every
# runner's schema: a cart request has to become a start_session, never a turn with no browser.
SESSION_TOOLS = ["mcp__data__build_walmart_cart_url", "mcp__data__record_walmart_cart",
                 "mcp__data__save_shopping_item"]


# The tools that start a Claude session or put words into one. Refused on every turn that is not
# manual (`is_manual`) by the fence (SdkRunner._fence); a non-manual turn never reaches a runner without
# that fence. Named one by one because the data server's other session tools (list, focus, rename, end)
# only look at or tidy what is already running.
SESSION_START_TOOLS = ["mcp__data__start_session", "mcp__data__follow_up", "mcp__data__resume_session",
                       "mcp__data__transfer_session"]


ALL_TOOLS = ["mcp__data", "mcp__voice", "mcp__govee-lights", "mcp__petlibro-feeder", *WEB_TOOLS, *MAIL_TOOLS, *SEND_TOOLS]


UNTRANSCRIBED_MAX_S = 75.0    # older than this a queued clip is closed, not transcribed late; under the 90s stuck-transcribing sweep


# Invariants between the timings above. Each one, broken, fails silently (two fallbacks claiming at once,
# a context-losing switch on a heartbeat hiccup, a queued clip closed while it is still being transcribed),
# so a host with a bad override refuses to start instead.
for _ok, _why in (
    (STANDBY_HEARTBEAT_S < HEARTBEAT_FRESH_S, "STANDBY_HEARTBEAT_S must stay under HEARTBEAT_FRESH_S, or a "
                                              "standing-by worker looks dead to the workers below it"),
    (HEARTBEAT_INTERVAL_S < HEARTBEAT_FRESH_S, "HEARTBEAT_INTERVAL_S must stay under HEARTBEAT_FRESH_S"),
    (OWNER_GRACE_S > HEARTBEAT_FRESH_S, "OWNER_GRACE_S must exceed HEARTBEAT_FRESH_S"),
):
    if not _ok:
        raise SystemExit(f"voice worker config: {_why}")
