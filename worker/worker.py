#!/usr/bin/env python3
"""Headless voice-command worker daemon.

Claims voice commands from a Supabase queue (`voice_requests`) and runs each one
through a headless `claude -p` session on the Claude *subscription* (no API key),
with the `data` MCP attached, then writes the conversation back to Supabase
(`voice_messages`) for a live display.

The same code runs on the Mac (WORKER_ID=mac) and later the Pi (WORKER_ID=pi).
Only ordering of WORKER_PRIORITY differs; the highest-priority worker that has a
fresh heartbeat wins each request (the others defer).

Phase 1 only handles requests when `voice_focus.focus == 'hub'` (the shared "hub"
conversation). When focus points at a specific Mac session id, that's a Phase-2
feature — for now such requests are left untouched (released back to pending) and
skipped, so this worker never crashes on them. See README for the Phase-2 TODO.

Design notes:
- Serialized: exactly one request is processed at a time, never concurrent.
- Defensive: every Supabase call is wrapped so a transient HTTP error logs and
  is swallowed rather than killing the loop.
- Hub turns are fresh runs with the day's conversation replayed into the prompt (recent_context: since 4 AM
  Eastern or the last manual reset, timestamped). Full-agent mode keeps a persisted `claude` session
  (agent_session.json) resumed via `--resume`, reset ONLY on a manual "Reset context". Switching the
  agent_mode toggle never clobbers the other's context. See should_reset_session() / session_file().
"""

import asyncio
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx

# --------------------------------------------------------------------------- #
# Config (env, with sensible defaults)
# --------------------------------------------------------------------------- #

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
    w.strip() for w in os.environ.get("WORKER_PRIORITY", "mac,pi").split(",") if w.strip()
]

STATE_DIR = Path(os.path.expanduser(os.environ.get("STATE_DIR", "~/.voice-worker")))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# The worker never bills the Console API: every turn runs on the Claude subscription, and a failure is
# retried and then surfaced, never finished on the key. The key may still be in the environment (older
# .env files, the Fly secret), so it is popped at import — a subprocess that inherited it would quietly
# bill the API instead of the subscription.
os.environ.pop("ANTHROPIC_API_KEY", None)
# Most hub turns are tool use, but nutrition macro estimation leans on the model's internal
# food knowledge — so default to Sonnet (strong priors, still fast). Override with HUB_MODEL
# ('haiku' for max speed on trivial turns, 'opus' for the best macro/disambiguation quality).
HUB_MODEL = os.environ.get("HUB_MODEL", "sonnet")

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
# Empty (Fly, or a Pi without the setting) means no iMessage tools rather than a server that can't connect.
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
# tool calls in real time. Authenticated by the hub's kiosk token in the URL.
HUB_PUSH_URL = os.environ.get(
    "HUB_PUSH_URL",
    "https://jared-status-hub.fly.dev/api/voice-activity?token=REDACTED",
)


def push_activity(text: str, state: str = "tool") -> None:
    """Fire a transient activity event to the hub (best-effort; never raises)."""
    if not HUB_PUSH_URL:
        return
    try:
        httpx.post(HUB_PUSH_URL, json={"text": text, "state": state, "focus": "hub"}, timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


# Native push to Jared's phone: the past-tense result of a completed voice command lands as a phone
# notification (APNs via the hub's /api/notify — the same proven sender + device tokens as the weight
# reminder). The hub token rides the URL (same as the activity push above).
HUB_NOTIFY_URL = os.environ.get(
    "HUB_NOTIFY_URL",
    "https://jared-status-hub.fly.dev/api/notify?token=REDACTED",
)


_push_cache = {"v": None, "t": 0.0}


def push_enabled() -> bool:
    """Whether to push command results to Jared's phone. Hub setting `push_enabled` (Settings → Push
    notifications), cached ~30s. Default ON."""
    now = time.monotonic()
    if _push_cache["v"] is None or now - _push_cache["t"] > 30:
        v = True
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                          params={"key": "eq.push_enabled", "select": "value"},
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=5)
            rows = r.json()
            if rows:
                v = str(rows[0].get("value")).strip().lower() not in ("0", "false", "off", "no", "")
        except Exception:  # noqa: BLE001
            pass
        _push_cache.update(v=v, t=now)
    return _push_cache["v"]


def notify_phone(body: str, title: str = "Hub") -> None:
    """Send a voice command's final result to Jared's phone as a push notification. Threaded + best-
    effort so it never delays closing out the request and never raises. Skipped if push is toggled off."""
    if not (HUB_NOTIFY_URL and body) or not push_enabled():
        return

    def _send():
        try:
            httpx.post(HUB_NOTIFY_URL, json={"title": title, "body": body[:300], "kind": "voice"}, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_send, daemon=True).start()


# Failure alerts. A request is not allowed to fail silently: a subscription run that still fails after
# its retries (`run_subscription`) is pushed with its error, because a revoked or expired token is a
# standing condition that every later request pays for. One push per host per (stage, class) per ALERT_COOLDOWN_S, so a burst of
# the same fault does not flood the phone. Deliberately bypasses push_enabled — that toggle is for
# command results, not outages.
ALERT_COOLDOWN_S = float(os.environ.get("ALERT_COOLDOWN_S", "3600"))
_alert_last: dict = {}


def classify_failure(err: str) -> str:
    e = (err or "").lower()
    if "401" in e or "authenticate" in e or "revoked" in e or "oauth" in e:
        return "auth"
    if "credit balance" in e or "billing" in e or "402" in e:
        return "billing"
    if "429" in e or "rate limit" in e or "overloaded" in e:
        return "rate-limit"
    return "error"


# A subscription run that fails for a reason that passes on its own — Anthropic overloaded or erroring,
# a rate limit, the network — is tried again after each of these pauses before the turn is given up.
# There is no paid fallback behind it: waiting a little is free, and on 2026-09-21 a single "529
# Overloaded" sent an Opus turn to the Console key. An auth or billing failure is not retried; waiting
# cannot fix a revoked login.
SUBSCRIPTION_RETRY_S = (3, 8, 15)


def transient_failure(err: str) -> bool:
    e = (err or "").lower()
    if e.startswith("claude timed out"):
        return False   # our own watchdog: a hung run hangs again, and each retry holds the claim 300s more
    if classify_failure(err) == "rate-limit":
        return True
    return any(t in e for t in ("529", "500", "502", "503", "504", "internal server error", "timed out",
                                "timeout", "connection", "network", "temporarily"))


def run_subscription(run, label: str) -> dict:
    """`run()` (a subscription turn), retried through SUBSCRIPTION_RETRY_S while it fails transiently.
    Returns the last result; the caller surfaces it if it is still a failure."""
    result = run()
    for pause in SUBSCRIPTION_RETRY_S:
        if result.get("ok") or not transient_failure(result.get("raw_error") or ""):
            break
        log(f"{label} failed ({result.get('raw_error')}) — retrying on the subscription in {pause}s")
        push_activity(f"Retrying ({classify_failure(result.get('raw_error') or '')})")
        time.sleep(pause)
        result = run()
    if not result.get("ok"):
        # Reported by process_request, which knows whether the command was handed on or ended.
        log(f"{label} failed ({result.get('raw_error')}) — giving up, no paid fallback")
    return result


