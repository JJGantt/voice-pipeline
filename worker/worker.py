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
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx

import ops_report as ops

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

# The worker never bills the Console API: every turn runs on the Claude subscription, and a failure is
# retried and then surfaced, never finished on the key. The key may still be in the environment (older
# .env files, the Fly secret), so it is popped at import — a subprocess that inherited it would quietly
# bill the API instead of the subscription.
os.environ.pop("ANTHROPIC_API_KEY", None)
# Every hub turn runs on ONE model with the full toolset: Opus at its default effort, which is adaptive —
# it thinks as hard as the turn needs. Quality over speed, by Jared's call (2026-10-29): there is no fast
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
# tool calls in real time. Authenticated by the hub's kiosk token in the URL.
HUB_PUSH_URL = os.environ.get(
    "HUB_PUSH_URL",
    "https://jared-status-hub.fly.dev/api/voice-activity?token=REDACTED",
)


# Set for the duration of a FALLBACK command (`process_request`): the hub is the thing that did not answer,
# so every push to it would only spend its timeout, once per tool call. One request runs at a time.
_hub_unreachable = False


def push_activity(text: str, state: str = "tool") -> None:
    """Fire a transient activity event to the hub (best-effort; never raises)."""
    if not HUB_PUSH_URL or _hub_unreachable:
        return
    try:
        httpx.post(HUB_PUSH_URL, json={"text": text, "state": state, "focus": "hub"}, timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


# Failure alerts. A request is not allowed to fail silently: a subscription run that still fails after
# its retries (`run_subscription`), an orphaned request, a transcription that nothing could do — each is
# reported to the ops responder (health/responder), which groups repeats into one incident, starts an
# agent to investigate, and tells Jared. `<stage> · <class>` leads the message so repeats of one fault
# group together however their error text differs.


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
    """Report a worker failure to the ops responder. `detail` is what the investigating agent is told
    about the request and what became of it."""
    kind = classify_failure(err)
    msg = f"{stage} · {kind} · {(err or '')[:300]}"
    log(f"ALERT {msg}")
    ops.report(msg, context={"detail": detail[:2000]} if detail else None)


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

# Estimating nutrition, appended to every hub turn. The web-lookup rule: from memory Opus was off 20–40%
# on less-common branded/restaurant items (Jinya vegan ramen 960 vs ~790, La Colombe draft latte 130–150
# vs ~110) and searching fixed them, while famous chains and generic foods came out exact without it — so
# search only where memory is weak (docs/ROUTING.md).
NUTRITION_DIRECTIVE = (
    "\n\n[If this involves estimating nutrition for a dish whose exact contents aren't given, decompose "
    "it into its likely ingredients with realistic restaurant portions, estimate each component's macros, "
    "and sum them — don't lowball. ALWAYS use the structured per-ingredient breakdown the log tool "
    "supports: give each component its OWN protein/carbs/fat (and calories when you know them); the server "
    "computes the meal's totals by summing the ingredients, so keep each component's macros accurate. "
    "When the food is a specific restaurant menu item or a branded/packaged product and you can't quote "
    "its exact published nutrition with certainty, search the web for it before logging — prefer the "
    "restaurant's or brand's own nutrition page over third-party trackers. Don't search for generic foods "
    "(eggs, rice, an apple), saved recipes, or repeats of something already in the log; use your own "
    "knowledge or the stored numbers for those.]"
)

WEB_TOOLS = ["WebSearch", "WebFetch"]
# His email and texts, named tool by tool so the two sends can be held apart below.
MAIL_TOOLS = [
    "mcp__gmail__search_email", "mcp__gmail__get_email", "mcp__gmail__get_email_thread",
    "mcp__gmail__list_labels", "mcp__gmail__find_email_contact", "mcp__gmail__create_draft",
    "mcp__gmail__modify_email",
    "mcp__messages__list_chats", "mcp__messages__get_thread", "mcp__messages__search_messages",
    "mcp__messages__list_contacts",
]
# Sending a text or an email reaches another person, so it is decided per TURN, not per list: allowed
# when he pushed a button or typed, never from the always-on room mics, which hear the whole room and
# would send a sentence that was never meant for the assistant. See SdkRunner._fence.
SEND_TOOLS = ["mcp__messages__send_message", "mcp__gmail__send_email"]
# Tools that only mean something next to his signed-in browser, which this worker does not have: a cart
# URL has to be opened there, a cart recording is what its /cart page showed, and an item id is read off
# the retailer's site. Here they could only be called with made-up values, so they are REMOVED from every
# runner's schema: a cart request has to become a start_session, never a turn with no browser.
SESSION_TOOLS = ["mcp__data__build_walmart_cart_url", "mcp__data__record_walmart_cart",
                 "mcp__data__save_shopping_item"]
ALL_TOOLS = ["mcp__data", "mcp__voice", "mcp__govee-lights", "mcp__petlibro-feeder", *WEB_TOOLS, *MAIL_TOOLS, *SEND_TOOLS]


# Appended to every hub turn: how to pick between tools that look interchangeable.
TOOLS_DIRECTIVE = (
    "\n\n[When he explicitly names a RECIPE — 'my oatmeal recipe', 'the gochujang tofu recipe' — log or "
    "stage it with log_recipe. A meal he describes by a past log ('same as yesterday', 'what I had "
    "Monday') is NEVER a recipe, even when a recipe shares its name: it is a copy of that logged entry. "
    "For sets, call get_workout_catalog first and pass the matching exercise's ID — however he said it, "
    "plural or singular; only a movement genuinely absent from the catalog gets upsert_catalog_exercise. "
    "LOOKING SOMETHING UP is done right here with WebSearch and WebFetch: a fact, a score, a listing, "
    "opening hours, a place, anything current. Search, read the pages you need, and answer in your "
    "reply. A session is only for what needs his SIGNED-IN browser: ACTING on a site (his Amazon or "
    "Walmart cart, an order, a ride, a booking, a form) or reading something behind his logins (an "
    "order's status, what is in a cart). That is a start_session on the Pi with his request passed "
    "through. The session's first reply is posted back into this conversation on its own, so say only "
    "that it has started; never offer to check on it later, because nothing will run you again to do "
    "it. Never answer a question about the current world out of your own memory, and never tell him you "
    "cannot browse.]"
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

# Pictures sent from the iPhone share sheet (source 'share', apps/Share in status-hub) — usually a
# screenshot. ATTACH_DIRECTIVE's one-meal framing is wrong for these: a screenshot is something he wants
# looked at or acted on, and logging it as food would be a confident wrong answer.
SHARE_DIRECTIVE = (
    "\n\n[The user shared {n} image(s) from their iPhone's share sheet — usually screenshots — saved "
    "locally at the paths below. View EVERY image with the Read tool before answering. Do what their "
    "text asks about them. With no text, work out from the image what they most likely want (what it "
    "shows, what it means, or the obvious action it calls for) and answer that. Do not log nutrition "
    "unless they ask.\nImages:\n{paths}]"
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
    "\n\n[You are his orchestrator: the one assistant he talks to for everything. When he describes work "
    "that needs a real machine — writing or changing code, debugging, building or deploying, digging "
    "through one of his projects, anything that needs his signed-in browser — start a session for it "
    "with start_session, whether or not he says the word 'session'. Use list_repos to pick the project "
    "the work belongs to, and omit repo when it is unclear. Handle it yourself only when your own tools "
    "cover it: his data, lists, lights, a web lookup, a question you can answer. "
    "When something you try for him fails because a tool is BROKEN or MISSING (it errors, misbehaves, "
    "or nothing you have can do what he asked), do not just report it: also start a session to fix it, "
    "with a prompt giving his request, the tool you called and its exact error, and say in your reply "
    "that you did. A failure that is only the state of the world (the TV is off, an item is out of "
    "stock) is not a bug; report it and start nothing. "
    "Sessions post their replies into this conversation, shown as 'Session \"<name>\" (<id>)'. When what "
    "he says is plainly his answer to one of them — answering its question, approving or correcting its "
    "plan, telling it to go ahead — send his words to that session with follow_up, passing its id, "
    "instead of answering it yourself or starting a new session. When you cannot tell whether he is "
    "replying to a running session or describing new work, start a new session: he gives a command and "
    "looks away, so a question back from you usually goes unseen.]"
)

# Appended to every hub turn. The watch keeps a flagged reply reachable until it has actually been on
# screen (status-hub docs/WATCH.md, "Unread answers"), so a later command cannot bury it; most replies
# are confirmations he never needs to read, and those must NOT be flagged or they would queue up
# unread forever. The marker is stripped before the reply is stored (`take_read_marker`), and the
# replayed day puts it back on flagged replies (`recent_context`), so the history the model reads
# shows the convention being followed rather than a run of untagged answers to copy.
READ_MARKER = "[[read]]"
READ_DIRECTIVE = (
    "\n\n[Decide by what he ASKED FOR, not by what your reply happens to contain. If he asked a question "
    "or asked you to find something out for him (a fact, a lookup, a calculation, a summary, a "
    "recommendation, what he did last time), end your reply with " + READ_MARKER + " as the very last "
    "thing. If he asked you to DO something, leave it off, even when your reply describes what you "
    "picked, copied, looked up along the way or worked out to do it: logging, staging or editing food, "
    "a set, a list item, a light, putting something on the TV, starting, moving or messaging a session. "
    "Also leave it off when you did nothing because the words were not meant for you. One exception "
    "overrides all of this: if anything you tried to do for him failed, was refused, only partly "
    "worked, or could not be done, ALWAYS end with the marker, because he assumes it worked and needs "
    "to find out it did not. The marker is removed before he sees the reply.]"
)


def take_read_marker(text: str) -> tuple[str, bool]:
    """The reply without the read marker, and whether it carried one."""
    if READ_MARKER not in (text or ""):
        return text, False
    return text.replace(READ_MARKER, "").strip(), True


# A second speech-to-text reading from a different Whisper model, present only when its words differ
# from the transcript (status-hub /api/voice-command, voice_requests.alt_transcript). The two mishear
# different words, so the model is told to reconcile them rather than trust either: large-v3 wrote
# "pair putt" where turbo heard "haircut", and turbo wrote "to view" where large heard "a to-do".
ALT_TRANSCRIPT_DIRECTIVE = (
    "\n\n[Speech-to-text ran twice on this recording, with two different models, and they disagree. The "
    "other reading was: \"{alt}\". Where they differ, one of them misheard him. Act on the reading that "
    "makes sense, word by word, and never save a name or a value that appears in only one reading and "
    "makes no sense, as though he said it. If you cannot tell which he said, ask him.]"
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
# runner (SdkRunner._fence, --disallowedTools); this closes the way around that the model could still
# reach for — starting a session whose own tools would send it.
ROOM_SEND_DIRECTIVE = (
    "\n\n[This came from an always-on room mic, which cannot send texts or email. If it asks you to "
    "send, reply to or forward a text or an email, do not do it by any route — no session — and say it "
    "has to come from the button on his phone or watch.]"
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
# memory (continuity is machine-local; a hub turn has none at all). So the conversation you remember is
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


def log_error(msg: str) -> None:
    """A failure: logged like any line, and reported to the ops responder (health/responder), where a new
    incident starts an agent. A line that only says a retry or a fallback is coming stays `log`."""
    log(msg)
    ops.report(msg)


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
            log_error(f"GET {table}?{query} failed: {e}")
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
            log_error(f"POST {table} failed: {e}")
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
            log_error(f"PATCH {table}?{query} failed: {e}")
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
            log_error(f"UPSERT {table} failed: {e}")
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
    above = [w for w in WORKER_PRIORITY[:my_rank] if w not in exclude]
    if not above:
        return None   # nobody to defer to: the primary decides without reading presence at all
    # Fresh, not the 30s cache: this runs only while a request is waiting on a worker below the top, and
    # a stale view is exactly what keeps it waiting on a primary that has just announced it is stopping
    # (measured: 9.1s to fail over from a stopped Fly worker, all of it the cache and FAILOVER_S).
    _presence_cache["rows"] = None
    healthy = {r.get("worker") for r in presence_rows(db) if _healthy(r)}
    return next((w for w in above if w in healthy), None)


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
    """The worker THIS one should leave a request to, or None to claim it. In full-agent mode, affinity
    first: a live context window owned by a DIFFERENT worker that is still online keeps the conversation
    there, over priority; if I own it, I claim. Otherwise priority ordering. A request other workers have already failed on
    (`tried`) skips affinity — the owner is likely the worker that just failed it — and goes to the
    highest-ranked worker that has not tried it."""
    if tried:
        return higher_priority_target(db, tried)
    # Affinity only in full-agent mode, whose CLI session lives on one machine. A hub turn is a fresh run
    # handed the day's thread from the live state, which every worker holds, so keeping it on the last
    # claimer bought nothing — and kept commands on a slower worker for up to an hour.
    owner = context_owner(db) if agent_settings()["mode"] else None
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
        log_error(f"local Whisper failed for {rid}: {e}")
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
        log_error(f"untranscribed sweep failed: {e}")


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
        log_error(f"read {f.name} failed: {e}")
    return None


def write_session(agent: bool, session_id: str) -> None:
    f = session_file(agent)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps({"session_id": session_id, "last_activity_iso": now_iso()})
        )
    except Exception as e:  # noqa: BLE001
        log_error(f"write {session_file(agent).name} failed: {e}")


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
                       "&select=id,focus,mode,role,text,created_at,ignored,done,needs_read,session_id,session_label")
            rows += page
            if len(page) < 1000:
                break
    except Exception as e:  # noqa: BLE001
        _note_failure(e)
        log_error(f"live state load failed: {e}")
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
        log_error(f"wakefree auto-off check failed: {e}")


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
        log_error(f"wakefree auto-reenable check failed: {e}")


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
        log_error(f"publish_repo_dirs failed: {e}")


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
    that makes a resumed session drift from what was actually said.

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
        if r.get('role') == 'assistant' and r.get('session_id'):
            # A session's reply posted into the hub thread. Named, so a reply from him that follows it can
            # be sent back to that session with follow_up rather than answered as if the hub had said it.
            who = f"Session \"{r.get('session_label') or r['session_id']}\" ({r['session_id']})"
        txt = (r.get('text') or '').strip()
        if r.get('ignored'):
            return f"[{when}] You (ignored as room noise, not acted on): {txt}"
        if r.get('needs_read') and r.get('role') == 'assistant':
            txt = f"{txt} {READ_MARKER}"
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
                log_error(f"attachment fetch failed ({r.status_code}): {name}")
                continue
            if tmpdir is None:
                tmpdir = tempfile.mkdtemp(prefix="voice-att-")
            fp = os.path.join(tmpdir, name)
            with open(fp, "wb") as f:
                f.write(r.content)
            local.append(fp)
        except Exception as e:  # noqa: BLE001
            log_error(f"attachment fetch error: {name}: {e}")
    return tmpdir, local


# The current command's audio-recording START time (voice_requests.capture_start), written here per
# command. The data MCP's now() reads it so a voice-logged SET lands at when Jared actually spoke — his
# rest-clock reference — not ~30s later when the command finishes the pipeline.
#
# It is a FILE and not an env value, and that distinction is the whole point. The warm hub client
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
        log_error(f"cmd_ts write failed: {e}")   # a stale stamp is worse than none, so this is never fatal


def write_mcp_config() -> str:
    env = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
    # DATA_CALLER=hub marks sessions this worker dispatches, so their first reply is relayed back
    # into the hub thread (data-layer startSession → orchestrator/src/relay.js).
    data_env = {**env, "DATA_TS_FILE": str(CMD_TS_FILE),
                "DATA_CALLER": "hub"}
    cfg = {
        "mcpServers": {
            "data": {"command": "node", "args": [DATA_MCP_JS], "env": data_env},
            "voice": {"command": "node", "args": [VOICE_MCP_JS], "env": env},
            "govee-lights": {"command": GOVEE_PY, "args": [GOVEE_MCP_PY], "env": env},
        }
    }
    # Only where the feeder server is installed: it is LAN-only, and a registered server whose files are
    # missing (Fly) fails to start on every turn.
    if os.path.exists(PETLIBRO_MCP_PY):
        cfg["mcpServers"]["petlibro-feeder"] = {"command": PETLIBRO_PY, "args": [PETLIBRO_MCP_PY], "env": env}
    # The Mac and the Pi keep the Gmail credential in gmail-mcp/.env; Fly has no such file and holds it
    # as secrets, passed through here — only what is set, since an empty value would shadow the .env.
    # Registered only where one of the two exists: without it every Gmail call fails at the token.
    g_env = {k: os.environ[k] for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
             if os.environ.get(k)}
    if os.path.exists(GMAIL_MCP_PY) and (len(g_env) == 3 or os.path.exists(pathlib.Path(GMAIL_MCP_PY).parent / ".env")):
        cfg["mcpServers"]["gmail"] = {"command": GMAIL_PY, "args": [GMAIL_MCP_PY], "env": g_env}
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
    # room-mic turn. A deny beats any allow rule, and taking them out of sight loses nothing.
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
    """Warm Claude Agent SDK client — every hub turn runs here, billed to the subscription.

    One long-lived headless Claude Code process holds the four MCP servers, so the per-command spawn
    spawn cost is paid once, not per turn. Each hub turn runs in a FRESH conversation (`/clear` on the
    same warm process, see _turn): the caller replays the day's hub conversation into the prompt. Pinned to HUB_MODEL
    at HUB_EFFORT with the full toolset at connect; full-agent and fallback turns keep their own `claude -p`
    spawns. A failed turn is retried by `run_subscription` and then surfaced, never moved onto the paid API.
    """

    def __init__(self, model: str = HUB_MODEL, effort: "str | None" = HUB_EFFORT):
        """The full toolset plus Read, which the fence allows only inside the current turn's photo
        directory."""
        self.model = model
        self.effort = effort
        self.tools = ALL_TOOLS + ["Read"]
        self._read_dir = None  # this turn's attachment directory, the only place Read may look
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

        gated = "That tool is not available to this assistant."
        exact = set(self.tools)
        servers = tuple(t + "__" for t in self.tools if t.startswith("mcp__") and t.count("__") == 1)

        def _decide(decision, reason=None):
            out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
            if reason:
                out["permissionDecisionReason"] = reason
            return {"hookSpecificOutput": out}

        async def _fence(input_data, tool_use_id, context):  # noqa: ARG001
            # The allow list, enforced BEFORE any permission rule. This client is Claude Code, which also
            # applies the host user's own settings — and the Mac's pre-approve every data, gmail and
            # messages tool in auto mode; with allowed_tools alone a room-mic turn sent an email in the
            # first test of the send rule. A hook's deny outranks any allow rule, so the list holds here.
            name = input_data.get("tool_name") or ""
            if name in SEND_TOOLS and name in exact:
                # The one per-turn decision: the client is warm across turns, so only the flag run()
                # sets can say which mic this turn came from.
                if self._ambient:
                    return _decide("deny", "Sending texts and email is off for the always-on room mics. "
                                           "He can send it with the button on his phone or watch, or by typing it.")
                return _decide("allow")
            if name == "Read":   # photo turns only: the CLI views an image by reading its file
                path = os.path.realpath(str((input_data.get("tool_input") or {}).get("file_path") or ""))
                if self._read_dir and path.startswith(os.path.realpath(self._read_dir) + os.sep):
                    return _decide("allow")
                return _decide("deny", "Read is only for the photos attached to this message.")
            if name in exact or name.startswith(servers):
                return {}
            return _decide("deny", gated)

        async def _deny(tool_name, tool_input, context):  # noqa: ARG001
            # Reached only by a tool the fence let through without pre-approval; there is none today.
            # (Without a callback the SDK raises on a permission request, failing the whole turn.)
            return PermissionResultDeny(message=gated)

        opts = ClaudeAgentOptions(
            model=self.model,
            **({"effort": self.effort} if self.effort else {}),
            # The SDK ships its own copy of Claude Code, which only moves when the package is upgraded, and
            # the alias "sonnet" resolves to whatever model THAT copy knows. Running the installed CLI keeps
            # this path on the same models as every other turn instead of drifting a release behind.
            cli_path=shutil.which(CLAUDE_BIN),
            mcp_servers=write_mcp_config(),
            allowed_tools=list(self.tools),
            disallowed_tools=list(SESSION_TOOLS),   # browser-only: out of sight entirely
            # The stable hub framing lives here so the prefix caches across turns; per-turn context
            # (the replayed conversation) rides on the prompt instead.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": HUB_SYSTEM + "\n\n" + SHARED_STATE_NOTE,
                           "exclude_dynamic_sections": True},
            env={"ENABLE_TOOL_SEARCH": "false"},   # all schemas inline, same as the -p path
            can_use_tool=_deny,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_fence])]},
            cwd=os.path.expanduser("~"),
            # The SDK reads the CLI's stdout one JSON line at a time and dies on any line over 1MB (its
            # default). A photo turn's Read result carries the image base64 in one line, so a phone photo
            # kills the whole turn ("JSON message exceeded maximum buffer size"). The buffer only grows
            # to the size of the line actually read, so a high ceiling costs nothing on ordinary turns.
            max_buffer_size=64 * 1024 * 1024,
        )
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        self._client = client
        self._used = False
        log(f"sdk runner: warm client up ({self.model}{' @' + self.effort if self.effort else ''})")

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

    def run(self, transcript, on_event=None, ambient=True, read_dir=None) -> dict:
        """`ambient` says the turn came from a room mic, which is what _gate reads for the sends;
        `read_dir` is where this turn's photos are. Turns run one at a time, so a flag on the runner is
        exactly one turn's."""
        self._start_loop()
        self._ambient, self._read_dir = ambient, read_dir
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
        log_error(f"transcribing sweep failed: {e}")


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
            claimed_at = _parse_ts(req.get("claimed_at") or "")
            # A different epoch proves the claimer died only if the owner has heartbeated SINCE the claim.
            # A presence row older than the claim (cached here up to PRESENCE_CACHE_S, or simply written
            # before a just-restarted process's first beat) still carries the previous process's epoch,
            # and would orphan a live claim the instant it was made.
            epoch_after_claim = bool(ts) and bool(claimed_at) and ts > claimed_at
            alive = fresh and (not req.get("claim_epoch") or not epoch or epoch == req.get("claim_epoch")
                               or not epoch_after_claim)
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
    # The fallback worker reads `fallback` rows in the SAME query, so the watch's way in costs no extra
    # read per cycle. Every other worker's filter stays `pending`, which is what keeps them off it.
    statuses = "in.(pending,fallback)" if WORKER_ID == FALLBACK_WORKER else "eq.pending"
    pending = db.get(
        "voice_requests",
        f"status={statuses}&failed_on=not.cs.{{{WORKER_ID}}}&order=created_at.asc&limit=8"
        "&select=id,transcript,source,status,created_at,updated_at,capture_start,capture_end,failed_on",
    )
    fb = next((r for r in pending or [] if r.get("status") == "fallback"), None)
    if fb and _running:
        # No deferring, holding or grouping: it came from one watch because the hub did not answer, and
        # nobody else will take it.
        got = db.patch("voice_requests", f"id=eq.{fb['id']}&status=eq.fallback",
                       {"status": "claimed", "claimed_by": WORKER_ID, "claim_epoch": WORKER_EPOCH,
                        "claimed_at": now_iso(), "assigned_to": WORKER_ID, "updated_at": now_iso()},
                       extra_headers={"Prefer": "return=representation"})
        return got[0] if got else None
    pending = [r for r in pending or [] if r.get("status") == "pending"]
    # SPOKEN ORDER, not arrival order. A watch that loses the hub holds its clips and sends them together
    # when it gets back, and the uploads land in whatever order they finish: a correction ("I said 11")
    # then runs before the command it corrects and guesses at it. A row with no capture window (typed)
    # sorts by when it arrived. Both are Postgres UTC timestamps, so they compare as strings.
    pending.sort(key=lambda r: r.get("capture_start") or r.get("created_at") or "")
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
    oldest_at = _parse_ts(min(r.get("created_at") or "" for r in pending))
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

    # A stop signal can land anywhere in the reads above, and the handler only clears _running: without
    # this check the worker claims a command it has seconds to live for, and the next worker gets it only
    # after reclaim_stale declares this one dead.
    if not _running:
        return None
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