def alert_failure(stage: str, err: str, detail: str = "") -> None:
    """Push a worker failure to Jared's phone (`<host> · <stage> · <class> · <error>`) and start a session in
    the health repo to investigate it (`investigate`). `detail` is what that session is told about the
    request and what became of it. Best-effort, threaded, and both share the one cooldown."""
    kind = classify_failure(err)
    key = f"{stage}:{kind}"
    now = time.monotonic()
    if now - _alert_last.get(key, -ALERT_COOLDOWN_S) < ALERT_COOLDOWN_S:
        return
    _alert_last[key] = now
    body = f"{WORKER_ID} · {stage} · {kind} · {(err or '')[:180]}"
    log(f"ALERT {body}")

    def _send():
        if HUB_NOTIFY_URL:
            try:
                httpx.post(HUB_NOTIFY_URL, json={"title": "Voice worker", "body": body[:300], "kind": "voice"}, timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
        investigate(stage, kind, err, detail)

    threading.Thread(target=_send, daemon=True).start()


# The machines a desk session can run on, by the presence row each one's session host beats.
DESK_PRESENCE = {"pi": "pi-desk", "mac": "desk"}
HEALTH_REPO = "health"   # by name in `repos`, whose path is what the session hosts accept; the Pi clones it


def investigate(stage: str, kind: str, err: str, detail: str) -> None:
    """Dispatch a session in the health repo to find out why this worker failed. It runs on a machine
    other than this one when that machine's session host is up — the failing host may be failing for
    the same reason — and on this one otherwise. Fly has no session host, so a Fly failure goes to the
    Pi or the Mac. Writes the `open_session` command directly: no focus change, so his next spoken
    follow-up still goes where it was going."""
    order = ["mac", "pi"] if WORKER_ID == "pi" else ["pi", "mac"]
    try:
        hdr = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        rows = httpx.get(f"{SUPABASE_URL}/rest/v1/worker_presence",
                         params={"worker": f"in.({','.join(DESK_PRESENCE.values())})", "select": "worker,last_seen"},
                         headers=hdr, timeout=5).json()
        up = {r["worker"] for r in rows if _healthy(r)}
        target = next((m for m in order if DESK_PRESENCE[m] in up), None)
        if target is None:
            log("investigate: no session host is up; nothing dispatched")
            return
        prompt = (
            f"The voice worker on `{WORKER_ID}` failed at {now_iso()} (stage `{stage}`, class `{kind}`).\n\n"
            f"Error: {err}\n\n{detail}\n\n"
            "Find the cause and fix it if you can, on whichever machine it lives. Logs: the Pi's is "
            "`journalctl -u voice-worker`, the Mac's is `~/repos/voice-pipeline/worker/worker.out.log`, "
            "Fly's is `fly logs -a jared-voice`. The request is in Supabase `voice_requests`. Follow this "
            "repo's CLAUDE.md: check function, not liveness, and give the failure a row in WATCHLIST.md. "
            "Your last message should say what broke, what you changed, and what is still open."
        )
        repo = httpx.get(f"{SUPABASE_URL}/rest/v1/repos", params={"name": f"eq.{HEALTH_REPO}", "select": "path,name"},
                         headers=hdr, timeout=5).json()[0]
        httpx.post(f"{SUPABASE_URL}/rest/v1/desk_commands", headers={**hdr, "Content-Type": "application/json"},
                   json={"id": str(uuid.uuid4()), "kind": "open_session",
                         "payload": {"path": repo["path"], "name": repo["name"], "target": target, "prompt": prompt,
                                     "label": f"Voice worker failure on {WORKER_ID}"}},
                   timeout=5).raise_for_status()
        log(f"investigate: health session dispatched to {target}")
    except Exception as e:  # noqa: BLE001 — the alert already went; never take the worker down
        log(f"investigate: dispatch failed: {e}")


# Drive the ESP light to reflect EVERY command's lifecycle (universal indicator, not just ESP-sourced):
# /led-blink while the worker is working, /led-off when done. The server holds the ESP socket; we hit its
# HTTP routes. Fire-and-forget in a thread so it never adds latency to the command path.
LED_TOKEN = os.environ.get("LED_TOKEN", "")
LED_BASE_URL = os.environ.get("LED_BASE_URL", "https://jared-voice.fly.dev").rstrip("/")


def ping_esp_led(state: str) -> None:
    """Set the ESP light: state ∈ {off, blink, on}. Best-effort; no-op if the ESP server/light is absent."""
    if not LED_BASE_URL:
        return

    def _go():
        try:
            httpx.get(f"{LED_BASE_URL}/led-{state}",
                      params={"token": LED_TOKEN} if LED_TOKEN else None, timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_go, daemon=True).start()

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
# A mic still hearing Jared means the utterance is NOT FINISHED, whoever else has already enqueued a
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
                                                        # never auto-resets — only manual erase clears it
# Sentinel the hub UI enqueues to force a fresh Claude session (manual "Reset context").
# Must match VOICE_RESET_SENTINEL in the status-hub server.
RESET_SENTINEL = "__RESET_CONTEXT__"
_NOTHING_SAID = re.compile(r"^\W*thank\s+you\W*$", re.IGNORECASE)

# Power/precision directive (the Opus escalation path): maximum-precision for hard estimates — typically a
# restaurant dish whose ingredients/macros are unknown. Opus runs at its DEFAULT effort: measured on four
# meals, `--effort max` returned the same macros 7–19s slower per turn (docs/ROUTING.md). "ultrathink"
# here sets no budget — the CLI (2.1.301) turns it into a one-line note asking for deeper reasoning.
POWER_DIRECTIVE = (
    "\n\n[Precision mode — ultrathink. Take your time and reason carefully before acting. If this "
    "involves estimating nutrition for a dish whose exact contents aren't given, decompose it into its "
    "likely ingredients with realistic restaurant portions, estimate each component's macros, and sum "
    "them — don't lowball. ALWAYS use the structured per-ingredient breakdown the log tool supports: "
    "give each component its OWN protein/carbs/fat (and calories when you know them); the server computes "
    "the meal's totals by summing the ingredients, so keep each component's macros accurate.]"
)

# ---- Dynamic route. Sonnet is the fast first responder, given ONLY the SAFE tools below in its
# --allowedTools; the gated tools are absent, so in `-p` mode Sonnet is DENIED them and physically cannot
# corrupt that data. It still SEES them in the schema and, per the escalate directive, hands the whole
# turn to a fresh Opus rather than claiming it can't.
#
# What decides the split is NOT read vs write. It is whether the model has to supply a value out of its
# OWN knowledge: a food's macros, or the muscle map for a movement the catalog has never seen. Those are
# gated. A tool whose every argument is already in front of the model — a number the user spoke, an id
# from a read it just did — is on the fast path however destructive it is, because there is nothing there
# for a bigger model to know better.
# The web and reading his email and texts are on BOTH lists. Nothing in them is a value the model
# invents: a search takes his question, a read takes what he asked for. Named tool by tool rather than
# by server so the two sends can be held apart; a tool either server adds later is visible but denied
# until it is listed here.
OPEN_TOOLS = [
    "mcp__gmail__search_email", "mcp__gmail__get_email", "mcp__gmail__get_email_thread",
    "mcp__gmail__list_labels", "mcp__gmail__find_email_contact", "mcp__gmail__create_draft",
    "mcp__gmail__modify_email",
    "mcp__messages__list_chats", "mcp__messages__get_thread", "mcp__messages__search_messages",
    "mcp__messages__list_contacts",
    "WebSearch", "WebFetch",
]
# Sending a text or an email reaches another person, so it is decided per TURN, not per list: allowed
# when he pushed a button or typed, never from the always-on room mics, which hear the whole room and
# would send a sentence that was never meant for the assistant. See turn_tools() and SdkRunner._gate.
SEND_TOOLS = ["mcp__messages__send_message", "mcp__gmail__send_email"]
# Tools that only mean something next to his signed-in browser, which this worker does not have: a cart
# URL has to be opened there, a cart recording is what its /cart page showed, and an item id is read off
# the retailer's site. Here they could only be called with made-up values. They are REMOVED from every
# runner's schema rather than gated, because a gated tool is what makes Sonnet escalate — and a cart
# request has to become a start_session, never an Opus turn with no browser.
SESSION_TOOLS = ["mcp__data__build_walmart_cart_url", "mcp__data__record_walmart_cart",
                 "mcp__data__save_shopping_item"]
ALL_TOOLS = ["mcp__data", "mcp__voice", "mcp__govee-lights", "mcp__petlibro-feeder", *OPEN_TOOLS, *SEND_TOOLS]  # full toolset (Opus)


def turn_tools(ambient: bool, base: "list | None" = None) -> list:
    """A turn's allow list: `base` (the full toolset by default) with the sends added for a pushed
    command and removed for one from a room mic."""
    tools = [t for t in (base or ALL_TOOLS) if t not in SEND_TOOLS]
    return tools if ambient else tools + SEND_TOOLS

# Sonnet's allow list.  ⚠️  See docs/ROUTING.md before changing this.
#
# It MUST be an allow list, not a deny list, and the difference is load-bearing:
# a tool omitted from --allowedTools stays VISIBLE in the schema and is refused only when called, so
# Sonnet sees log_nutrition, recognises it needs it, and escalates. --disallowedTools instead removes
# the tool from the schema outright — Sonnet cannot see it, has nothing to escalate about, and
# truthfully reports that nutrition logging "isn't available". That regression shipped and broke
# logging until it was caught, so: do not "simplify" this into a deny list.
#
# The cost is that this list has to be maintained by hand and can rot. validate_safe_tools() below
# makes that loud instead of silent.
SAFE_TOOLS = [
    "mcp__voice", "mcp__govee-lights", "mcp__petlibro-feeder",              # actuators + voice settings
    "mcp__data__get_nutrition", "mcp__data__get_nutrition_targets", "mcp__data__get_workout",
    "mcp__data__get_workout_catalog", "mcp__data__get_exercise_history", "mcp__data__get_weekly_volume",
    "mcp__data__get_health_profile", "mcp__data__get_rep_watcher", "mcp__data__list_workouts",
    "mcp__data__get_surf",
    # The saved shopping catalogue and the recorded Walmart cart: reads, so "what's in my cart" is
    # answered here instead of costing an Opus escalation.
    "mcp__data__get_shopping_items", "mcp__data__get_walmart_cart",
    "mcp__data__list_staged", "mcp__data__reload_wall",                     # reads + the wall reload
    # lists / notes / dated items: low-stakes writes needing no domain knowledge → fast path.
    # Neither reminders nor notes have tools of their own any more — a reminder is a list item with a
    # due date (00056), a note is an undated one whose body is its `text` (00092). Both are covered
    # by get_lists / add_to_list / remove_from_list.
    # create_list IS on the fast path even though it makes something new: it invents nothing, taking
    # only a name Jared said. It is separate from add_to_list precisely so that adding cannot create
    # a list by accident (00094) — gating it here would just move that accident to a bigger model.
    "mcp__data__get_due", "mcp__data__set_due", "mcp__data__set_priority", "mcp__data__set_list_priority", "mcp__data__set_list_emoji",
    "mcp__data__get_lists", "mcp__data__add_to_list", "mcp__data__complete_item", "mcp__data__remove_from_list", "mcp__data__delete_list",
    "mcp__data__create_list",
    # Recipes. Reading them invents nothing, and log_recipe cannot: it takes a saved recipe's NAME and
    # a serving count, and every macro it writes was estimated once, when the recipe was saved. That is
    # the whole reason it exists as a separate tool from log_nutrition — there is no field on it through
    # which a macro could be guessed, so the fast path is safe by construction rather than by care.
    # save_recipe IS gated: writing a recipe means estimating each ingredient's macros from its portion.
    "mcp__data__list_recipes", "mcp__data__get_recipe", "mcp__data__log_recipe", "mcp__data__delete_recipe",
    # Body figures the user says out loud. log_weight refuses anything it was not explicitly given;
    # set_height is inches. Nothing to estimate, so nothing the capable model would do better.
    # update_weight and remove_weight take the entry ID from get_health_profile, so neither can pick a
    # weigh-in by date and neither writes a number he did not say.
    "mcp__data__log_weight", "mcp__data__set_height", "mcp__data__update_weight", "mcp__data__remove_weight",
    # Sets. An exercise is named by its CATALOG ID (migration 00075), so none of these can invent a
    # movement or land on the wrong one: an id from get_workout_catalog either exists or the call
    # fails. Registering a genuinely new movement means inventing a muscle map, which is real
    # knowledge — that is upsert_catalog_exercise, deliberately absent from this list, so the case
    # escalates rather than being guessed at down here. remove_set takes a set id; all three accept a
    # stated occurred_at, a wall-clock string he said out loud that needs no more knowledge than the
    # reps do.
    "mcp__data__log_set", "mcp__data__update_set", "mcp__data__remove_set",
    # Rep watcher: an exercise the catalog already has, plus a weight. setWatcher() cannot create a
    # catalog entry — it errors on an unknown name — and clear takes no arguments at all.
    "mcp__data__set_rep_watcher", "mcp__data__clear_rep_watcher",
    # Moving or deleting an entry by ITS ID. The macros were estimated when it was staged; committing
    # it changes no number, and a delete invents nothing. Destructive, but destructiveness is not a
    # question a bigger model answers better.
    "mcp__data__commit_staged", "mcp__data__remove_staged", "mcp__data__delete_nutrition_entry",
    # Dispatching work to a machine and managing what is running there. None of it changes data, and
    # none of it needs domain knowledge — routing a prompt to a session, closing one, moving one. The
    # whole group belongs on the fast path or none of it does, and follow_up sat outside it only by
    # oversight, which cost every dictated follow-up a full Opus re-run before it could be typed.
    "mcp__data__list_repos", "mcp__data__start_session", "mcp__data__get_sessions",
    "mcp__data__resume_session", "mcp__data__end_session", "mcp__data__transfer_session",
    "mcp__data__follow_up", "mcp__data__rename_session", "mcp__data__focus_session",
    "mcp__data__unfocus_session",
    "mcp__data__set_wall",
    *OPEN_TOOLS,
]

def validate_safe_tools(mcp_js: str) -> None:
    """Warn about SAFE_TOOLS entries that no longer exist in the data MCP.

    The allow list is maintained by hand — it has to be, because omitting a tool is what makes it
    visible-but-denied and therefore escalatable. The danger is that it drifts: data-layer renames or
    removes a tool and nothing here notices. It was still naming create_reminder and
    complete_list_item months after those were deleted, and it silently gated out every tool added
    since, which is why dispatching work by voice could not work at all.

    This does not fix the list, only make its rot audible. A name that no longer exists is dead
    weight; a data tool that exists but is listed nowhere is either deliberately gated or forgotten,
    and only the log can tell you which.
    """
    try:
        src = pathlib.Path(mcp_js).read_text(errors="ignore")
    except OSError:
        return  # the MCP build is not readable from here; not worth failing startup over
    # Tool definitions only. A looser pattern also matches the server name in the mcpServers
    # block ("data"), which is not a tool and shows up as a phantom every startup.
    real = {n for n in re.findall(r"\{\s*name:\s*'([a-z_]+)'\s*,\s*description:", src)}
    if not real:
        return
    listed = {t.split("__")[-1] for t in SAFE_TOOLS if t.startswith("mcp__data__")}
    dead = sorted(listed - real)
    if dead:
        log(f"[tools] SAFE_TOOLS names {len(dead)} tool(s) that no longer exist: {', '.join(dead)}")
    ungated = sorted(real - listed - {t.split("__")[-1] for t in SESSION_TOOLS})
    if ungated:
        log(f"[tools] not on the fast path (gated or forgotten): {', '.join(ungated)}")


# Escalation: Sonnet emits this (mirrors IGNORE_SENTINEL) when a request needs a gated write tool; the
# dispatch then re-runs the whole turn on Opus + max thinking. Appended to every standard-route turn.
ESCALATE_SENTINEL = "__ESCALATE__"
ESCALATE_DIRECTIVE = (
    "\n\n[You are the fast first responder. You can see the full toolset, but the tools that need a "
    "value supplied out of the model's OWN knowledge are handled by a more capable model, not you: "
    "estimating a food's macros (logging, staging or editing nutrition, subtracting leftovers, or SAVING "
    "a recipe), setting nutrition targets, and giving categories/muscles to an exercise the catalog does "
    "not have yet. Everything whose arguments are already in front of you is YOURS, and you should just "
    "do it: read anything (get_*/list_*); manage lists, notes and dated items; log a weight or height he "
    "stated as a number; commit, remove or delete an entry by its id; log, edit or remove a SET; set or "
    "clear the rep watcher; drive the lights, feeder, TV and voice settings; and read and send his "
    "email (gmail tools) and his texts (messages tools). "
    "When he explicitly names a RECIPE — 'my oatmeal recipe', 'the gochujang tofu recipe' — log or stage "
    "it with log_recipe, which is yours, because its macros were estimated once when it was saved and you "
    "are only naming it. A meal he describes by a past log ('same as yesterday', 'what I had Monday') is "
    "NEVER a recipe, even when a recipe shares its name: it is a copy of that logged entry. "
    "For sets, call get_workout_catalog first and pass the matching exercise's ID — however he said it, "
    "plural or singular. If the movement is NOT in the catalog at all it needs a muscle map you would "
    "have to invent — escalate instead. "
    "LOOKING SOMETHING UP is yours, done right here with WebSearch and WebFetch: a fact, a score, a "
    "listing, opening hours, a place, anything current. Search, read the pages you need, and answer in "
    "your reply. A session is only for what needs his SIGNED-IN browser: ACTING on a site (his Amazon "
    "or Walmart cart, an order, a ride, a booking, a form) or reading something behind his logins (an "
    "order's status, what is in a cart). That is a start_session on the Pi with his request passed "
    "through, and it is YOURS too: start_session is on your toolset, it is not an escalation. The "
    "session's first reply is posted back into this conversation on its own, so say only that it has "
    "started; never offer to check on it later, because nothing will run you again to do it. Never "
    "answer a question about the current world out of your own memory, and never tell him you cannot "
    "browse. "
    "Decide this from the CURRENT instruction ALONE. The recent conversation above is background: that "
    "it was about food, weight or workouts does not make THIS turn one, and a request to add something "
    "to a list is a list request no matter what was being discussed a minute ago. "
    "If the current instruction genuinely needs a gated tool, do NOT attempt it and do NOT say you "
    f"can't — reply with EXACTLY '{ESCALATE_SENTINEL}' and nothing else, and it is handed to the "
    "capable model. The same when he asks for that model himself — 'escalate to Opus', 'use Opus', "
    f"'have Opus do it': reply '{ESCALATE_SENTINEL}' alone, whatever the rest of the request is, and "
    "his words go to Opus unchanged. Handle everything else yourself.]"
)

# Photo turns (attachments on the request): the photos are downloaded locally, their paths ride the
# prompt, and Read is allowed so the model can view them. The critical framing: however many photos
# arrive, they ALL describe ONE item — context to pin it down (the dish, its nutrition label, the
# menu/restaurant that identifies it) — never separate things to log.
ATTACH_DIRECTIVE = (
    "\n\n[The user attached {n} photo(s), saved locally at the paths below. View EVERY photo with the "
    "Read tool before acting. All of the photos together describe ONE single item (one dish, product, "
    "or entry) — never treat multiple photos, or multiple things visible in one photo, as separate "
    "items. If a finger or hand is pointing at something, THAT is the item. The photos are context to "
    "identify that one item: the item itself, its nutrition-facts label, a menu listing it, or the "
    "restaurant/packaging that pins down exactly which it is. Read exact macros straight off any "
    "nutrition label; otherwise identify the item and estimate carefully. A nutrition-facts label with "
    "no other text is the whole package eaten — if the label states multiple servings per container, "
    "multiply the per-serving macros by that count; don't log just one serving. Honor any quantity/"
    "portion given in the user's text instead (grams eaten, fraction of the dish, 'one serving') — "
    "that overrides the whole-package default. Unless the text clearly says otherwise, the intent is "
    "to log ONE nutrition entry."
    "\nPhotos:\n{paths}]"
)

# Sentinel the model emits to silently drop a turn (wake-word false positive). Matched loosely below.
IGNORE_SENTINEL = "__IGNORE__"


def is_ignore(text) -> bool:
    t = (text or "").lower()
    return "__ignore__" in t or re.sub(r"[^a-z]", "", t) == "ignore"

# Appended to every command he meant (pressed, typed, sent from the watch or the phone). These turns are
# never told the ignore sentinel exists — only ADDRESSING_DIRECTIVE teaches it, on room-mic turns — so
# there is nothing to skip with. Nor is there a canned "didn't understand" reply: offered one, the model
# used it as an exit on clear speech that merely ended without a literal question.
DELIBERATE_DIRECTIVE = (
    "\n\n[He said this to you on purpose. When he describes something without asking outright, answer "
    "what he evidently wants to know. Only if you genuinely cannot tell what he means, say so in your "
    "own words and ask him.]"
)

# Always-on mics hear the WHOLE room — the TV, Jared talking to his cats, other people talking to or
# near him — and speaker-ID can't tell "a command for the assistant" from "Jared said a sentence out
# loud." Almost none of it is gibberish; it's real, grammatical speech that simply isn't ADDRESSED to
# the assistant. So the gate is not "is this noise?" but "is this a command/question meant for ME?"
# Appended to transcribed turns only — typed input is intentional and never gets this.
ADDRESSING_DIRECTIVE = (
    "\n\n[This text came from an always-on mic that hears the whole room — the TV, Jared talking to his "
    "cats or to other people, others talking near him — so most of what reaches you was NOT said to you. "
    "It is almost never gibberish; it is real, grammatical speech that simply isn't addressed to the "
    "assistant. Act only on an utterance meant for you. When it is not, reply with EXACTLY "
    f"'{IGNORE_SENTINEL}' and nothing else — never explain that it wasn't a command."
    "\n\nOne parsing note: when he dictates development work — 'send a command to the Mac agent…', 'in "
    "my status-hub repo I want you to…' — the machine he names is where to SEND the work, not someone "
    "else being addressed instead of you. Those are for you. Development talk that names no destination is him speaking to a session already open elsewhere, which has heard him already."
    "\n\nOne narrow exception. An utterance that itself asks to focus a session — 'focus the X session' or 'pin the X session', followed "
    "by an instruction, IN THAT SAME UTTERANCE, sends that instruction to that session. THAT IS THE ONLY "
    "CASE. A session being focused by an earlier command does NOT make later speech an instruction to "
    "it — the mic keeps hearing the room long after he has stopped talking to you, and forwarding that "
    "into a live session is the worst thing you can do here. Anything later needs him to hand it over: "
    "'tell it to', 'send a follow-up', 'reply to it'.]"
)

# The room mics can never send a text or an email. The send tools are denied on those turns by every
# runner (SdkRunner._fence, turn_tools, --disallowedTools); this closes the ways around that the model
# could still reach for — escalating, or starting a session whose own tools would send it.
ROOM_SEND_DIRECTIVE = (
    "\n\n[This came from an always-on room mic, which cannot send texts or email. If it asks you to "
    "send, reply to or forward a text or an email, do not do it by any route — no session, no "
    f"'{ESCALATE_SENTINEL}' — and say it has to come from the button on his phone or watch.]"
)

# Role framing for every hub turn. The base preset is claude_code (a coding agent), so without this
# the model treats requests like a dev session and reaches for Bash/curl. This establishes what it
# actually is. It deliberately gives NO per-tool guidance — each tool's schema documents its own use.
HUB_SYSTEM = (
    "You are the assistant behind Jared's personal life-data hub. This is NOT a coding session and you "
    "are NOT exploring a codebase. Your sole job is to fulfill each request by calling the tools you "
    "have been given: to read and update his life data (nutrition, workouts, weight, "
    "smart lights, the cat feeder, and the like), to read and send his email and texts, and to search "
    "and read the web. The tools are the only way to act, and each tool's own "
    "description tells you how to use it — follow those. Bash, curl, a shell, the filesystem, and file "
    "editing are NOT available to you; never attempt them. "
    "GOLDEN RULE: when a request clearly maps to your tools, COMPLETE it in one shot — fill in any "
    "unspecified detail with a reasonable assumption, or look up what you need with the read tools (e.g. "
    "call get_nutrition for 'the meal I had yesterday' and reuse it), instead of refusing or asking the "
    "user to clarify. Only ask when it is genuinely unclear WHAT they want done — never merely because an "
    "amount, a date, or some other detail was left implicit or could simply be looked up. "
    "If truly no available tool can satisfy a request, say so rather than improvising another way. "
    "NEVER DESCRIBE AN ACTION A TOOL DID NOT PERFORM. When a tool returns an error, a refusal, or "
    "anything other than the result you expected, say what actually happened — the tool's own words are "
    "usually the right answer. Reporting a message as sent, a session as focused, or a value as logged "
    "when the call failed is the worst thing you can do here, because the reply is often the only "
    "feedback he gets and he will believe it. "
    "Before you EVER say you lack a tool for something, re-read the tool list you were given and check. "
    "Claiming a capability does not exist when it does is worse than any other failure here, because it "
    "sends him off to solve a problem that is not real. "
    "EVERY PART OF THE REQUEST: a single sentence often contains several instructions joined by 'and then', "
    "'and also', or a comma. Do all of them. Before replying, re-read the request and confirm each separate "
    "instruction was carried out; a trailing clause is the one most easily dropped. If you did only some of "
    "it, say which parts you did and which you did not, rather than reporting success for the whole. "
    "YOUR LAST MESSAGE IS THE WHOLE ANSWER: anything you write between tool calls is shown only as passing "
    "progress and is not kept, so the final message must stand on its own. Restate in it whatever it "
    "depends on from earlier in the turn; never point back at it."
)

# Full-agent framing — the opposite of HUB_SYSTEM. Used only on the subscription path when "Full agent
# mode" is on (power / custom-local, Mac+Pi). It places NO restriction on capability: the model keeps its
# full native toolset (shell, file editing, web, etc.) AND the data/voice/govee MCP tools, and is told it
# is a general-purpose agent — so Jared can code or do anything by voice. Requests arrive as transcriptions.
AGENT_SYSTEM = (
    "You are Jared's all-purpose assistant, driven by voice. You have your full set of capabilities — you can "
    "write and run code, use the shell, edit files, search the web, and anything else — IN ADDITION to the "
    "data/voice/govee MCP tools for his life data (nutrition, workouts, weight, lists, reminders, notes, "
    "recipes), smart lights, and the cat feeder (petlibro-feeder: feed the cats, check food level / last "
    "feed / schedule). Use whatever combination of tools the request needs. Most of what he asks is "
    "routine life-data — logging nutrition/workouts/weight, lists, reminders, notes, lights, the feeder — "
    "and on those be DECISIVE: when a request clearly maps to a tool, COMPLETE it in one shot, filling in any "
    "unspecified detail with a reasonable assumption (or looking it up with the read tools), rather than "
    "refusing or asking him to clarify; only ask when it's genuinely unclear WHAT he wants. "
    "Requests come from "
    "speech-to-text, so expect occasional mis-hearings; if a request is genuinely unintelligible, say so. "
    "Your last message is the whole answer: anything you write between tool calls is shown only as passing "
    "progress and is not kept, so restate in the final message whatever it depends on from earlier in the turn."
)


# Jared changes this same data OUT OF BAND while you work — via the hub's own UI (an "add set" button,
# inline edit/delete controls) and via other voice turns a DIFFERENT worker may handle with its own separate
# memory (continuity is machine-local; the fast path has none at all). So the conversation you remember is
# not a reliable mirror of the live data. Appended to BOTH framings so every path treats the data — what the
# tools return — as ground truth, never memory.
SHARED_STATE_NOTE = (
    "IMPORTANT — you are NOT the only one changing this data. Jared edits it himself in real time: the hub "
    "has on-screen buttons (including an 'add set' button) plus inline edit and delete controls, and some "
    "turns are handled by a different machine that does not share your memory. So your conversation history "
    "is NOT a reliable record of the current state — it may be stale, partial, or reset between turns. Never "
    "assume the data is in whatever state your memory implies. Treat the DATA ITSELF, what the tools return, "
    "as the only source of truth: when a current value or count matters, READ it fresh with a read tool "
    "instead of trusting what you remember doing. When a request is additive — 'add another set', 'log one "
    "more', 'add a fourth' — just perform that ONE addition against the current data; do NOT judge it "
    "'already done' because you recall adding something earlier, and do NOT reconcile to a remembered target "
    "count. And NEVER delete, revert, or 'correct' existing data to match what you expected — data that "
    "differs from your memory is Jared's own manual change, not a mistake. Do exactly what the request asks."
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    """UTC now as an ISO-8601 string (what Supabase timestamptz expects)."""
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"{now_iso()} [{WORKER_ID}] {msg}", flush=True)


def headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Supabase REST wrappers — these NEVER raise; they log and return a safe default
# so a transient HTTP blip can't kill the loop.
# --------------------------------------------------------------------------- #


_net_fail_at = 0.0   # monotonic time of this worker's last network-level Supabase failure


def _note_failure(e: Exception) -> None:
    """Record a failure that says the LINK is bad: transport errors and 5xx. A 4xx is a bad query, which
    says nothing about the network, and must not take a healthy worker out of rotation."""
    global _net_fail_at
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
        return
    _net_fail_at = time.monotonic()


def degraded() -> bool:
    return _net_fail_at > 0 and time.monotonic() - _net_fail_at < NET_DEGRADED_S


class Supabase:
    def __init__(self, client: httpx.Client):
        self.c = client

    def _url(self, table: str, query: str = "") -> str:
        u = f"{SUPABASE_URL}/rest/v1/{table}"
        return f"{u}?{query}" if query else u

    def get(self, table: str, query: str = "") -> list:
        try:
            r = self.c.get(self._url(table, query), headers=headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 — defensive on purpose
            _note_failure(e)
            log(f"GET {table}?{query} failed: {e}")
            return []

    def post(self, table: str, body, extra_headers: dict | None = None) -> list | None:
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.post(self._url(table), headers=h, json=body)
            r.raise_for_status()
            # return=representation gives JSON back; plain inserts give 201 + empty body
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            _note_failure(e)
            log(f"POST {table} failed: {e}")
            return None

    def patch(self, table: str, query: str, body, extra_headers: dict | None = None):
        try:
            h = headers()
            if extra_headers:
                h.update(extra_headers)
            r = self.c.patch(self._url(table, query), headers=h, json=body)
            r.raise_for_status()
            if r.content:
                try:
                    return r.json()
                except Exception:  # noqa: BLE001
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            _note_failure(e)
            log(f"PATCH {table}?{query} failed: {e}")
            return None

    def upsert(self, table: str, body, on_conflict: str):
        try:
            r = self.c.post(
                self._url(table, f"on_conflict={on_conflict}"),
                headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                json=body,
            )
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            _note_failure(e)
            log(f"UPSERT {table} failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Heartbeat / priority
# --------------------------------------------------------------------------- #


# This PROCESS, as opposed to this host. Claims carry it (`claim_epoch`) and the heartbeat publishes
# it, so a claim left by a process that died and came back under the same WORKER_ID — a restart
# mid-run, a deploy, a SIGKILL at the unit's stop timeout — is recognisable as orphaned even though
# the host's heartbeat is fresh.
WORKER_EPOCH = uuid.uuid4().hex[:12]
_stopping = False   # set by the signal handler: others stop deferring to a worker on its way out


def heartbeat(db: Supabase) -> None:
    info = {"epoch": WORKER_EPOCH}
    if degraded():
        info["degraded"] = True
    if _stopping:
        info["stopping"] = True
    db.upsert("worker_presence", {"worker": WORKER_ID, "last_seen": now_iso(), "info": info},
              on_conflict="worker")


def _parse_ts(s: str):
    if not s:
        return None
    try:
        # Supabase returns e.g. 2026-06-20T00:11:00.124643+00:00
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None


# worker_presence reads are cached briefly: heartbeats only update every HEARTBEAT_INTERVAL_S (30s),
# so a fresher read returns identical data, and every consumer tolerates HEARTBEAT_FRESH_S (90s) of
# staleness — reading it on every claim iteration was just egress noise.
_presence_cache = {"rows": None, "at": 0.0}
PRESENCE_CACHE_S = 30.0

# True while this worker is standing by (deferring to a healthy higher-priority worker). Set each claim
# attempt in claim_one; read by the heartbeat thread and the main loop to pick the slow standby cadence.
# A plain bool is safe here: one writer (the main loop), and the GIL makes the read/write atomic.
_standby = False
# While standing by with something pending: the monotonic moment the preferred worker's chance to claim it
# runs out. The main loop sleeps to exactly then instead of its slow standby backstop — a clock arrival,
# not a poll — and claim_one stops deferring once it has passed.
_failover_at = 0.0
# Set while a claim is deliberately deferred because a mic is still hearing him. The main loop looks
# again in a second rather than waiting out the idle backstop, so the hold costs ~1s, not ~15.
_holding = False


def presence_rows(db: Supabase) -> list:
    now = time.monotonic()
    if _presence_cache["rows"] is None or now - _presence_cache["at"] > PRESENCE_CACHE_S:
        _presence_cache["rows"] = db.get("worker_presence", "select=worker,last_seen,info") or []
        _presence_cache["at"] = now
    return _presence_cache["rows"]


def _healthy(row: dict, window_s: float = HEARTBEAT_FRESH_S) -> bool:
    """A presence row that is fresh AND not reporting a degraded link or a shutdown."""
    info = row.get("info") or {}
    if info.get("degraded") or info.get("stopping"):
        return False
    ts = _parse_ts(row.get("last_seen", ""))
    return bool(ts) and (datetime.now(timezone.utc) - ts).total_seconds() <= window_s


def other_worker_healthy(db: Supabase, exclude: "set | frozenset" = frozenset()) -> bool:
    """Whether another VOICE worker (not a desk session host, which beats into the same table) is healthy
    and not in `exclude` — the workers a request has already failed on."""
    return bool(untried_workers(db, {WORKER_ID} | set(exclude)))


def higher_priority_target(db: Supabase, exclude: "set | frozenset" = frozenset()) -> str | None:
    """The highest-ranked healthy worker ABOVE me in WORKER_PRIORITY, not in `exclude`, or None."""
    try:
        my_rank = WORKER_PRIORITY.index(WORKER_ID)
    except ValueError:
        # I'm not in the priority list — assume lowest, defer to nobody specific.
        my_rank = len(WORKER_PRIORITY)
    healthy = {r.get("worker") for r in presence_rows(db) if _healthy(r)}
    return next((w for w in WORKER_PRIORITY[:my_rank] if w not in exclude and w in healthy), None)


def context_owner(db: Supabase) -> str | None:
    """The worker that owns the currently-LIVE hub context window, or None if there is no live window
    (idle past the reset budget, or a new calendar day) or its owner is offline. Derived purely from the
    shared voice_requests log — the worker that claimed the most recent request — so it needs no extra
    state and every worker computes the same answer. This is what lets a live context stay on its owner
    even when a higher-priority worker is available: only a reset or the owner going offline releases it.
    (One tiny indexed lookup per claim attempt — a single ~60-byte row.)"""
    rows = db.get(
        "voice_requests",
        "claimed_by=not.is.null&order=claimed_at.desc&limit=1&select=claimed_by,claimed_at",
    )
    if not rows:
        return None
    owner = rows[0].get("claimed_by")
    claimed_at = _parse_ts(rows[0].get("claimed_at") or "")
    if not owner or not claimed_at:
        return None
    now = datetime.now(timezone.utc)
    age = (now - claimed_at).total_seconds()
    # Same "is the window still live?" test the local session uses: idle budget + calendar-day rollover.
    if age > session_reset_s():
        return None
    if claimed_at.astimezone().date() != now.astimezone().date():
        return None
    # An owner reporting a degraded link releases affinity at once: keeping the conversation on one
    # machine is not worth minutes per command.
    if any(r.get("worker") == owner and (r.get("info") or {}).get("degraded") for r in presence_rows(db)):
        return None
    # Liveness: a claim IS proof the owner was running. If it claimed within the grace window, treat the
    # owner as alive WITHOUT a presence read — this survives a briefly-unavailable presence query (e.g. the
    # Mac just woke and its worker_presence GET timed out), which otherwise let a waking higher-priority
    # worker wrongly STEAL a live window from the owner that had handled a command seconds earlier.
    if age <= OWNER_GRACE_S:
        return owner
    # Older claim (still inside the window): fall back to the heartbeat to confirm the owner is still up.
    for row in presence_rows(db):
        if row.get("worker") == owner:
            ts = _parse_ts(row.get("last_seen", ""))
            if ts and (now - ts).total_seconds() <= OWNER_GRACE_S:
                return owner
            return None   # owner went offline → release affinity so a fallback can take over
    return None


def defer_target(db: Supabase, tried: "set | frozenset" = frozenset()) -> str | None:
    """The worker THIS one should leave a request to, or None to claim it. Affinity first: a live context
    window owned by a DIFFERENT worker that is still online keeps the conversation there, over priority;
    if I own it, I claim. Otherwise priority ordering. A request other workers have already failed on
    (`tried`) skips affinity — the owner is likely the worker that just failed it — and goes to the
    highest-ranked worker that has not tried it."""
    if tried:
        return higher_priority_target(db, tried)
    owner = context_owner(db)
    if owner is not None:
        return None if owner == WORKER_ID else owner
    return higher_priority_target(db)


def should_defer(db: Supabase) -> bool:
    return defer_target(db) is not None


# Tools that only read. Running a turn again after any of these repeats nothing, so a failure after
# them is still handed on; any other tool marks the turn as having acted.
REPLAY_SAFE = {"WebSearch", "WebFetch", "Read", "feeder_status", "food_level"}


def replay_safe(tool: str) -> bool:
    return tool.startswith(("get_", "list_", "search_", "find_")) or tool in REPLAY_SAFE


# THE LAST TRANSCRIBER. When Groq and OpenAI both fail, the hub keeps the clip and queues the row
# 'untranscribed' (status-hub /api/voice-command) — but only while this Mac's worker is up, since it is
# the only host with a local Whisper. Taken as its realtime UPDATE lands (the one that attaches `audio`),
# and swept once when the socket subscribes, so a row queued while the socket was down is not missed.
WHISPER_CLI = os.path.expanduser(os.environ.get("WHISPER_CLI", "~/whisper.cpp/build/bin/whisper-cli"))
WHISPER_MODEL = os.path.expanduser(os.environ.get("WHISPER_MODEL", "~/whisper.cpp/models/ggml-small.en.bin"))
UNTRANSCRIBED_MAX_S = 75.0    # older than this a queued clip is closed, not transcribed late; under the 90s stuck-transcribing sweep


def local_whisper_ready() -> bool:
    return WORKER_ID == "mac" and os.access(WHISPER_CLI, os.X_OK) and os.path.exists(WHISPER_MODEL)


def transcribe_local(rec: dict) -> None:
    """Take one 'untranscribed' row: download its clip, run it through local Whisper, and hand it to the
    workers as 'pending'. The take is an atomic untranscribed→transcribing PATCH, so it runs once."""
    rid, audio = rec.get("id"), rec.get("audio") or {}
    if not (rid and audio.get("path")) or not local_whisper_ready():
        return
    hdr = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    url = f"{SUPABASE_URL}/rest/v1/voice_requests?id=eq.{rid}"
    try:
        got = httpx.patch(url + "&status=eq.untranscribed", json={"status": "transcribing", "updated_at": now_iso()},
                          headers={**hdr, "Content-Type": "application/json", "Prefer": "return=representation"},
                          timeout=10).json()
        if not got:
            return
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as d:
            src, wav = os.path.join(d, "clip"), os.path.join(d, "clip.wav")
            r = httpx.get(f"{SUPABASE_URL}/storage/v1/object/voice-clips/{audio['path']}", headers=hdr, timeout=30)
            r.raise_for_status()
            open(src, "wb").write(r.content)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", wav],
                           check=True, timeout=60)
            out = subprocess.run([WHISPER_CLI, "-m", WHISPER_MODEL, "-f", wav, "-l", "en", "--no-prints", "-nt"],
                                 check=True, timeout=120, capture_output=True, text=True).stdout
        text = " ".join(out.split()).strip()
        if not text or _NOTHING_SAID.match(text):
            body = {"status": "error", "error": "nothing heard (local Whisper)", "updated_at": now_iso()}
        else:
            body = {"transcript": text, "status": "pending", "transcribed_at": now_iso(), "updated_at": now_iso()}
        httpx.patch(url + "&status=eq.transcribing", json=body,
                    headers={**hdr, "Content-Type": "application/json"}, timeout=10).raise_for_status()
        log(f"local Whisper transcribed {rid} in {time.monotonic() - t0:.1f}s: {text[:80]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"local Whisper failed for {rid}: {e}")
        try:
            httpx.patch(url + "&status=eq.transcribing", json={"status": "error", "error": f"local Whisper failed: {e}"[:300],
                        "updated_at": now_iso()}, headers={**hdr, "Content-Type": "application/json"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        alert_failure("transcription", f"local Whisper failed: {e}",
                      f"Request {rid}: Groq and OpenAI had both failed, and the Mac's local Whisper failed too.")


def sweep_untranscribed() -> None:
    """On subscribe (Mac only): take any queued clip the socket missed while it was down."""
    if not local_whisper_ready():
        return
    try:
        since = (datetime.now(timezone.utc) - timedelta(seconds=UNTRANSCRIBED_MAX_S)).isoformat()
        rows = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_requests",
                         params={"status": "eq.untranscribed", "audio": "not.is.null", "created_at": f"gte.{since}",
                                 "select": "id,audio"},
                         headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10).json()
        for r in rows or []:
            threading.Thread(target=transcribe_local, args=(r,), daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log(f"untranscribed sweep failed: {e}")


def claude_unready() -> str | None:
    """Why this host cannot start a turn, or None when it can. A local check, made per turn: an npm
    self-update of the CLI that dies half way leaves the link dangling or the binary a non-executable
    placeholder, and every turn here then fails the same way until someone repairs the install."""
    if shutil.which(CLAUDE_BIN) is None:
        return f"claude CLI not runnable on {WORKER_ID}: {CLAUDE_BIN}"
    return None


def untried_workers(db: Supabase, tried: set) -> list:
    """Healthy workers in WORKER_PRIORITY that are not in `tried`."""
    return [r.get("worker") for r in presence_rows(db)
            if r.get("worker") in WORKER_PRIORITY and r.get("worker") not in tried and _healthy(r)]


def hand_off(db: Supabase, req: dict, err: str) -> bool:
    """Put a request this worker failed back to 'pending' for a worker that has not tried it yet.

    Only for a turn that called no tool, so running it again cannot repeat anything it did. The workers
    tried so far ride in `failed_on`; none of them claims it again, and the rest skip affinity and
    priority for it (`claim_one`), so the next healthy one takes it at once. False when no healthy
    worker is left untried — the caller then closes it as an error, as before."""
    tried = sorted(set(req.get("failed_on") or []) | {WORKER_ID})
    left = untried_workers(db, set(tried))
    if not left:
        return False
    got = db.patch(
        "voice_requests",
        f"id=eq.{req['id']}&status=eq.claimed&claimed_by=eq.{WORKER_ID}",
        {"status": "pending", "claimed_by": None, "claimed_at": None, "failed_on": tried,
         "updated_at": now_iso()},
        extra_headers={"Prefer": "return=representation"},
    )
    if not got:
        return False
    log(f"handed off request {req['id']} after failing here ({err}); untried and up: {left}")
    push_activity(f"Handing off from {WORKER_ID}", "tool")
    alert_failure("request", err, f"Request {req['id']} ({(req.get('transcript') or '')[:200]!r}) failed "
                  f"here before calling any tool and was handed to another worker (untried and up: {left}).")
    return True


# --------------------------------------------------------------------------- #
# Hub session state
# --------------------------------------------------------------------------- #


def session_file(agent: bool) -> Path:
    """Hub mode and full-agent mode keep SEPARATE, independently-persisted Claude sessions, so switching
    between them never carries over or clobbers the other's context."""
    return AGENT_SESSION_FILE if agent else HUB_SESSION_FILE


def read_session(agent: bool) -> dict | None:
    f = session_file(agent)
    try:
        if f.exists():
            return json.loads(f.read_text())
    except Exception as e:  # noqa: BLE001
        log(f"read {f.name} failed: {e}")
    return None


def write_session(agent: bool, session_id: str) -> None:
    f = session_file(agent)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps({"session_id": session_id, "last_activity_iso": now_iso()})
        )
    except Exception as e:  # noqa: BLE001
        log(f"write {session_file(agent).name} failed: {e}")


# LIVE STATE: the settings a turn consults, the voice focus, and today's hub thread, held in memory.
# Loaded once each time the realtime socket subscribes (it is recycled every ~5 minutes, which makes
# that reload the backstop behind the events) and kept current by its app_settings / voice_focus /
# voice_messages events, so a command reads none of them from the database. Reading them per command
# cost ~0.7s of round trips before a turn could start.
LIVE_SETTINGS = ("agent_mode", "agent_cwd", "session_reset_s", "voice_reset_at")
_live = {"loaded": False, "settings": {}, "focus": "hub", "thread": {}}
_live_lock = threading.Lock()
_live_db: "Supabase | None" = None   # its own client: loads run on the realtime thread


def _is_thread_row(r: dict) -> bool:
    return r.get("focus") == "hub" and r.get("mode") == "hub" and r.get("role") in ("user", "assistant")


def live_load() -> bool:
    """(Re)load the live state from the database. False, leaving it unloaded, if any read failed."""
    global _live_db
    if _live_db is None:
        _live_db = Supabase(httpx.Client(timeout=HTTP_TIMEOUT_S))

    def get(table, query):
        r = _live_db.c.get(_live_db._url(table, query), headers=headers())
        r.raise_for_status()
        return r.json()

    try:
        keys = ",".join(LIVE_SETTINGS)
        settings = {r["key"]: r.get("value") for r in get("app_settings", f"key=in.({keys})&select=key,value")}
        focus = (get("voice_focus", "id=eq.1&select=focus") or [{}])[0].get("focus") or "hub"
        since = day_start(datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        rows: list = []
        while True:
            page = get("voice_messages",
                       "focus=eq.hub&mode=eq.hub&role=in.(user,assistant)"
                       f"&created_at=gte.{since}&order=created_at.asc&limit=1000&offset={len(rows)}"
                       "&select=id,focus,mode,role,text,created_at,ignored,done")
            rows += page
            if len(page) < 1000:
                break
    except Exception as e:  # noqa: BLE001
        _note_failure(e)
        log(f"live state load failed: {e}")
        with _live_lock:
            _live["loaded"] = False
        return False
    with _live_lock:
        _live.update(loaded=True, settings=settings, focus=focus, thread={r["id"]: r for r in rows})
    log(f"live state loaded: {len(rows)} thread rows, focus={focus}")
    return True


def live_event(table: str, kind: str, rec: dict, old: dict) -> None:
    """Apply one realtime change to the live state."""
    with _live_lock:
        if not _live["loaded"]:
            return
        if table == "app_settings":
            key = (rec or old).get("key")
            if key in LIVE_SETTINGS:
                if kind == "DELETE":
                    _live["settings"].pop(key, None)
                else:
                    _live["settings"][key] = rec.get("value")
        elif table == "voice_focus" and kind != "DELETE":
            _live["focus"] = rec.get("focus") or "hub"
        elif table == "voice_messages":
            if kind == "DELETE":
                _live["thread"].pop(old.get("id"), None)
            elif _is_thread_row(rec):
                _live["thread"][rec["id"]] = rec
            else:
                _live["thread"].pop(rec.get("id"), None)


def _live_view() -> dict:
    """The live state, loading it first if no subscribe has done so yet (startup, or a failed load)."""
    if not _live["loaded"]:
        live_load()
    with _live_lock:
        return {"settings": dict(_live["settings"]), "focus": _live["focus"],
                "thread": list(_live["thread"].values())}


def session_reset_s() -> float:
    """Idle budget before the hub conversation resets, from the hub setting `session_reset_s`."""
    try:
        return float(_live_view()["settings"].get("session_reset_s") or SESSION_IDLE_RESET_S)
    except (TypeError, ValueError):
        return SESSION_IDLE_RESET_S


def agent_settings() -> dict:
    """Full-agent-mode knobs from app_settings: `agent_mode` (bool — upgrade the subscription path to the
    unrestricted Claude Code agent) and `agent_cwd` (where that agent runs; default $HOME)."""
    s = _live_view()["settings"]
    cwd = (s.get("agent_cwd") or "").strip()
    return {"mode": str(s.get("agent_mode", "")).strip().lower() in ("1", "true", "on", "yes"),
            "cwd": os.path.expanduser(cwd) if cwd else os.path.expanduser("~")}


AO_IGNORE_STREAK_OFF = 3    # consecutive ignored turns that put the brakes on… (Jared's numbers)
AO_IGNORE_WINDOW_S = 300    # …when they all land within this window — a rate, not a lifetime count


def maybe_auto_disable_wakefree() -> None:
    """A run of nothing-but-noise means nobody is talking TO the assistant: switch always-on off and let
    the wake words take over (they stay live when wake-free is off, so voice never goes dark). The streak
    is DERIVED from the last hub rows rather than counted in memory, so it reads the same whichever
    worker judged each turn; any acted-on command breaks it, and a streak that straddles a quiet gap
    (older than the window) doesn't count — only a currently-noisy room flips the mode."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_messages",
                      params={"select": "ignored,created_at", "focus": "eq.hub", "role": "eq.user",
                              # id is a UUID — ordering by it is lexical, not recency. created_at only.
                              "order": "created_at.desc", "limit": str(AO_IGNORE_STREAK_OFF)},
                      headers=headers(), timeout=5)
        rows = r.json()
        if len(rows) < AO_IGNORE_STREAK_OFF or not all(row.get("ignored") for row in rows):
            return
        oldest = datetime.fromisoformat(str(rows[-1]["created_at"]).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - oldest).total_seconds() > AO_IGNORE_WINDOW_S:
            return
        g = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "eq.wakefree_enabled", "select": "value"},
                      headers=headers(), timeout=5)
        grows = g.json()
        if not (grows and str(grows[0].get("value")).strip().lower() in ("1", "true", "on", "yes")):
            return   # already off — nothing to flip
        # Marker FIRST, then the flip: an auto-off must stay distinguishable from Jared turning
        # always-on off by voice — only a MARKED off self-heals after the cooldown below.
        httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                   headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                   json={"key": "wakefree_autooff_at", "value": now_iso(), "updated_at": now_iso()},
                   timeout=10)
        httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                   headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                   json={"key": "wakefree_enabled", "value": "0", "updated_at": now_iso()},
                   timeout=10)
        log(f"always-on auto-disabled: last {AO_IGNORE_STREAK_OFF} turns all noise within the hour")
    except Exception as e:  # noqa: BLE001 — the governor must never take a turn down
        log(f"wakefree auto-off check failed: {e}")


AO_AUTOOFF_COOLDOWN_S = 300     # minimum brake hold before a re-arm is even considered
AO_REARM_QUIET_S = 180          # …and every mic's heard_jared_at must be at least this stale (room died down)
_ao_reenable_check = {"t": 0.0, "interval": 300.0}


def maybe_auto_reenable_wakefree() -> None:
    """The governor's auto-off holds until the ROOM GOES QUIET, then re-arms always-on. During red the
    capture layer keeps ECAPA warm as a passive sensor and stamps voice_presence.heard_jared_at on
    confirmed voice — no model judgments — so an ongoing conversation keeps the brake on and a
    died-out one releases it: re-arm requires the minimum hold AND AO_REARM_QUIET_S of silence from
    every mic. A stale/absent stamp reads as quiet, so a dead sensor can only re-arm EARLIER, never
    wedge it red (and a still-noisy room then just re-trips on the next ignored streak). A MANUAL off
    (spoken command → set_always_on) writes no marker and never self-heals — only the governor's
    marked flips do. Runs from the heartbeat thread on every worker: one check per ~5 min normally,
    tightened to ~60s while a brake is active (idempotent across workers)."""
    now = time.monotonic()
    if now - _ao_reenable_check["t"] < _ao_reenable_check["interval"]:
        return
    _ao_reenable_check["t"] = now
    _ao_reenable_check["interval"] = 300.0
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                      params={"key": "in.(wakefree_enabled,wakefree_autooff_at)", "select": "key,value"},
                      headers=headers(), timeout=5)
        s = {row["key"]: row["value"] for row in r.json()}
        marker = (s.get("wakefree_autooff_at") or "").strip()
        enabled = str(s.get("wakefree_enabled", "0")).strip().lower() in ("1", "true", "on", "yes")

        def _set(k, v):
            httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                       headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                       json={"key": k, "value": v, "updated_at": now_iso()}, timeout=10)

        if enabled:
            if marker:
                _set("wakefree_autooff_at", "")   # re-enabled by hand before the cooldown — clear the stale marker
            return
        if not marker:
            return   # manual off — stays off until Jared says otherwise
        _ao_reenable_check["interval"] = 60.0   # brake active → watch for the release more closely
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(marker.replace("Z", "+00:00"))).total_seconds()
        if age < AO_AUTOOFF_COOLDOWN_S:
            return
        p = httpx.get(f"{SUPABASE_URL}/rest/v1/voice_presence",
                      params={"select": "heard_jared_at"}, headers=headers(), timeout=5)
        stamps = []
        for row in p.json():
            v = row.get("heard_jared_at")
            if v:
                try:
                    stamps.append(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
                except ValueError:
                    pass
        if stamps and (datetime.now(timezone.utc) - max(stamps)).total_seconds() < AO_REARM_QUIET_S:
            return   # the room is still talking — hold the brake
        _set("wakefree_enabled", "1")
        _set("wakefree_autooff_at", "")
        log(f"always-on auto-re-enabled: quiet after {int(age)}s braked")
    except Exception as e:  # noqa: BLE001 — never take the heartbeat down
        log(f"wakefree auto-reenable check failed: {e}")


def publish_repo_dirs() -> None:
    """Mac-only: scan the home tree for git repos and publish the list to app_settings.agent_cwd_options
    (JSON array) so the hub's full-agent working-directory picker offers the actual repos. The agent runs on
    whichever worker claims a request, but the Mac is primary, so its paths drive the dropdown; run_claude
    falls back to $HOME if a published path doesn't exist on the worker that ends up running it."""
    if WORKER_ID != "mac":
        return
    home = os.path.expanduser("~")
    found = set()
    for root in (home, os.path.join(home, "repos")):
        try:
            for entry in os.scandir(root):
                if entry.is_dir() and os.path.isdir(os.path.join(entry.path, ".git")):
                    found.add(entry.path)
        except OSError:
            pass
    opts = sorted(found)
    try:
        httpx.post(f"{SUPABASE_URL}/rest/v1/app_settings?on_conflict=key",
                   headers={**headers(), "Prefer": "resolution=merge-duplicates"},
                   json={"key": "agent_cwd_options", "value": json.dumps(opts), "updated_at": now_iso()},
                   timeout=10)
        log(f"published {len(opts)} repo dirs for the agent cwd picker")
    except Exception as e:  # noqa: BLE001
        log(f"publish_repo_dirs failed: {e}")


def should_reset_session(state: dict | None, agent: bool = False) -> bool:
    """Reset (start fresh) if: no state, idle > the reset budget, or it's a new calendar day. In full-agent
    mode the conversation resets MANUALLY only — a long coding session shouldn't be wiped by an idle gap or a
    midnight rollover (switching the mode off triggers a reset via the hub), so only a missing session forces
    a fresh start."""
    if not state or not state.get("session_id"):
        return True
    if agent:
        return False
    last = _parse_ts(state.get("last_activity_iso", ""))
    if not last:
        return True
    now = datetime.now(timezone.utc)
    if (now - last).total_seconds() > session_reset_s():
        return True
    if last.astimezone().date() != now.astimezone().date():
        return True
    return False


# The hub agent's day: it begins at 4 AM in Jared's own timezone, not at midnight and not in the host's
# zone (the Fly worker runs in UTC). The same boundary the hub uses for every "today" (lib/dates.js).
DAY_TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "America/New_York"))
DAY_STARTS_HOUR = 4


def day_start(now: datetime) -> datetime:
    """The start of the day `now` belongs to, as an aware datetime."""
    local = now.astimezone(DAY_TZ)
    start = local.replace(hour=DAY_STARTS_HOUR, minute=0, second=0, microsecond=0)
    return start - timedelta(days=1) if local < start else start


def recent_context() -> str:
    """THE WHOLE DAY, replayed into a FRESH claude run instead of using --resume. We keep our own clean
    transcript in voice_messages (each turn's user prompt + final assistant reply), so a fresh process
    gets exactly the conversation and NONE of the CLI-session baggage — tool schemas, thinking blocks,
    the __ESCALATE__ sentinel — that let the two models cross-contaminate one shared session.

    Everything since the day began (4 AM, `day_start`), or since a manual reset from the hub if that is
    later, oldest first, each line with its time — so he can refer back to anything said earlier today,
    from any surface. An idle gap does not cut it: a quiet afternoon is still today. A day is small: a
    heavy one measured ~4.5k tokens of real exchanges plus ~6k of marked room chatter. Tool RESULTS are
    deliberately omitted — a follow-up that needs live data just re-fetches with a tool."""
    now = datetime.now(timezone.utc)
    since = day_start(now)
    live = _live_view()
    reset = _parse_ts(live["settings"].get("voice_reset_at") or "")
    if reset and reset > since:
        since = reset
    rows = sorted((r for r in live["thread"]
                   if r.get("done") and (_parse_ts(r.get("created_at") or "") or since) >= since),
                  key=lambda r: r.get("created_at") or "")
    # An ignored turn is marked rather than hidden. Unmarked, room chatter would replay as things Jared
    # said TO the assistant; marked, he can point at one — "do the one you ignored" — and the model can
    # see both what was said and that nothing was done about it.
    def _line(r):
        ts = _parse_ts(r.get("created_at") or "")
        when = ts.astimezone(DAY_TZ).strftime("%-I:%M %p") if ts else "?"
        who = 'You' if r.get('role') == 'user' else 'Assistant'
        txt = (r.get('text') or '').strip()
        if r.get('ignored'):
            return f"[{when}] You (ignored as room noise, not acted on): {txt}"
        return f"[{when}] {who}: {txt}"
    # A bare sentinel reply is dropped from the replay. It is never shown as a reply — but a deliberate
    # turn that emitted one anyway was saved as `Assistant: __IGNORE__`, and replaying that line taught
    # every later button press the sentinel by example, so they answered with it too.
    lines = [_line(r) for r in rows
             if (r.get('text') or '').strip() and not (r.get('role') == 'assistant' and is_ignore(r.get('text')))]
    if not lines:
        return ""
    now_local = now.astimezone(DAY_TZ).strftime("%-I:%M %p")
    return ("[Today so far: everything said between you since the day began, oldest first, each line "
            "with its time. Context only; tool RESULTS are not included, so call a tool if you need "
            f"current data. It is now {now_local}.\n" + "\n".join(lines) + "]\n\n")


# --------------------------------------------------------------------------- #
# claude headless invocation
# --------------------------------------------------------------------------- #


def fetch_attachments(paths: list) -> tuple[str | None, list[str]]:
    """Download a request's attachment objects (private voice-attachments bucket, flat uuid.ext names)
    to a temp dir so the CLI's Read tool can view them. Returns (tmpdir, local_paths); tmpdir is None
    when nothing landed. Caller removes tmpdir after the run."""
    tmpdir = None
    local: list[str] = []
    for p in paths:
        name = str(p).strip().lstrip("/")
        if not re.fullmatch(r"[\w.\-]+", name):   # flat bucket — reject anything path-like
            log(f"attachment path rejected: {p!r}")
            continue
        try:
            r = httpx.get(f"{SUPABASE_URL}/storage/v1/object/voice-attachments/{name}",
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                          timeout=30)
            if r.status_code != 200 or not r.content:
                log(f"attachment fetch failed ({r.status_code}): {name}")
                continue
            if tmpdir is None:
                tmpdir = tempfile.mkdtemp(prefix="voice-att-")
            fp = os.path.join(tmpdir, name)
            with open(fp, "wb") as f:
                f.write(r.content)
            local.append(fp)
        except Exception as e:  # noqa: BLE001
            log(f"attachment fetch error: {name}: {e}")
    return tmpdir, local


# The current command's audio-recording START time (voice_requests.capture_start), written here per
# command. The data MCP's now() reads it so a voice-logged SET lands at when Jared actually spoke — his
# rest-clock reference — not ~30s later when the command finishes the pipeline.
#
# It is a FILE and not an env value, and that distinction is the whole point. The warm fast path
# (SdkRunner) starts its MCP servers ONCE per worker lifetime as warm stdio sessions, so anything placed in the
# subprocess environment is fixed for as long as the worker runs: passing the timestamp that way froze
# it at one moment and stamped 29 hours of writes with it (Aug 31, found 2026-09-01). The path is
# constant — safe to bake into a warm session — while the contents change per command.
CMD_TS_FILE = STATE_DIR / "cmd_ts"


def set_cmd_ts(value: str | None) -> None:
    """Publish (or clear) the in-flight command's capture_start for the data MCP."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CMD_TS_FILE.write_text(value or "")
    except Exception as e:  # noqa: BLE001
        log(f"cmd_ts write failed: {e}")   # a stale stamp is worse than none, so this is never fatal


# Where Jared was for the in-flight command, one line, for the data MCP to hand a session it starts.
# A file for the same warm-session reason as CMD_TS_FILE; rewritten (emptied when the device sent no
# position) at every command, so it can never be a previous turn's.
CMD_LOC_FILE = STATE_DIR / "cmd_loc"


def describe_location(loc) -> str:
    """The device's place name when it gave one, then the fix itself — or '' with no position."""
    if not isinstance(loc, dict) or loc.get("lat") is None or loc.get("lon") is None:
        return ""
    fix = f"{loc['lat']}, {loc['lon']}" + (f" (within about {loc['acc']} m)" if loc.get("acc") is not None else "")
    return f"{loc['place']} ({fix})" if loc.get("place") else fix


def set_cmd_loc(line: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CMD_LOC_FILE.write_text(line or "")
    except Exception as e:  # noqa: BLE001
        log(f"cmd_loc write failed: {e}")


def write_mcp_config() -> str:
    env = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
    # DATA_CALLER=hub marks sessions this worker dispatches, so their first reply is relayed back
    # into the hub thread (data-layer startSession → orchestrator/src/relay.js).
    data_env = {**env, "DATA_TS_FILE": str(CMD_TS_FILE), "DATA_LOC_FILE": str(CMD_LOC_FILE),
                "DATA_CALLER": "hub"}
    cfg = {
        "mcpServers": {
            "data": {"command": "node", "args": [DATA_MCP_JS], "env": data_env},
            "voice": {"command": "node", "args": [VOICE_MCP_JS], "env": env},
            "govee-lights": {"command": GOVEE_PY, "args": [GOVEE_MCP_PY], "env": env},
            "petlibro-feeder": {"command": PETLIBRO_PY, "args": [PETLIBRO_MCP_PY], "env": env},
        }
    }
    if os.path.exists(GMAIL_MCP_PY):
        cfg["mcpServers"]["gmail"] = {"command": GMAIL_PY, "args": [GMAIL_MCP_PY]}
    if HAS_MESSAGES:
        cfg["mcpServers"]["messages"] = {"type": "http", "url": MESSAGES_MCP_URL, "headers": MESSAGES_HEADERS}
    # alwaysLoad makes Claude Code connect a server BEFORE the first turn. Without it, `-p` connects
    # --mcp-config servers in the background and the model's first request carries only whichever ones
    # had finished starting: on Fly the voice server (~1.6s) made it and data (~2s) did not, so the
    # assistant answered "I don't have a nutrition tool" with the tool seconds away.
    for srv in cfg["mcpServers"].values():
        srv["alwaysLoad"] = True
    fd, path = tempfile.mkstemp(prefix="voice-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def _kill_group(proc) -> None:
    """Kill a run and everything it started. A grandchild holding stdout open would otherwise keep the
    stream reader from ever reaching EOF, and the claim with it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_claude(transcript: str, resume_session_id: str | None, on_event=None, system_append: str = "",
               model: str = HUB_MODEL, agent: bool = False, cwd: str | None = None,
               allow_read: bool = False, allowed_tools: "list | None" = None,
               deny_tools: "list | None" = None) -> dict:
    """Run `claude -p` headless and STREAM the stream-json output, parsing events as they arrive.

    on_event(kind, payload) fires live during the run so progress can be surfaced — currently
    on_event("tool", {"name", "input"}) for each tool call. Returns {"session_id", "text",
    "result", "ok", "raw_error"}.

    Two shapes:
      · HUB (agent=False): tool allowlist restricted to the MCP servers — no Bash/Edit/Read. In headless
        `-p` mode a tool not in --allowedTools is DENIED (not prompted), so the run never hangs.
      · FULL AGENT (agent=True): the unrestricted Claude Code agent — every native tool executes (via
        --dangerously-skip-permissions, since `-p` can't prompt) ALONGSIDE the MCP tools, runs in `cwd`,
        and gets the longer AGENT_TIMEOUT_S. No hub framing; an all-purpose voice agent (coding, etc.).
    """
    mcp_path = write_mcp_config()
    cmd = [CLAUDE_BIN, "-p", transcript]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += [
        "--model", model,
        "--mcp-config", mcp_path,
        "--strict-mcp-config",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if agent:
        cmd += ["--dangerously-skip-permissions"]   # all native tools execute; MCP tools still present
    else:
        allowed = list(allowed_tools) if allowed_tools is not None else list(ALL_TOOLS)
        if allow_read:
            allowed.append("Read")   # photo turns only: Read is how the CLI views image files
        cmd += ["--allowedTools", *allowed]   # a tool not listed is DENIED (not prompted) in -p mode
        # Only this run's flags decide what it may do. Claude Code otherwise also applies the host
        # user's settings, and the Mac's pre-approve the data, gmail and messages tools in auto mode,
        # which widens the list above. The worker runs from its own directory, so "project" loads none.
        cmd += ["--setting-sources", "project"]
    # Out of the schema, not just refused: the browser-only tools on every run, and the sends on a
    # room-mic turn. A deny beats any allow rule, and neither is something a turn should escalate for,
    # so taking them out of sight loses nothing.
    cmd += ["--disallowedTools", *SESSION_TOOLS, *(deny_tools or [])]
    if system_append:
        cmd += ["--append-system-prompt", system_append]
    timeout_s = AGENT_TIMEOUT_S if agent else CLAUDE_TIMEOUT_S
    if cwd and not os.path.isdir(cwd):   # a published path may not exist on the worker that runs it
        log(f"agent cwd {cwd!r} not present here; falling back to $HOME")
        cwd = os.path.expanduser("~")

    session_id = None
    assistant_chunks: list[str] = []
    result_text = ""
    raw_error = None
    proc = None
    timed_out = {"v": False}
    t_spawn = t_init = t_first = None   # timing seams: spawn → MCP-ready (system init) → first model token
    n_tools = 0

    # Subscription path: `claude -p` uses the logged-in Claude subscription. ANTHROPIC_API_KEY is popped
    # from the environment at import, so no subprocess can inherit it and silently bill the API.
    sub_env = os.environ.copy()
    # Load ALL MCP tool schemas inline every turn — no lazy tool-search/discovery round-trip. This is a
    # tool-centric agent (its whole job is calling these tools), and the trimmed toolset is well under the
    # context budget, so we want every tool callable directly. (Default 'auto' defers above 10% of context.)
    sub_env["ENABLE_TOOL_SEARCH"] = "false"

    try:
        t_spawn = time.monotonic()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=sub_env, cwd=cwd,  # line-buffered; cwd is where a full agent operates
            start_new_session=True,   # its own process group, so a kill reaches anything it spawned
        )
        # Watchdog: kill the run if it exceeds the timeout, which ends the streaming loop at EOF.
        done = threading.Event()

        def _watchdog():
            if not done.wait(timeout_s):
                timed_out["v"] = True
                _kill_group(proc)

        threading.Thread(target=_watchdog, daemon=True).start()

        # Drain stderr concurrently so it can't fill its pipe and deadlock the stdout reader below.
        err_buf: list[str] = []

        def _drain_err():
            try:
                for el in proc.stderr:
                    err_buf.append(el)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_drain_err, daemon=True).start()

        # Stream events as claude emits them (one JSON object per line).
        said = []   # text since the last tool call: progress once a tool follows it, else the reply
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            etype = ev.get("type")

            if etype == "system":  # init event = process up + MCP servers loaded (the cold-start boundary)
                if t_init is None:
                    t_init = time.monotonic()
                if ev.get("session_id"):
                    session_id = ev["session_id"]

            elif etype == "assistant":  # text lives in content[*].text; tool calls in content[*].tool_use
                if t_first is None:     # first model output → end of "thinking before it says anything"
                    t_first = time.monotonic()
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        assistant_chunks.append(block.get("text", ""))
                        said.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        n_tools += 1
                        tname = block.get("name", "")
                        if on_event:
                            try:
                                if "".join(said).strip():
                                    on_event("text", {"text": "\n".join(said)})
                                on_event("tool", {"name": tname, "input": block.get("input")})
                            except Exception:  # noqa: BLE001
                                pass
                        said = []
                if ev.get("session_id"):
                    session_id = ev["session_id"]

            elif etype == "result":  # final event
                if ev.get("session_id"):
                    session_id = ev["session_id"]
                if isinstance(ev.get("result"), str):
                    result_text = ev["result"]
                if ev.get("is_error"):
                    raw_error = ev.get("result") or "claude reported is_error"

        done.set()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:   # stdout closed but the process lingers: the reply is already in
            _kill_group(proc)
        stderr = "".join(err_buf)
        t_end = time.monotonic()
        if t_spawn:                       # latency breakdown of this run (where the seconds actually go)
            cold = f"{t_init - t_spawn:.1f}s" if t_init else "n/a"
            ttf  = f"{t_first - t_init:.1f}s" if (t_first and t_init) else "n/a"
            gen  = f"{t_end - t_first:.1f}s" if t_first else "n/a"
            log(f"timing: cold-start(spawn→mcp) {cold} | first-token {ttf} | "
                f"gen+{n_tools}tools {gen} | total {t_end - t_spawn:.1f}s | "
                f"model={model} resume={'y' if resume_session_id else 'n'} agent={'y' if agent else 'n'}")

        if timed_out["v"]:
            return {"session_id": session_id, "text": "", "result": "",
                    "ok": False, "raw_error": f"claude timed out after {CLAUDE_TIMEOUT_S}s"}
        if proc.returncode not in (0, None) and not result_text and not assistant_chunks:
            raw_error = raw_error or f"claude exited {proc.returncode}: {stderr[:500]}"

        text = result_text or "\n".join(c for c in assistant_chunks if c).strip()
        return {
            "session_id": session_id,
            "text": text,
            "result": result_text,
            "ok": raw_error is None and bool(text),
            "raw_error": raw_error,
        }
    except Exception as e:  # noqa: BLE001
        if proc is not None:
            _kill_group(proc)
        return {
            "session_id": session_id,
            "text": "",
            "result": "",
            "ok": False,
            "raw_error": f"claude invocation failed: {e}",
        }
    finally:
        try:
            os.unlink(mcp_path)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Request handling
# --------------------------------------------------------------------------- #


_SDK_LOOP_LOCK = threading.Lock()


class SdkRunner:
    """Warm Claude Agent SDK client — the PRIMARY fast path, billed to the subscription.

    One long-lived headless Claude Code process holds the four MCP servers, so the per-command spawn
    spawn cost is paid once, not per turn. Each hub turn runs in a FRESH conversation (`/clear` on the
    same warm process, see _turn): the caller replays the day's hub conversation into the prompt. Pinned to Sonnet +
    SAFE_TOOLS at connect; escalation and full-agent turns keep their own `claude -p` spawns (full
    toolset). A failed turn is retried by `run_subscription` and then surfaced; errors never upgrade the
    model and never move a turn onto the paid API.
    """

    def __init__(self):
        self._loop = None
        self._client = None
        self._ambient = True   # the turn in flight came from a room mic; set by run() before each turn
        self._used = False     # the warm client has run a turn since it connected, so needs a /clear
        self._lock = None      # serialises connects; created on the SDK loop

    def _start_loop(self):
        with _SDK_LOOP_LOCK:   # warm() runs on its own thread, so two callers can race here
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="sdk-loop", daemon=True).start()

    async def _ensure(self):
        if self._client is not None:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()   # warm() and a first turn can both get here; connect once
        async with self._lock:
            if self._client is None:
                await self._connect()

    def warm(self) -> None:
        """Connect now rather than on the next turn. Measured cold, the first command after a restart
        took 19.7s on Fly and 9.9s on the Pi where a warm one took 2.8s and 4.5s; every deploy, restart
        and failed turn (`_recycle`) used to hand that cost to whatever he said next."""
        if claude_unready():
            return
        self._start_loop()
        try:
            asyncio.run_coroutine_threadsafe(self._ensure(), self._loop).result(timeout=120)
        except Exception as e:  # noqa: BLE001
            log(f"sdk runner: warm-up failed ({e}); the next turn connects")

    async def _connect(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher, PermissionResultDeny

        gated = "This tool is gated on the fast path. If the request needs it, reply __ESCALATE__ (alone)."
        exact = set(SAFE_TOOLS)
        servers = tuple(t + "__" for t in SAFE_TOOLS if t.startswith("mcp__") and t.count("__") == 1)

        def _decide(decision, reason=None):
            out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
            if reason:
                out["permissionDecisionReason"] = reason
            return {"hookSpecificOutput": out}

        async def _fence(input_data, tool_use_id, context):  # noqa: ARG001
            # The fast path's allow list, enforced BEFORE any permission rule. This client is Claude
            # Code, which also applies the host user's own settings — and the Mac's pre-approve every
            # data, gmail and messages tool in auto mode. With allowed_tools alone, Sonnet on the Mac
            # could call the gated nutrition tools directly, and a room-mic turn sent an email in the
            # first test of the send rule. A hook's deny outranks any allow rule, so the list holds here.
            name = input_data.get("tool_name") or ""
            if name in SEND_TOOLS:
                # The one per-turn decision: the client is warm across turns, so only the flag run()
                # sets can say which mic this turn came from.
                if self._ambient:
                    return _decide("deny", "Sending texts and email is off for the always-on room mics. "
                                           "He can send it with the button on his phone or watch, or by typing it.")
                return _decide("allow")
            if name in exact or name.startswith(servers):
                return {}
            return _decide("deny", gated)

        async def _deny(tool_name, tool_input, context):  # noqa: ARG001
            # Reached only by a tool the fence let through without pre-approval; there is none today.
            # (Without a callback the SDK raises on a permission request, failing the whole turn.)
            return PermissionResultDeny(message=gated)

        opts = ClaudeAgentOptions(
            model="sonnet",
            # The SDK ships its own copy of Claude Code, which only moves when the package is upgraded, and
            # the alias "sonnet" resolves to whatever model THAT copy knows. Running the installed CLI keeps
            # this path on the same models as every other turn instead of drifting a release behind.
            cli_path=shutil.which(CLAUDE_BIN),
            mcp_servers=write_mcp_config(),
            allowed_tools=list(SAFE_TOOLS),
            disallowed_tools=list(SESSION_TOOLS),   # browser-only: out of sight, so never a reason to escalate
            # The stable hub framing lives here so the prefix caches across turns; per-turn context
            # (the replayed conversation) rides on the prompt instead.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": HUB_SYSTEM + "\n\n" + SHARED_STATE_NOTE,
                           "exclude_dynamic_sections": True},
            env={"ENABLE_TOOL_SEARCH": "false"},   # all schemas inline, same as the -p path
            can_use_tool=_deny,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_fence])]},
            cwd=os.path.expanduser("~"),
        )
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        self._client = client
        self._used = False
        log("sdk runner: warm client up (sonnet, safe toolset)")

    async def _teardown(self):
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _turn(self, prompt, on_event):
        await self._ensure()
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage
        # `/clear` is what makes this turn a fresh conversation; the `session_id` passed to query() is
        # only a label and starts nothing. Without it every turn appends to one conversation that also
        # re-receives the whole day as `ctx`, so it grows quadratically: ~977k tokens after 112 turns,
        # when auto-compaction stalled a turn for 90s and every command queued behind it. ~0.2s.
        if self._used:
            await self._client.query("/clear")
            async for _ in self._client.receive_response():
                pass
        self._used = True
        await self._client.query(prompt, session_id=uuid.uuid4().hex)
        chunks, result_text, ok, raw_error = [], "", True, None
        used_tools = False
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        chunks.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        # The reply is what the model says after its LAST tool call. Text before one
                        # ("searching now") is narration; it goes to the activity stream as progress
                        # instead.
                        said = "\n".join(chunks).strip()
                        chunks = []
                        used_tools = True   # gates the retry in run(); tracked even with no on_event
                        if on_event:
                            try:
                                if said:
                                    on_event("text", {"text": said})
                                on_event("tool", {"name": b.name, "input": b.input})
                            except Exception:  # noqa: BLE001
                                pass
            elif isinstance(msg, ResultMessage):
                ok = not msg.is_error
                result_text = msg.result or ""
                if msg.is_error:
                    raw_error = result_text or msg.subtype
        return {"session_id": None, "text": "\n".join(chunks).strip(), "used_tools": used_tools,
                "result": result_text, "ok": ok, "raw_error": raw_error}

    def _recycle(self):
        """Drop the warm client and connect a fresh one in the background, before the next turn needs it."""
        try:
            asyncio.run_coroutine_threadsafe(self._teardown(), self._loop).result(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self.warm, name="sdk-warm", daemon=True).start()

    def _attempt(self, transcript, on_event) -> dict:
        """One turn on the warm client. `used_tools` reports whether it acted before failing; a crash
        hides that, so it reports True — the conservative answer, since it only suppresses a retry."""
        try:
            fut = asyncio.run_coroutine_threadsafe(self._turn(transcript, on_event), self._loop)
            return fut.result(timeout=CLAUDE_TIMEOUT_S + 30)
        except Exception as e:  # noqa: BLE001 — never wedge the worker
            return {"session_id": None, "text": "", "result": "", "ok": False, "used_tools": True,
                    "raw_error": f"sdk run failed: {e}"}

    def run(self, transcript, on_event=None, ambient=True) -> dict:
        """`ambient` says the turn came from a room mic, which is what _gate reads for the sends.
        Turns run one at a time, so a flag on the runner is exactly one turn's."""
        self._start_loop()
        self._ambient = ambient
        result = self._attempt(transcript, on_event)
        if result.get("ok"):
            return result
        # ANY failure leaves the warm process in an unknown state, so recycle it. Refusals are why the
        # recycle cannot be limited to timeouts and transport errors: the CLI answers "Sonnet 5 can't
        # help with this. Start a new session to continue." through the ORDINARY result path, and then
        # returns that same error for every later turn regardless of content — passing a fresh
        # session_id to an already-connected client does not clear it. On 2026-09-02 one false-triggered
        # ambient capture wedged the client for 85 minutes; every command in that window died behind it.
        self._recycle()
        if result.get("used_tools"):
            return result   # it acted, or may have: never replay side effects
        # Nothing was called, so re-running is side-effect free and keeps the turn on the subscription
        # instead of spending the caller's fallback. A genuinely refusable turn just fails twice, fast.
        log(f"sdk turn failed ({result.get('raw_error')}) — recycled warm client, retrying once")
        retry = self._attempt(transcript, on_event)
        if not retry.get("ok"):
            self._recycle()   # the cold client failed too; don't hand the next turn a used one
        return retry


SDK = SdkRunner()


def get_focus() -> str:
    return _live_view()["focus"]


def inject_to_terminal(text: str) -> tuple[bool, str | None]:
    """Send a spoken line to the focused Mac session's wall terminal (the
    orchestrator writes it to the live pty and presses Enter). Best-effort."""
    try:
        r = httpx.post(
            f"{ORCH_URL}/inject",
            params={"token": ORCH_TOKEN},
            json={"text": text, "submit": True},
            timeout=10.0,
        )
        if r.status_code < 300:
            return True, None
        return False, f"orchestrator /inject HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"orchestrator unreachable: {e}"


_last_transcribing_sweep = 0.0
_last_reclaim_sweep = 0.0
_last_standby_check = 0.0


def refresh_standby(db: Supabase) -> None:
    """Re-evaluate standby on an IDLE cycle, rarely.

    `_standby` only chooses this worker's poll cadence — the claim decision is made fresh in
    `claim_one` the moment something is actually pending, so a minute-stale flag here can cost nothing
    but a cycle at the wrong interval. Asking every cycle meant a `voice_requests` read (via
    `context_owner`) plus sometimes a `worker_presence` read, for ever, to learn nothing."""
    global _standby, _last_standby_check
    now_mono = time.monotonic()
    if now_mono - _last_standby_check < STANDBY_CHECK_S:
        return
    _last_standby_check = now_mono
    _standby = should_defer(db)


def sweep_stuck_transcribing(db: Supabase) -> None:
    """Close rows stuck in 'transcribing' — the capturer opened the lifecycle row at capture-accept and
    then died before STT finished. The transcription budget is bounded (~61s: one 30s Groq call + one
    retry), so >90s in this state can only mean a dead capturer; erroring the row is what releases the
    working indicators (ESP blink + floor lamp) that turned on at accept. Throttled to one sweep/30s."""
    global _last_transcribing_sweep
    now_mono = time.monotonic()
    if now_mono - _last_transcribing_sweep < 30:
        return
    _last_transcribing_sweep = now_mono
    try:
        rows = db.get("voice_requests",
                      "status=eq.transcribing&select=id,created_at&order=created_at.asc&limit=5")
        now = datetime.now(timezone.utc)
        for r in rows or []:
            ts = _parse_ts(r.get("created_at") or "")
            if ts and (now - ts).total_seconds() > 90:
                db.patch("voice_requests", f"id=eq.{r['id']}&status=eq.transcribing",
                         {"status": "error", "error": "transcription never completed (capturer died)",
                          "updated_at": now_iso()})
                log(f"closed stuck transcribing request {r['id']}")
    except Exception as e:  # noqa: BLE001
        log(f"transcribing sweep failed: {e}")


def reclaim_stale(db: Supabase) -> None:
    """Release requests no live process is working on, so a worker takes them.

    An ORPHAN is a claimed row whose process is gone: its owner's heartbeat is stale, or the owner is up
    under a different `claim_epoch` — a worker that died mid-run and came back under the same name, which
    a heartbeat alone can never show. One that had not acted (`acted_at` empty) goes back to pending with
    the owner in `failed_on`, and the realtime UPDATE sends the next worker to it. One that HAD acted may
    be half-done, so it is not replayed: it ends as an error and is investigated. So does an orphan older
    than the conversation window, which would be stale to run.

    A STRANDED row is pending with every healthy worker already in `failed_on` — the one it was handed
    to died before claiming it. Its `failed_on` is cleared so everyone may try again; each failure hands
    on until none is left and the last one ends it, so this cannot loop.

    The sweep is a backstop on its own clock (RECLAIM_SWEEP_S, and once at startup), run whether or not
    anything is pending."""
    global _last_reclaim_sweep
    now_mono = time.monotonic()
    if _last_reclaim_sweep and now_mono - _last_reclaim_sweep < RECLAIM_SWEEP_S:
        return
    _last_reclaim_sweep = now_mono
    now = datetime.now(timezone.utc)
    presence = {r.get("worker"): r for r in presence_rows(db)}
    for req in db.get("voice_requests", "status=eq.claimed&order=created_at.asc&limit=20"
                      "&select=id,transcript,claimed_by,claim_epoch,claimed_at,created_at,acted_at,failed_on") or []:
        owner = req.get("claimed_by") or ""
        if owner == WORKER_ID:
            alive = req.get("claim_epoch") == WORKER_EPOCH
        else:
            p = presence.get(owner) or {}
            ts = _parse_ts(p.get("last_seen", ""))
            fresh = bool(ts) and (now - ts).total_seconds() <= HEARTBEAT_FRESH_S
            epoch = (p.get("info") or {}).get("epoch")
            alive = fresh and (not req.get("claim_epoch") or not epoch or epoch == req.get("claim_epoch"))
            claimed_at = _parse_ts(req.get("claimed_at") or "")
            if not alive and fresh is False and claimed_at and (now - claimed_at).total_seconds() <= HEARTBEAT_FRESH_S:
                continue   # stale heartbeat but a claim newer than the window: give the owner its chance
        if alive:
            continue
        guard = f"id=eq.{req['id']}&status=eq.claimed&claimed_by=eq.{owner}"
        created = _parse_ts(req.get("created_at") or "")
        what = f"Request {req['id']} ({(req.get('transcript') or '')[:200]!r}) was orphaned by '{owner}'"
        if req.get("acted_at") or (created and (now - created).total_seconds() > session_reset_s()):
            why = ("it had already acted, so it may be half-done and was not replayed" if req.get("acted_at")
                   else "it is too old to run now")
            if db.patch("voice_requests", guard,
                        {"status": "error", "error": f"orphaned: '{owner}' died mid-run; {why}", "updated_at": now_iso()},
                        extra_headers={"Prefer": "return=representation"}):
                log(f"closed orphaned request {req['id']} from '{owner}': {why}")
                alert_failure("orphan", f"'{owner}' died mid-run", f"{what}; {why}. It ended as an error.")
            continue
        if db.patch("voice_requests", guard,
                    {"status": "pending", "claimed_by": None, "claimed_at": None, "claim_epoch": None,
                     "failed_on": sorted(set(req.get("failed_on") or []) | {owner}), "updated_at": now_iso()},
                    extra_headers={"Prefer": "return=representation"}):
            log(f"released orphaned request {req['id']} from '{owner}' back to pending")
            alert_failure("orphan", f"'{owner}' died mid-run",
                          f"{what} before it acted, and was put back to pending for another worker.")
    for req in db.get("voice_requests", "status=eq.untranscribed&select=id,created_at,transcript") or []:
        created = _parse_ts(req.get("created_at") or "")
        if created and (now - created).total_seconds() > UNTRANSCRIBED_MAX_S:
            if db.patch("voice_requests", f"id=eq.{req['id']}&status=eq.untranscribed",
                        {"status": "error", "error": "every transcriber failed and the Mac did not take it",
                         "updated_at": now_iso()}, extra_headers={"Prefer": "return=representation"}):
                log(f"closed untranscribed request {req['id']}: nobody transcribed it")
                alert_failure("transcription", "every transcriber failed",
                              f"Request {req['id']}: Groq, OpenAI and the Mac's local Whisper all failed to "
                              f"produce a transcript within {UNTRANSCRIBED_MAX_S:.0f}s; it ended as an error.")
    for req in db.get("voice_requests", "status=eq.pending&failed_on=neq.{}&select=id,failed_on,created_at") or []:
        created = _parse_ts(req.get("created_at") or "")
        if created and (now - created).total_seconds() > FAILOVER_S and \
                not untried_workers(db, set(req.get("failed_on") or [])):
            if db.patch("voice_requests", f"id=eq.{req['id']}&status=eq.pending",
                        {"failed_on": [], "updated_at": now_iso()}, extra_headers={"Prefer": "return=representation"}):
                log(f"request {req['id']} was stranded (every healthy worker in failed_on) — cleared for a retry")


def _mics_hearing(db: Supabase):
    """The most recent moment ANY mic reported hearing Jared, or None.

    `voice_presence.heard_jared_at` is already written by every capture source on each confirmed check
    — it drives the HUD and the ESP light. Nothing used it to decide WHEN TO ACT, which is what let a
    fragment run while another mic was still recording the same sentence.
    """
    stamps = [_parse_ts(r.get("heard_jared_at") or "")
              for r in db.get("voice_presence", "select=heard_jared_at")]
    stamps = [t for t in stamps if t]
    return max(stamps) if stamps else None


def _same_utterance(rows: list, first: dict) -> list:
    """The pending rows that are the SAME utterance as `first` — one spoken thing, several mics.

    Grown transitively over the union of recording windows, because that is the shape the failure had:
    the Mac cut one dictation into two fragments while the ESP held the whole 65 seconds, so the
    fragments only meet each other THROUGH the long window that contains them both.

    A row with no window (typed input) is nobody's duplicate and stands alone.
    """
    tol = timedelta(seconds=DEDUP_TOL_S)
    win = (_parse_ts(first.get("capture_start") or ""), _parse_ts(first.get("capture_end") or ""))
    if not (win[0] and win[1]):
        return [first]
    group, lo, hi = [first], win[0], win[1]
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r in group:
                continue
            s2, e2 = _parse_ts(r.get("capture_start") or ""), _parse_ts(r.get("capture_end") or "")
            if not (s2 and e2):
                continue
            if s2 <= hi + tol and lo <= e2 + tol:
                group.append(r)
                lo, hi = min(lo, s2), max(hi, e2)
                changed = True
    return group


def failover_wait(db: Supabase, waited_s: float, target: str) -> float:
    """Seconds still to leave a pending request to `target` before taking it; 0 means take it now.

    FAILOVER_S from its creation: a healthy target claims in about a second, so one that has not is not
    going to. Past that, the only reason to keep waiting is that `target` ITSELF is mid-run — a follow-up
    queues behind the run it follows — and only for BUSY_WAIT_S of that run, because a run that hangs
    would otherwise hold every command behind it for as long as it hangs. Another worker's run is no
    reason to wait at all."""
    if waited_s < FAILOVER_S:
        return FAILOVER_S - waited_s
    busy = db.get("voice_requests",
                  f"status=eq.claimed&claimed_by=eq.{target}&select=claimed_at&order=claimed_at.desc&limit=1")
    at = _parse_ts((busy or [{}])[0].get("claimed_at") or "") if busy else None
    if at:
        left = BUSY_WAIT_S - (datetime.now(timezone.utc) - at).total_seconds()
        if left > 0:
            return left
    log(f"pending {waited_s:.0f}s and {target} has not taken it — taking it")
    return 0.0


def claim_one(db: Supabase) -> dict | None:
    """Find one pending request and atomically claim it. Returns the row or None."""
    global _standby
    sweep_stuck_transcribing(db)

    # PENDING FIRST. Deferring is about who claims a request, so with nothing to claim the answer
    # changes nothing — and asking cost a voice_requests read on every idle cycle for ever. While idle
    # the standby flag is refreshed on its own slow clock, which is all its consumer (the poll cadence)
    # needs; the instant there IS something pending, it is decided fresh, before any claim.
    global _holding
    pending = db.get(
        "voice_requests",
        f"status=eq.pending&failed_on=not.cs.{{{WORKER_ID}}}&order=created_at.asc&limit=8"
        "&select=id,transcript,source,created_at,updated_at,capture_start,capture_end,failed_on",
    )
    reclaim_stale(db)   # orphans and stranded rows, on its own clock, whether or not anything is pending
    if not pending:
        _holding = False
        refresh_standby(db)
        return None

    # A host that cannot start a turn does not claim: it marks what is pending as tried here, one write
    # each, and that UPDATE is the realtime wake that sends the next worker to it. Claiming first and
    # handing off after cost 1.17s of reads on the Pi before the check was reached; the check is ~13µs.
    # A row no other healthy worker is left to try is claimed instead, and ends as an error the normal way.
    unready = claude_unready()
    if unready:
        passed = [r for r in pending
                  if untried_workers(db, set(r.get("failed_on") or []) | {WORKER_ID})]
        for r in passed:
            db.patch("voice_requests", f"id=eq.{r['id']}&status=eq.pending",
                     {"failed_on": sorted(set(r.get("failed_on") or []) | {WORKER_ID}), "updated_at": now_iso()})
        if passed:
            log(f"passed {len(passed)} pending request(s) on unclaimed: {unready}")
            alert_failure("request", unready, f"This host cannot start a turn, so it passed {len(passed)} "
                          f"pending request(s) to the other workers unclaimed: {[r['id'] for r in passed]}.")
        pending = [r for r in pending if r not in passed]
        if not pending:
            return None

    global _failover_at
    oldest_at = _parse_ts(pending[0].get("created_at") or "")
    # A row handed on or released waits from when it became claimable again, not from its creation —
    # otherwise its preferred untried worker gets no head start and the lowest-ranked one races it.
    back_at = _parse_ts(pending[0].get("updated_at") or "") if pending[0].get("failed_on") else None
    since = max(t for t in (oldest_at, back_at) if t) if (oldest_at or back_at) else None
    waited_s = (datetime.now(timezone.utc) - since).total_seconds() if since else 0.0
    tried = set(pending[0].get("failed_on") or [])
    if degraded() and other_worker_healthy(db, tried):
        # My own link is failing: a claim here could take minutes to answer while a healthy worker sits idle.
        _standby, _failover_at = True, 0.0
        return None
    target = defer_target(db, tried)
    left = failover_wait(db, waited_s, target) if target else 0.0
    if left > 0:
        _standby = True   # another worker should take it → stand down until its chance runs out
        _failover_at = time.monotonic() + left + 0.5   # a clock arrival, not a poll
        return None
    _standby, _failover_at = False, 0.0   # I'm eligible to claim → run the active cadence

    # HOLD WHILE HE IS STILL TALKING. One utterance reaches the queue as several rows — mics endpoint
    # at different pauses — and acting on the first one to arrive is how half a sentence becomes a
    # command. Measured case: the Mac cut a 65-second dictation into two fragments and enqueued them
    # while the ESP was still recording; the fragments ran, and the ESP's complete 888-character
    # capture (the actual instruction) was then discarded as a duplicate of them.
    #
    # Waiting for every mic to go quiet costs nothing on a short command: by the time "turn the light
    # blue" is enqueued, no mic has heard anything for seconds, so it claims immediately.
    heard = _mics_hearing(db)
    held_s = (datetime.now(timezone.utc) - oldest_at).total_seconds() if oldest_at else 0.0
    if heard and (datetime.now(timezone.utc) - heard).total_seconds() < MIC_QUIET_S and held_s < MAX_HOLD_S:
        if not _holding:
            log(f"holding {len(pending)} pending request(s) — a mic is still hearing him")
        _holding = True
        return None
    _holding = False

    # THE FULLEST CAPTURE WINS, not the first to arrive. Two mics produce two accounts of one
    # utterance; the longer one heard more of it. Deliberately never a merge of both: stitching two
    # transcripts risks saying a sentence twice inside a command, which is worse than losing a clause.
    #
    # (A real merge is the interesting version — the Mac hears the first half, the ESP the second, and
    # walking between rooms mid-sentence still yields one command. That needs the halves aligned by
    # their recording windows rather than concatenated, and is deliberately not attempted here.)
    group = _same_utterance(pending, pending[0])
    req = max(group, key=lambda r: len(r.get("transcript") or ""))
    if len(group) > 1:
        log(f"one utterance, {len(group)} captures — taking {req.get('source')}'s "
            f"({len(req.get('transcript') or '')} chars) over "
            f"{[(r.get('source'), len(r.get('transcript') or '')) for r in group if r is not req]}")
    rid = req["id"]

    claimed = db.patch(
        "voice_requests",
        f"id=eq.{rid}&status=eq.pending",
        {
            "status": "claimed",
            "claimed_by": WORKER_ID,
            "claim_epoch": WORKER_EPOCH,
            "claimed_at": now_iso(),
            "assigned_to": WORKER_ID,
            "updated_at": now_iso(),
        },
        extra_headers={"Prefer": "return=representation"},
    )
    # Empty representation array => someone else won the race.
    if not claimed:
        return None
    # The other captures of the same utterance are retired HERE, before this one runs — not left
    # pending for the next iteration to pick up as a fresh command. Marked, never deleted: a bad
    # grouping should be visible in the queue rather than a command that silently vanished.
    for other in group:
        if other is req:
            continue
        db.patch("voice_requests", f"id=eq.{other['id']}&status=eq.pending",
                 {"status": "done", "result": SUPERSEDED_RESULT, "updated_at": now_iso()})
    return claimed[0]


# Results that mean the request executed NOTHING. A row like this must never be the surviving half of a
# two-mic pair: if one mic endpoints early it enqueues a mid-sentence fragment, the fragment is discarded
# as a false positive, and the OTHER mic's complete capture then arrives overlapping it — dedupe on
# arrival order alone drops the real command and the whole utterance disappears with nothing run.
# Skipping these is safe precisely because they did no work, so there is nothing to run twice.
SUPERSEDED_RESULT = "(superseded — a fuller capture of the same words ran)"
NO_WORK_RESULTS = ("(duplicate — dropped)", "(ignored: wake-word false positive)", SUPERSEDED_RESULT)


def _duplicate_of_recent(db: Supabase, req: dict) -> bool:
    """True if an earlier already-claimed/done request that ACTUALLY RAN covers the SAME RECORDING WINDOW
    as this one — i.e. a second always-on mic (ESP + Mac) heard the same utterance. Compares the actual
    [capture_start, capture_end] spans (overlap, each padded by DEDUP_TOL_S for clock skew + ESP stream
    latency), NOT enqueue time — so the pair always collapses regardless of transcription speed, while two
    genuinely distinct (sequential, non-overlapping) commands BOTH run even a second apart. Falls back to
    created_at proximity only for a pair where either side predates capture-window stamping."""
    rid = req.get("id")
    s, e = _parse_ts(req.get("capture_start") or ""), _parse_ts(req.get("capture_end") or "")
    ts = _parse_ts(req.get("created_at") or "")
    rows = db.get("voice_requests",
                  "select=id,created_at,status,result,capture_start,capture_end&order=created_at.desc&limit=12")
    tol = timedelta(seconds=DEDUP_TOL_S)
    for r in rows:
        if r.get("id") == rid or r.get("status") not in ("claimed", "done"):
            continue
        if (r.get("result") or "").strip() in NO_WORK_RESULTS:
            continue        # it ran nothing — it cannot stand in for this capture
        os_, oe = _parse_ts(r.get("capture_start") or ""), _parse_ts(r.get("capture_end") or "")
        if s and e and os_ and oe:
            if s <= oe + tol and os_ <= e + tol:   # padded window overlap → same utterance
                return True
        else:   # fallback: enqueue-time proximity for rows without a recording window
            ots = _parse_ts(r.get("created_at") or "")
            if ts and ots and 0 <= (ts - ots).total_seconds() <= DEDUP_WINDOW_S:
                return True
    return False


def process_request(db: Supabase, req: dict) -> None:
    rid = req["id"]
    transcript = req.get("transcript") or ""
    log(f"claimed request {rid}: {transcript[:80]!r}")

    # Timestamp carry-through: the data MCP defaults every write's timestamp to when the audio recording
    # STARTED (not ~30s later at pipeline end), so a voice-logged set lands at Jared's rest-clock moment.
    # Only for audio captures (typed/web commands have no capture_start → real wall clock).
    set_cmd_ts((req.get("capture_start") or "").strip() or None)

    # Manual context reset from the hub UI: drop the CURRENT mode's session so the next command starts
    # fresh — full-agent mode resets its own context, hub mode its own (whichever you're in). The status-hub
    # already cleared the visible thread and pushed a live reset to every surface — here we just forget the
    # Claude session and ack (no Claude run, no turns).
    if transcript.strip() == RESET_SENTINEL:
        reset_agent = agent_settings()["mode"] and WORKER_ID != "fly"
        try:
            session_file(reset_agent).unlink(missing_ok=True)
        except Exception as e:
            log(f"reset: unlink session failed: {e}")
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(context reset)", "updated_at": now_iso()})
        log(f"{'agent' if reset_agent else 'hub'} context reset — session dropped")
        return

    # A command that is only "thank you" is never run: from a microphone it is what Whisper writes for a
    # clip with no speech in it, and typed it asks for nothing. The transcribers already drop it (server.py,
    # mac-listener, status-hub's transcribe.js); this catches every other way a row gets here.
    if _NOTHING_SAID.match(transcript):
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "error", "error": "nothing said", "updated_at": now_iso()})
        log(f"ignored {transcript!r}: nothing said")
        return

    focus = get_focus()

    # Focus is a specific Mac session → inject the transcript into that session's
    # wall terminal (the orchestrator owns the live `claude --resume` pty) instead
    # of running a fresh hub `claude -p`. The conversation shows in the xterm panel.
    if focus != "hub":
        log(f"focus={focus!r}: injecting into wall terminal via orchestrator")
        ok, err = inject_to_terminal(transcript)
        if not ok and hand_off(db, req, err or "inject failed"):
            return   # only the Mac runs the orchestrator; another worker may reach it
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            ({"status": "done", "result": "(injected to terminal)", "updated_at": now_iso()}
             if ok else
             {"status": "error", "error": err or "inject failed", "updated_at": now_iso()}),
        )
        return

    # Dedup: multiple always-on listeners (ESP + Mac) hear one utterance, so it lands as several requests
    # a few seconds apart. If an overlapping capture was already handled, drop this one so the command
    # never runs twice. Only an always-on capture can be double-heard: a press, a watch command or a
    # typed message comes from one device on purpose, so it skips the check and the read it costs.
    if req.get("always_on") and _duplicate_of_recent(db, req):
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(duplicate — dropped)", "updated_at": now_iso()})
        log(f"dropped duplicate request {rid}: {transcript[:60]!r}")
        return

    # (The user turn is written AFTER the model runs — deferred so a wake-word false positive that the
    # model drops never appears in the conversation. See below.)

    # Context. Full-agent (coding) mode keeps a real CLI --resume session — its context (open files, long
    # tool chains) can't be reconstructed from replies. The HUB conversation does NOT use --resume: each
    # turn is a FRESH process handed the recent transcript (recent_context), so the two models never share a
    # session and can't cross-contaminate. Only agent mode reads/writes a persisted session id.
    ag = agent_settings()
    session_agent = ag["mode"] and WORKER_ID != "fly"
    ctx = ""
    if session_agent:
        state = read_session(True)
        if should_reset_session(state, agent=True):
            resume_id = None
            log("agent session: starting FRESH (reset rule fired)")
        else:
            resume_id = state["session_id"]
            log(f"agent session: resuming {resume_id}")
    else:
        resume_id = None
        ctx = recent_context()
        log(f"hub: fresh run + {len(ctx)} chars injected conversation" if ctx else "hub: fresh run (no prior context)")

    # Surface tool calls live by pushing them straight to the hub's activity bar (transient — not
    # stored in voice_messages, so they never appear in the conversation thread). The first one that
    # CHANGES something marks the request acted (`req["_acted"]`, and `acted_at` on the row): until then
    # a failed or orphaned turn can run again elsewhere without repeating anything.

    def _emit(kind, payload):
        if kind == "text":
            # What the model says between tool calls. The runners report only text a tool call
            # followed, so the final reply never arrives here; that is the thread message.
            said = str(payload.get("text") or "").strip()
            if said:
                push_activity(said[:2000], "text")
            return
        if kind != "tool":
            return
        name = str(payload.get("name") or "")
        short = name.rsplit("__", 1)[-1] if "__" in name else name
        if not req.get("_acted") and not replay_safe(short):
            req["_acted"] = True
            threading.Thread(target=db.patch, daemon=True,
                             args=("voice_requests", f"id=eq.{rid}", {"acted_at": now_iso()})).start()
        try:
            args = payload.get("input")
            argstr = json.dumps(args, ensure_ascii=False, separators=(",", ":")) if args else ""
        except Exception:  # noqa: BLE001
            argstr = str(payload.get("input") or "")
        if len(argstr) > 240:
            argstr = argstr[:239] + "…"
        text = (short + (" " + argstr if argstr else "")).strip()
        push_activity(text[:260], "tool")  # direct push → instant activity bar; full width like the rest

    # Prompt directives ride only on the prompt handed to Claude, never shown in the thread. None of them
    # shapes how the reply is written: every surface renders the same markdown reply.
    source = (req.get("source") or "").strip()
    # AMBIENT is a different property from transcribed, and conflating them silently ate real messages.
    # The always-on mics hear the whole room, so most of what they send was not addressed to the
    # assistant and a turn may legitimately be dropped as noise. A press is meant, however
    # conversational it sounds. Judging one "not for you" threw away "So I've never done better than 3 by 10."
    # The capture stamps it (`always_on`), not the source label: one device can be a listener and a
    # button, and a list of labels here drifted from the hub's — the phone's listener was in neither.
    ambient = bool(req.get("always_on"))
    prompt = transcript
    # Give the model a clock so any relative date/time resolves correctly — without it the model defaults
    # to its training era (e.g. logging "yesterday" against 2024). Pure wall-clock, no timezone concept.
    # Priority, BEST ANCHOR FIRST — what we want is the moment Jared SPOKE, not the moment we got here:
    #  1. capture_start — when the mic started recording, i.e. when he started talking. That's what
    #     relative arithmetic has to hang off: "a timer for 11 minutes" means 11 minutes from when he
    #     said it. Stamped UTC by the capture side, so convert into this machine's local wall clock to
    #     read like the other two branches.
    #  2. client_now — the REQUESTING DEVICE's own clock (phone/browser), for typed commands that carry
    #     no recording window; better than ours because the worker is only "wherever Jared is" when the
    #     request came from home.
    #  3. this worker's clock — last resort (and what typed/local commands legitimately land on). For a
    #     voice command it's the wrong anchor: it reads at claim/transcription time, ~25s after he
    #     started speaking on a HEALTHY run and much later when the Mac worker is down and the job falls
    #     through to the Pi or Fly, so every "in N minutes" would quietly lose that lag.
    # Rides on the prompt, NOT the cached system block, so it can't change the per-command system prefix
    # and break the prompt cache.
    _cap = _parse_ts((req.get("capture_start") or "").strip())
    client_now = (req.get("client_now") or "").strip()
    _m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$", client_now)
    if _cap:
        _now = _cap.astimezone()   # UTC capture stamp → this machine's local wall clock
        _now_str = _now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
    elif _m:
        _now = datetime(*(int(x) for x in _m.groups()))
        _now_str = _now.strftime("%A, %B %-d, %Y, %-I:%M %p")
    else:
        _now = datetime.now().astimezone()
        _now_str = _now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z")
    prompt += ("\n\n[This command was said/typed at " + _now_str + " ("
               + _now.strftime("%Y-%m-%d") + ") — treat THAT as 'now'. Anchor every relative date/time to "
               "it: both relative dates — 'today', 'yesterday', 'last night', 'this morning', 'last week' "
               "— and relative arithmetic — 'in 11 minutes', 'an hour from now'. Never your training data, "
               "and never your own guess at the current instant: the pipeline took some seconds to reach "
               "you and that lag is not part of what he asked for. When logging something for 'now' (no "
               "different time named), pass this exact moment as occurred_at (YYYY-MM-DDTHH:MM:SS) to the "
               "logging tool explicitly rather than omitting it — the tool's own clock may not be wherever "
               "this was actually said/typed.]")
    # Where he was, when the device sent it (voice_requests.location). One line on the prompt, and the
    # same line for the data MCP to pass to any session this turn starts.
    where = describe_location(req.get("location"))
    set_cmd_loc(where)
    if where:
        prompt += ("\n\n[Where he was when he said this: " + where + ". Use it only when the request "
                   "depends on where he is (near me, directions, local hours, a pickup point), and "
                   "never mention it otherwise.]")
    if ambient:
        prompt += ADDRESSING_DIRECTIVE + ROOM_SEND_DIRECTIVE
    else:
        prompt += DELIBERATE_DIRECTIVE

    # ---- ONE dynamic route. Every hub command goes to Sonnet (safe tools + escalate); a request that needs
    # a gated WRITE tool escalates to a FRESH Opus (full tools). Photos skip straight to Opus (vision via the
    # CLI Read tool; Sonnet would just escalate anyway). Full-agent mode is the unrestricted coding agent on
    # its own --resume session. No user-facing model/route choice anymore — the model is chosen per turn.
    attachments = req.get("attachments") or []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except Exception:  # noqa: BLE001
            attachments = []
    use_agent = session_agent   # full-agent (coding) mode → the unrestricted agent, on its own session
    log(f"route={'agent' if use_agent else ('photo→opus' if attachments else 'dynamic sonnet→opus')} src={source}")
    ping_esp_led("blink")   # working — the ESP light blinks for EVERY command, not just ESP-sourced ones
    push_activity(transcript[:240], "working")   # bottom-strip: the prompt being worked

    # Role framing. Full-agent mode swaps the locked-down hub framing for the unrestricted all-purpose
    # framing. The lights are not listed here: a turn that needs them calls the govee `list_lights` tool.
    sys_ctx = (AGENT_SYSTEM if use_agent else HUB_SYSTEM) + "\n\n" + SHARED_STATE_NOTE
    # Photo turns: pull the attachment objects local so Read can view them, and put the paths + the ONE-item
    # framing on the prompt. Photos go straight to Opus in the dispatch below.
    att_tmp, att_paths = None, []
    if attachments:
        att_tmp, att_paths = fetch_attachments(attachments)
        if att_paths:
            prompt += ATTACH_DIRECTIVE.format(n=len(att_paths), paths="\n".join(att_paths))
            log(f"attachments: {len(att_paths)} photo(s) staged in {att_tmp}")
        else:
            prompt += ("\n\n[The user attached photo(s), but they could not be retrieved — tell them "
                       "that briefly instead of guessing at the contents.]")

    # Dispatch — one dynamic route, the same on every worker (Pi, Mac, and Fly, which holds its own
    # subscription token). Everything runs on the Claude SUBSCRIPTION; there is no paid path. A run that
    # fails transiently is retried (`run_subscription`), and one that still fails surfaces as an error.
    # `ctx` (the replayed hub conversation) rides on the FRESH run; there is no --resume for hub turns, so
    # the two models never share a session. agent → the unrestricted coding agent (its own --resume
    # session). Photos → Opus (vision via Read). Otherwise: Sonnet (warm SDK client) with the safe toolset;
    # a gated need → a FRESH Opus. Errors never upgrade the model. A failure that called no tool is
    # handed to another worker below rather than ended here.
    hub_prompt = ctx + prompt   # ctx is "" in agent mode (uses --resume) or when there's no live conversation
    room_deny = SEND_TOOLS if ambient else None   # the room mics never send a text or an email
    unready = claude_unready()
    if unready:
        result = {"session_id": None, "text": "", "result": "", "ok": False, "raw_error": unready}
    elif use_agent:
        result = run_claude(prompt, resume_id, on_event=_emit, system_append=sys_ctx, model="opus",
                            agent=True, cwd=ag["cwd"], allow_read=bool(att_paths),
                            deny_tools=room_deny)
    elif attachments:
        # Photos → straight to Opus (vision needs the capable model + Read; Sonnet would only escalate).
        result = run_subscription(
            lambda: run_claude(hub_prompt + POWER_DIRECTIVE, None, on_event=_emit, system_append=sys_ctx,
                               model="opus", allow_read=bool(att_paths), allowed_tools=turn_tools(ambient),
                               deny_tools=room_deny),
            "subscription Opus (photo)")
    else:
        # FAST PATH — Sonnet on the WARM SDK client, safe toolset only. Not a per-command `claude -p`: that
        # spawn (CLI + four stdio MCP servers) measured 3.6s on the Pi (~42% of a simple turn) before the
        # model saw a token; the warm client pays it once.
        # Errors never upgrade the model: on 2026-08-30 an error-escalation turned every noise judgment
        # into a fresh Opus spawn — 291 in one evening.
        fast_prompt = hub_prompt + ESCALATE_DIRECTIVE
        result = run_subscription(lambda: SDK.run(fast_prompt, on_event=_emit, ambient=ambient),
                                  "subscription fast path")
        st = (result.get("text") or "").strip().lower()
        escalate = ("__escalate__" in st) or (re.sub(r"[^a-z]", "", st) == "escalate")
        # Escalate ONLY on the sentinel — a gated need. A failed fast path surfaces as an error below; it
        # does not buy an Opus run.
        if escalate:
            log("dynamic → Opus (full toolset): __ESCALATE__")
            # Surface the hop itself: the bottom activity bar shows "escalated to Opus" and keeps an OPUS
            # chip up for the rest of the run, so a turn that quietly changed models is visible on the wall
            # instead of looking like an unexplained pause between the prompt and the first Opus tool call.
            push_activity("Escalated to Opus", "escalate")
            # These turns are rare and already slow, so the CLI cold start is noise here.
            result = run_subscription(
                lambda: run_claude(hub_prompt + POWER_DIRECTIVE, None, on_event=_emit, system_append=sys_ctx,
                                   model="opus", allow_read=bool(att_paths), allowed_tools=turn_tools(ambient),
                                   deny_tools=room_deny),
                "subscription Opus")

    if att_tmp:
        shutil.rmtree(att_tmp, ignore_errors=True)   # staged photos served their turn

    if result.get("session_id") and session_agent:   # only full-agent mode keeps a CLI session; hub replays ctx
        write_session(True, result["session_id"])
        log(f"captured agent session_id {result['session_id']}")

    text = (result.get("text") or "").strip()

    # Safety net: __ESCALATE__ is an INTERNAL signal and must NEVER reach the user. Fresh hub runs + a clean
    # replayed transcript make it nearly impossible now (the two models no longer share a session), but as a
    # backstop, re-run on a fresh Opus with NO context at all — nothing to mimic.
    if ("__escalate__" in text.lower()) or (re.sub(r"[^a-z]", "", text.lower()) == "escalate"):
        log("result leaked __ESCALATE__ → clean Opus re-run (no context)")
        push_activity("Escalated to Opus", "escalate")
        result = run_claude(prompt, None, on_event=_emit, system_append=sys_ctx,
                            model="opus", allow_read=bool(att_paths), allowed_tools=turn_tools(ambient), deny_tools=room_deny)
        text = (result.get("text") or "").strip()

    # The same net for __IGNORE__ on a turn he MEANT. Only room-mic turns are taught the sentinel, but a
    # model can still pick it up (it did, from replayed history) — and on a deliberate turn there is no
    # ignore branch to catch it, so it was saved and pushed to him as the reply. Re-run clean, with no
    # context to mimic; the prompt already carries DELIBERATE_DIRECTIVE.
    if not ambient and is_ignore(text) and not result.get("used_tools"):
        log("deliberate turn answered __IGNORE__ → clean Opus re-run (no context)")
        result = run_claude(prompt, None, on_event=_emit, system_append=sys_ctx,
                            model="opus", allow_read=bool(att_paths), allowed_tools=turn_tools(ambient), deny_tools=room_deny)
        text = (result.get("text") or "").strip()

    # A failure here is this machine's, not the command's, until every machine has tried it: hand it on.
    # Nothing has been written to the thread yet, so the worker that takes it writes the whole turn.
    if not result.get("ok") and not req.get("_acted") and hand_off(db, req, result.get("raw_error") or "unknown error"):
        return

    # Tag every row with the mode so the hub UI can show this mode's thread separately and switch live.
    # Defined up here because the ignore branch below writes a row too.
    msg_mode = "agent" if session_agent else "hub"

    # Wake-word false positive: the model judged the transcript noise or a mishear. No ASSISTANT turn is
    # written — nothing was done and nothing is spoken — but the words are KEPT, flagged `ignored`.
    #
    # They used to be erased entirely, and that made a wrong judgement unrecoverable: Jared could not
    # say "do the one you ignored", and a following "answer my question" pointed at something that, as
    # far as the model could see, had never been said. Which is precisely what happened on 2026-08-28.
    if ambient and is_ignore(text):
        push_activity("(ignored — likely noise)", "done")
        db.post("voice_messages",
                {"request_id": rid, "focus": "hub", "mode": msg_mode, "role": "user",
                 "text": transcript, "done": True, "ignored": True, "worker": WORKER_ID})
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(ignored: wake-word false positive)", "updated_at": now_iso()})
        log(f"ignored likely false-positive (kept in thread): {transcript[:60]!r}")
        maybe_auto_disable_wakefree()
        return

    # Real turn → now write the user bubble (deferred past the noise check), then close the activity bar.
    db.post("voice_messages",
            {"request_id": rid, "focus": "hub", "mode": msg_mode, "role": "user", "text": transcript,
             "done": True, **({"attachments": attachments} if attachments else {})})
    push_activity(text[:240], "done")  # close out the activity bar w/ the result (room for the full line)

    if result["ok"]:
        db.post(
            "voice_messages",
            {"request_id": rid, "focus": "hub", "mode": msg_mode, "role": "assistant",
             "text": text, "done": True, "worker": WORKER_ID},
        )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "done", "result": text, "updated_at": now_iso()},
        )
        # Pushed to the phone (and from there the watch) when nothing else will tell him: a room mic has
        # no screen, and the watch app is suspended the moment the wrist drops, so a command sent from it
        # would otherwise finish with no word to him until he looks again.
        if source in ("fly", "mac", "watch-cmd") and text:
            notify_phone(text)
        log(f"done request {rid}: {text[:120]!r}")
    else:
        err = result.get("raw_error") or "unknown error"
        # Still surface whatever assistant text we did get, if any.
        if result.get("text"):
            db.post(
                "voice_messages",
                {"request_id": rid, "focus": "hub", "mode": msg_mode, "role": "assistant",
                 "text": result["text"], "done": True, "worker": WORKER_ID},
            )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "error", "error": err, "updated_at": now_iso()},
        )
        log(f"ERROR request {rid}: {err}")
        alert_failure("request", err, f"Request {rid} ({transcript[:200]!r}) ENDED AS AN ERROR here: "
                      + ("it had already acted, so it was not handed on." if req.get("_acted") else
                         f"no healthy worker was left untried (failed_on {req.get('failed_on') or []})."))


# --------------------------------------------------------------------------- #
# Realtime waker — event-driven claim. A background asyncio loop holds a Supabase
# realtime subscription to voice_requests INSERTs and sets a threading.Event the
# instant a command is enqueued, so the main loop claims it immediately instead of
# waiting out the idle poll (removing up to IDLE_POLL_S of latency on every command).
# The poll stays as a fallback: when the websocket is healthy the event fires first
# and we never hit the timeout; if it drops, wait() just times out and we degrade to
# exactly the old polling cadence. Subscribing requires voice_requests to be in the
# supabase_realtime publication (migration 00021).
# --------------------------------------------------------------------------- #

REALTIME_ENABLED = os.environ.get("REALTIME", "1").strip().lower() not in ("0", "false", "no", "off")


class RealtimeWaker:
    def __init__(self):
        self.event = threading.Event()
        self.healthy = False   # True only while the voice_requests subscription is live — drives the poll cadence
        self._loop = None
        self._sock = None
        self._started = False

    def start(self) -> None:
        if self._started or not REALTIME_ENABLED:
            if not REALTIME_ENABLED:
                log("realtime listener disabled by env (REALTIME=0); polling only")
            return
        try:
            import realtime  # noqa: F401 — probe availability before spawning the thread
        except Exception as e:  # noqa: BLE001 — missing lib must not break the worker; just poll
            log(f"realtime listener unavailable ({e}); falling back to {IDLE_POLL_DEGRADED_S}s polling")
            return
        self._started = True
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, name="rt-waker", daemon=True).start()
        log("realtime listener starting (event-driven claim on voice_requests → pending)")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            log(f"realtime listener loop exited: {e}")

    async def _serve(self) -> None:
        from realtime import AsyncRealtimeClient
        # Outer loop rebuilds the client if connect()/subscribe() itself fails (e.g. no network at
        # boot); auto_reconnect handles transient drops once connected. Never raises out — the worker
        # keeps polling regardless.
        while _running:
            try:
                self._sock = AsyncRealtimeClient(f"{SUPABASE_URL}/realtime/v1", SUPABASE_KEY,
                                                 auto_reconnect=True)
                await self._sock.connect()
                ch = self._sock.channel("voice-worker")
                # "*" (not INSERT) — and ONE binding, not two: realtime-py is unreliable with two
                # bindings on the same table (same note in mac-listener/listener.py).
                ch.on_postgres_changes("*", callback=self._on_change,
                                       schema="public", table="voice_requests")
                for t in ("app_settings", "voice_focus", "voice_messages"):   # the live state's feeds
                    ch.on_postgres_changes("*", callback=self._on_live, schema="public", table=t)
                await ch.subscribe(self._on_sub)
                # Recycle the socket every ~5 min even while it still reports is_connected: a HALF-OPEN
                # subscription stays "connected" but silently stops delivering INSERTs, stranding commands on
                # the slow backstop poll (the ~60s pickup lag). Tear down + rebuild on a fixed cadence so
                # events keep flowing — the same guard the Fly server, mac-listener, and litter watcher use.
                started = time.monotonic()
                while _running and self._sock.is_connected and (time.monotonic() - started) < 300.0:
                    await asyncio.sleep(1.0)
                if _running and self._sock.is_connected:
                    log("realtime waker recycling socket (proactive half-open guard)")
            except Exception as e:  # noqa: BLE001
                log(f"realtime listener error: {e}; retrying in 5s (poll fallback active)")
            self.healthy = False   # disconnected (or never subscribed) → main loop tightens its poll
            try:
                if self._sock:
                    await self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            if _running:
                await asyncio.sleep(5.0)

    def _on_change(self, payload) -> None:
        """Wake on any voice_requests change that leaves a row CLAIMABLE (status 'pending').

        Binding INSERT alone missed every real command. BOTH capture paths (ESP `server.py` and Mac
        `wakefree.py`) insert a placeholder row as 'transcribing' the instant the capture closes, then
        UPDATE it to 'pending' when the transcript lands. So the INSERT woke the worker with nothing
        yet to claim, and the UPDATE that actually made it claimable fired no event it listened for —
        every command then waited out the full IDLE_POLL_S backstop. Measured: a consistent 14-16s of
        dead air between "transcribed" and "claimed" on three consecutive commands.

        Filtered to 'pending' so the worker's own claimed/done writes can't wake it in a loop.
        """
        try:
            rec = (payload.get("data") or {}).get("record") or {}
        except AttributeError:
            return
        status = str(rec.get("status") or "")
        if status == "pending":
            self.event.set()
        elif status == "untranscribed" and rec.get("audio") and local_whisper_ready():
            threading.Thread(target=transcribe_local, args=(rec,), daemon=True).start()

    def _on_live(self, payload) -> None:
        try:
            d = payload.get("data") or {}
            live_event(str(d.get("table") or ""), str(d.get("type") or "").split(".")[-1].upper(),
                       d.get("record") or {}, d.get("old_record") or {})
        except Exception as e:  # noqa: BLE001 — a bad frame must not take the listener down
            log(f"live event failed: {e}")

    def _on_sub(self, state, err=None) -> None:
        self.healthy = "SUBSCRIBED" in str(state).upper()   # any other state (TIMED_OUT/CLOSED/ERROR) → degraded poll
        if self.healthy:   # a fresh subscription missed whatever happened while it was down: reload
            threading.Thread(target=live_load, name="live-load", daemon=True).start()
            threading.Thread(target=sweep_untranscribed, name="untranscribed-sweep", daemon=True).start()
        log(f"realtime subscription: {state}{(' err=' + str(err)) if err else ''}")

    def wait(self, timeout: float) -> bool:
        """Block until a row goes 'pending' (instant) or `timeout` elapses (poll fallback)."""
        fired = self.event.wait(timeout)
        self.event.clear()
        return fired


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