def fallback_directive(db: Supabase, rid: str) -> str:
    """What a fallback command's run is told. The FIRST fallback command of an outage starts the
    troubleshooting session; later ones within FALLBACK_OUTAGE_S only say the fallback is in use, so a
    morning of commands during one outage is one investigation, not one per command."""
    # Z, not +00:00: a '+' in a query string reads as a space, and the filter then fails outright.
    since = (datetime.now(timezone.utc) - timedelta(seconds=FALLBACK_OUTAGE_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        earlier = db.get("voice_requests", f"source=eq.watch-fallback&id=neq.{rid}&created_at=gte.{since}&select=id&limit=1")
    except Exception:  # noqa: BLE001 — unknown: investigate rather than stay quiet
        earlier = []
    note = ("\n\n[FALLBACK: this command reached you through the watch's FALLBACK path — the watch could not "
            "get an answer from the hub (Fly app jared-status-hub), so it sent the clip through a Supabase edge "
            "function and you are running on the Pi. Open your reply with one short sentence saying it came "
            "through the fallback because the hub did not answer, then handle the command itself exactly as you "
            "normally would. Your reply reaches the watch directly; the hub's own surfaces may be down.")
    if earlier:
        return note + (" An earlier command in this outage already started a troubleshooting session, so do "
                       "not start another.]")
    return note + (" This is the first fallback command of this outage: AFTER handling the command, start ONE "
                   "session with the data tool start_session (machine 'pi', repo 'status-hub', label 'Hub "
                   "unreachable: troubleshoot') whose prompt asks it to find out why the watch could not reach "
                   "jared-status-hub at " + now_iso() + " — check https://jared-status-hub.fly.dev/healthz, "
                   "`fly status -a jared-status-hub`, its logs and Fly's status page — and to report what it "
                   "finds. Say in your reply that you started it.]")


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
            log_error(f"reset: unlink session failed: {e}")
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

    # THE HUB DID NOT ANSWER. The watch sent this through the voice-fallback edge function; it is run
    # here on Opus with every tool, says so, and the first one of an outage starts a session to find out
    # why. See fallback_directive.
    fallback = (req.get("source") or "") == "watch-fallback"
    global _hub_unreachable
    _hub_unreachable = fallback

    focus = "hub" if fallback else get_focus()

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
    # To the SECOND, in both the readable clock and the literal occurred_at value: a set's stamp is the
    # zero of the watch's m:ss rest clock, and a minute-precision stamp let the model pass HH:MM:00,
    # which started that clock up to 59s before he had finished the set.
    # Rides on the prompt, NOT the cached system block, so it can't change the per-command system prefix
    # and break the prompt cache.
    _cap = _parse_ts((req.get("capture_start") or "").strip())
    client_now = (req.get("client_now") or "").strip()
    _m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$", client_now)
    if _cap:
        _now = _cap.astimezone()   # UTC capture stamp → this machine's local wall clock
        _now_str = _now.strftime("%A, %B %-d, %Y, %-I:%M:%S %p %Z")
    elif _m:
        _now = datetime(*(int(x) for x in _m.groups()))
        _now_str = _now.strftime("%A, %B %-d, %Y, %-I:%M:%S %p")
    else:
        _now = datetime.now().astimezone()
        _now_str = _now.strftime("%A, %B %-d, %Y, %-I:%M:%S %p %Z")
    prompt += ("\n\n[This command was said/typed at " + _now_str + " ("
               + _now.strftime("%Y-%m-%d") + ") — treat THAT as 'now'. Anchor every relative date/time to "
               "it: both relative dates — 'today', 'yesterday', 'last night', 'this morning', 'last week' "
               "— and relative arithmetic — 'in 11 minutes', 'an hour from now'. Never your training data, "
               "and never your own guess at the current instant: the pipeline took some seconds to reach "
               "you and that lag is not part of what he asked for. When logging something for 'now' (no "
               "different time named), pass this exact moment as occurred_at — " + _now.strftime("%Y-%m-%dT%H:%M:%S")
               + ", seconds included — to the logging tool explicitly rather than omitting it — the tool's own clock may not be wherever "
               "this was actually said/typed.]")
    if ambient:
        prompt += ADDRESSING_DIRECTIVE + ROOM_SEND_DIRECTIVE
    else:
        prompt += DELIBERATE_DIRECTIVE
    prompt += READ_DIRECTIVE
    alt = (req.get("alt_transcript") or "").strip()
    if alt:
        prompt += ALT_TRANSCRIPT_DIRECTIVE.format(alt=alt)

    # ---- ONE route. Every hub command, photos included, runs on the warm SDK client (HUB_MODEL, full
    # toolset). Full-agent mode is the unrestricted coding agent on its own --resume session.
    attachments = req.get("attachments") or []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except Exception:  # noqa: BLE001
            attachments = []
    use_agent = session_agent   # full-agent (coding) mode → the unrestricted agent, on its own session
    hub_route = f"hub {HUB_MODEL}@{HUB_EFFORT or 'default'}" + (" +photo" if attachments else "")
    log(f"route={'fallback→opus agent' if fallback else 'agent' if use_agent else hub_route} src={source}")
    ping_esp_led("blink")   # working — the ESP light blinks for EVERY command, not just ESP-sourced ones
    push_activity(transcript[:240], "working")   # bottom-strip: the prompt being worked

    # Photo turns: pull the attachment objects local so Read can view them, and put the paths + the ONE-item
    # framing on the prompt.
    att_tmp, att_paths = None, []
    if attachments:
        att_tmp, att_paths = fetch_attachments(attachments)
        if att_paths:
            directive = SHARE_DIRECTIVE if source == "share" else ATTACH_DIRECTIVE
            prompt += directive.format(n=len(att_paths), paths="\n".join(att_paths))
            log(f"attachments: {len(att_paths)} photo(s) staged in {att_tmp}")
        else:
            prompt += ("\n\n[The user attached photo(s), but they could not be retrieved — tell them "
                       "that briefly instead of guessing at the contents.]")

    # Dispatch — one route, the same on every worker (Pi, Mac, and Fly, which holds its own subscription
    # token). Everything runs on the Claude SUBSCRIPTION; there is no paid path. A run that fails
    # transiently is retried (`run_subscription`), and one that still fails surfaces as an error. `ctx`
    # (the replayed hub conversation) rides on a FRESH conversation; there is no --resume for hub turns.
    # agent → the unrestricted coding agent (its own --resume session). Everything else, photos included,
    # → the warm SDK client. A failure that called no tool is handed to another worker below rather than
    # ended here.
    hub_prompt = ctx + prompt   # ctx is "" in agent mode (uses --resume) or when there's no live conversation
    room_deny = SEND_TOOLS if ambient else None   # the room mics never send a text or an email
    unready = claude_unready()
    if unready:
        result = {"session_id": None, "text": "", "result": "", "ok": False, "raw_error": unready}
    elif fallback:
        # Opus, the unrestricted agent, a FRESH run with the day's conversation replayed — whatever broke,
        # it can handle the command and see enough to say what is wrong. Never the --resume agent session.
        fb_prompt = (ctx or recent_context()) + prompt + fallback_directive(db, rid)
        result = run_subscription(
            lambda: run_claude(fb_prompt, None, on_event=_emit, system_append=AGENT_SYSTEM + "\n\n" + SHARED_STATE_NOTE,
                               model="opus", agent=True, cwd=os.path.expanduser("~")),
            "subscription Opus (fallback)")
    elif use_agent:
        result = run_claude(prompt, resume_id, on_event=_emit, system_append=AGENT_SYSTEM + "\n\n" + SHARED_STATE_NOTE, model="opus",
                            agent=True, cwd=ag["cwd"], allow_read=bool(att_paths),
                            deny_tools=room_deny)
    else:
        # The warm SDK client, not a per-command `claude -p`: that spawn (CLI + four stdio MCP servers)
        # measured 3.6s on the Pi before the model saw a token; the warm client pays it once.
        result = run_subscription(
            lambda: SDK.run(hub_prompt + NUTRITION_DIRECTIVE + TOOLS_DIRECTIVE, on_event=_emit,
                            ambient=ambient, read_dir=att_tmp if att_paths else None),
            "subscription hub")

    if att_tmp:
        shutil.rmtree(att_tmp, ignore_errors=True)   # staged photos served their turn

    if result.get("session_id") and session_agent:   # only full-agent mode keeps a CLI session; hub replays ctx
        write_session(True, result["session_id"])
        log(f"captured agent session_id {result['session_id']}")

    text = (result.get("text") or "").strip()

    # The same net for __IGNORE__ on a turn he MEANT. Only room-mic turns are taught the sentinel, but a
    # model can still pick it up (it did, from replayed history) — and on a deliberate turn there is no
    # ignore branch to catch it, so it was saved and pushed to him as the reply. Re-run clean, with no
    # context to mimic; the prompt already carries DELIBERATE_DIRECTIVE.
    if not ambient and is_ignore(text) and not result.get("used_tools"):
        log("deliberate turn answered __IGNORE__ → clean re-run (no context)")
        result = SDK.run(prompt + NUTRITION_DIRECTIVE + TOOLS_DIRECTIVE, on_event=_emit, ambient=ambient)
        text = (result.get("text") or "").strip()

    text, needs_read = take_read_marker(text)

    # A failure here is this machine's, not the command's, until every machine has tried it: hand it on.
    # Nothing has been written to the thread yet, so the worker that takes it writes the whole turn.
    if not result.get("ok") and not fallback and not req.get("_acted") and hand_off(db, req, result.get("raw_error") or "unknown error"):
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
             "text": text, "done": True, "worker": WORKER_ID, "needs_read": needs_read},
        )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "done", "result": text, "updated_at": now_iso()},
        )
        log(f"done request {rid}: {text[:120]!r}")
    else:
        err = result.get("raw_error") or "unknown error"
        # Still surface whatever assistant text we did get, if any.
        if result.get("text"):
            db.post(
                "voice_messages",
                {"request_id": rid, "focus": "hub", "mode": msg_mode, "role": "assistant",
                 "text": take_read_marker(result["text"])[0], "done": True, "worker": WORKER_ID},
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
                log_error(f"realtime listener error: {e}; retrying in 5s (poll fallback active)")
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
        if status == "pending" or (status == "fallback" and WORKER_ID == FALLBACK_WORKER):
            self.event.set()
        elif status == "untranscribed" and rec.get("audio") and local_whisper_ready():
            threading.Thread(target=transcribe_local, args=(rec,), daemon=True).start()

    def _on_live(self, payload) -> None:
        try:
            d = payload.get("data") or {}
            live_event(str(d.get("table") or ""), str(d.get("type") or "").split(".")[-1].upper(),
                       d.get("record") or {}, d.get("old_record") or {})
        except Exception as e:  # noqa: BLE001 — a bad frame must not take the listener down
            log_error(f"live event failed: {e}")

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

    # Sent HERE, before returning: an idle worker exits within a moment of this handler, and a daemon
    # thread sending it was killed with the process first — the Pi then waited out FAILOVER_S on a Fly
    # worker that was already gone.
    try:
        with httpx.Client(timeout=3) as c:
            heartbeat(Supabase(c))
    except Exception:  # noqa: BLE001
        pass
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


# The ops responder's heartbeat (health/responder) says the WORK loop is alive, not just this process.
# It is sent from the heartbeat thread because the main loop legitimately blocks for a whole turn, so it
# goes out only while the main loop has come round within LOOP_STALL_S: longer than any turn should take,
# so a turn is never mistaken for a hang, and a loop wedged past it goes quiet like a dead worker.
LOOP_STALL_S = float(os.environ.get("LOOP_STALL_S", "1800"))
OPS_BEAT_S = 60
_loop_at = time.monotonic()


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
                if time.monotonic() - _loop_at < LOOP_STALL_S:
                    ops.beat(max(OPS_BEAT_S, STANDBY_HEARTBEAT_S if _standby else HEARTBEAT_INTERVAL_S),
                             {"standby": bool(_standby)})
                maybe_auto_reenable_wakefree()   # self-throttled to ~5 min; rides the beat, no extra timer
            except Exception as e:  # noqa: BLE001
                log_error(f"heartbeat thread error: {e}")
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
            log_error(f"listener supervisor error: {e}")
        stop_evt.wait(60)


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY is required (service_role key). See README.",
              file=sys.stderr)
        return 2

    once = "--once" in sys.argv

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    ops.install(machine=WORKER_ID, service="voice-worker")
    log(f"starting (priority={WORKER_PRIORITY}, state_dir={STATE_DIR}, once={once})")

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
            global _loop_at
            while _running:
                _loop_at = time.monotonic()
                try:
                    processed = one_iteration(db)
                except Exception as e:  # noqa: BLE001 — a failed claim must not end the worker
                    log_error(f"iteration failed: {e}")
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