_running = True


_waker = None   # the RealtimeWaker, so a signal can cut the main loop's wait short


def _handle_signal(signum, frame):  # noqa: ARG001
    """Stop after the current iteration. Says so in the heartbeat at once, so no worker keeps deferring
    to one on its way out, and wakes the main loop so an idle worker exits in a moment instead of
    sleeping out its poll past the unit's stop timeout (which then SIGKILLs it)."""
    global _running, _stopping
    _running, _stopping = False, True
    log(f"received signal {signum}; shutting down after current iteration")

    def _announce():
        try:
            with httpx.Client(timeout=5) as c:
                heartbeat(Supabase(c))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_announce, daemon=True).start()
    if _waker is not None:
        _waker.event.set()


def one_iteration(db: Supabase) -> bool:
    """Run a single claim+process cycle. Returns True if a request was processed."""
    req = claim_one(db)
    if not req:
        return False
    try:
        process_request(db, req)
    except Exception as e:  # noqa: BLE001 — a bug in processing must not kill the loop
        log(f"process_request crashed: {e}")
        try:
            if not req.get("_acted") and hand_off(db, req, f"process_request crashed: {e}"):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            db.patch(
                "voice_requests",
                f"id=eq.{req['id']}",
                {"status": "error", "error": str(e), "updated_at": now_iso()},
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        # /led-off must fire for EVERY command — the blink (ping_esp_led "blink") is universal, so the
        # off has to be too. Gating this to source=='fly' meant a Mac/web command blinked the ESP and
        # NEVER cleared it (e.g. a wake-word false positive: claimed → blink → dropped as noise →
        # stuck blinking forever). The ESP just ignores the ping when there's no light to clear.
        ping_esp_led("off")   # model done (or dropped/errored) → clear the ESP working-light
    return True


def _heartbeat_loop(stop_evt: threading.Event) -> None:
    """Heartbeat on its OWN thread + client so the worker stays 'present' even while a long command blocks
    the main loop. Otherwise its presence goes stale mid-task and a lower-priority worker poaches the
    user's follow-up — running it against a different machine/session and losing the conversation context.
    A rapid follow-up is the user tagging an extra thought onto the same thread, so it MUST queue on the
    same worker and resume the same session. Fires immediately, then every HEARTBEAT_INTERVAL_S (or the
    slower STANDBY_HEARTBEAT_S while this worker is standing by)."""
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as hb_client:
        hb_db = Supabase(hb_client)
        while not stop_evt.is_set():
            try:
                heartbeat(hb_db)
                maybe_auto_reenable_wakefree()   # self-throttled to ~5 min; rides the beat, no extra timer
            except Exception as e:  # noqa: BLE001
                log(f"heartbeat thread error: {e}")
            # Standing by → beat slowly (but still < HEARTBEAT_FRESH_S so we never look dead).
            stop_evt.wait(STANDBY_HEARTBEAT_S if _standby else HEARTBEAT_INTERVAL_S)


LISTENER_LABEL = "com.jaredgantt.voice-mac-listener"


def _listener_supervisor(stop_evt: threading.Event) -> None:
    """Mac only: revive the wake-word listener whenever its launchd job is dead. launchd's KeepAlive is
    NOT sufficient on this macOS — it parks the respawn of an exited agent ("pended nondemand spawn =
    inefficient"), which left the Mac mic silently dead for 24h after the Jul 3 CoreAudio-wedge exit.
    A demand spawn (launchctl kickstart) always works, so the always-on worker checks the job every 60s
    and kicks it if it has no PID. The listener's own wedge path now re-execs in place (no exit at all);
    this covers everything else: hard crashes, jetsam kills, and login-time RunAtLoad spawns that launchd
    also pends."""
    while not stop_evt.is_set():
        try:
            out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[2] == LISTENER_LABEL and parts[0] == "-":
                    log(f"listener job dead (last status {parts[1]}) — kickstarting")
                    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LISTENER_LABEL}"],
                                   capture_output=True, timeout=10)
        except Exception as e:  # noqa: BLE001
            log(f"listener supervisor error: {e}")
        stop_evt.wait(60)


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY is required (service_role key). See README.",
              file=sys.stderr)
        return 2

    once = "--once" in sys.argv

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"starting (priority={WORKER_PRIORITY}, state_dir={STATE_DIR}, once={once})")
    validate_safe_tools(DATA_MCP_JS)

    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        db = Supabase(client)
        publish_repo_dirs()   # Mac-only: refresh the agent cwd picker's repo list

        if once:
            heartbeat(db)
            processed = one_iteration(db)
            log(f"--once complete (processed={processed})")
            return 0

        global _waker
        waker = _waker = RealtimeWaker()
        waker.start()   # event-driven claim; no-op fallback to polling if realtime is unavailable
        threading.Thread(target=SDK.warm, name="sdk-warm", daemon=True).start()   # first turn starts warm

        # Heartbeat on its own thread so a long command (a multi-minute agent run) never lets this
        # worker's presence go stale — which would let a lower-priority worker poach the user's follow-up
        # onto a different session. The main loop is then free to block on process_request as long as a
        # command takes, and the queued follow-up runs HERE, next, resuming the same conversation.
        hb_stop = threading.Event()
        threading.Thread(target=_heartbeat_loop, args=(hb_stop,), name="heartbeat", daemon=True).start()
        if WORKER_ID == "mac":   # the Mac mic's keeper — see _listener_supervisor
            threading.Thread(target=_listener_supervisor, args=(hb_stop,), name="listener-supervisor", daemon=True).start()

        try:
            while _running:
                try:
                    processed = one_iteration(db)
                except Exception as e:  # noqa: BLE001 — a failed claim must not end the worker
                    log(f"iteration failed: {e}")
                    processed = False
                # Loop quickly if we just worked (drain the queue); else wait for the next INSERT.
                # waker.wait wakes instantly on a realtime INSERT; the timeout is only the missed-event
                # backstop — slow while the subscription is healthy, tight when realtime is down.
                if not processed:
                    # Cadence, standby FIRST: a standing-by worker polls rarely regardless of realtime
                    # health — it isn't the one claiming, realtime wakes it on real commands, and it ramps
                    # back the instant the primary goes silent (should_defer flips false). Only the ACTIVE
                    # worker tightens to the degraded 2s poll when realtime drops; otherwise healthy backstop.
                    if _holding:
                        wait_s = HOLD_RETRY_S   # he is still talking; look again shortly
                    elif _standby:
                        wait_s = (max(0.5, _failover_at - time.monotonic()) if _failover_at
                                  else STANDBY_POLL_S)
                    elif not waker.healthy:
                        wait_s = IDLE_POLL_DEGRADED_S
                    else:
                        wait_s = IDLE_POLL_S
                    waker.wait(wait_s)
        finally:
            hb_stop.set()

    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
