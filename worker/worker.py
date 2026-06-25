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
- The hub `claude` session is reused via `--resume` and reset on a 2h idle gap or
  a calendar-day rollover (see should_reset_session()).
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone, date, timedelta
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

# Low-latency activity push to the status-hub server: transient "what Jarvis is doing" events go
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


def notify_phone(body: str, title: str = "Jarvis") -> None:
    """Send a voice command's final result to Jared's phone as a push notification. Threaded + best-
    effort so it never delays closing out the request and never raises."""
    if not (HUB_NOTIFY_URL and body):
        return

    def _send():
        try:
            httpx.post(HUB_NOTIFY_URL, json={"title": title, "body": body[:300], "kind": "voice"}, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_send, daemon=True).start()


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

# Timing
HEARTBEAT_INTERVAL_S = 10.0
IDLE_POLL_S = 1.0
HEARTBEAT_FRESH_S = 30.0      # a higher-priority worker counts as "present" within this
DEDUP_WINDOW_S = 10.0         # fallback only: 2 mics' enqueue times for rows that predate capture-window stamping
DEDUP_TOL_S = 0.75            # pad each recording window by this (Fly↔Mac clock skew + ESP→Fly stream latency)
SESSION_IDLE_RESET_S = 5 * 60       # default reset budget (5min idle); hub setting session_reset_s overrides
HTTP_TIMEOUT_S = 30.0
CLAUDE_TIMEOUT_S = float(os.environ.get("CLAUDE_TIMEOUT_S", "300"))
AGENT_TIMEOUT_S = float(os.environ.get("AGENT_TIMEOUT_S", "900"))  # full-agent (coding) runs get a longer leash

HUB_SESSION_FILE = STATE_DIR / "hub_session.json"
# Sentinel the hub UI enqueues to force a fresh Claude session (manual "Reset context").
# Must match VOICE_RESET_SENTINEL in the status-hub server.
RESET_SENTINEL = "__RESET_CONTEXT__"

# Quick-add requests (source 'web-quick', from the hub's bottom-right mic FAB) show their reply in a
# tiny status chip that holds only a few words before it fades. Append this directive to the turn so
# Jarvis still does the work but confirms it in one short phrase. Only the directive-free transcript
# is shown as the user's bubble; this text is invisible to the conversation.
QUICK_REPLY_DIRECTIVE = (
    "\n\n[Reply format for THIS turn only: this came from the quick-add voice button, whose reply shows "
    "in a tiny status chip for a couple seconds. Do the task, then confirm in ONE phrase of 3–7 words "
    "naming what was done — e.g. \"Lunch bagel logged\", \"Weight logged: 178\", \"Lights set to blue\", "
    "\"Added milk to the list\". No greeting, no explanation, no follow-up question. If it failed, say so "
    "in under 7 words.]"
)

# Power tier (the hub's "brain" button): maximum-precision path for hard estimates — typically a
# restaurant dish whose ingredients/macros are unknown. Routed to Opus, and this directive both asks
# for careful decomposition AND contains the "ultrathink" keyword, which is how `claude -p` unlocks
# its largest extended-thinking budget (there is no CLI flag for it).
POWER_DIRECTIVE = (
    "\n\n[Precision mode — ultrathink. Take your time and reason carefully before acting. If this "
    "involves estimating nutrition for a dish whose exact contents aren't given, decompose it into its "
    "likely ingredients with realistic restaurant portions, estimate each component's macros, and sum "
    "them — don't lowball. Prefer the structured per-ingredient breakdown the log tool supports.]"
)

# Medium thinking: a mild nudge that contains the bare "think" keyword (the smallest extended-thinking
# budget on `claude -p`), without the "ultrathink" maximum. Pairs with API effort="medium".
MEDIUM_DIRECTIVE = "\n\n[Think it through before acting.]"

# Sentinel the model emits to silently drop a turn (wake-word false positive). Matched loosely below.
IGNORE_SENTINEL = "__IGNORE__"

# Always-on voice listeners (ESP, Mac) occasionally false-trigger on noise/silence, so the transcript
# can be a mishear, not a real request (a stray word, or a Whisper artifact like "thank you" from
# silence). Appended to transcribed turns so the model can drop obvious noise instead of earnestly
# answering it. Transcribed sources only — typed input is intentional and never gets this.
WAKE_NOISE_DIRECTIVE = (
    "\n\n[This text came from an always-on voice wake-word listener that frequently FALSE-TRIGGERS on "
    "background noise or silence, so it is often a mis-transcription rather than a real instruction. This "
    "system ONLY handles actual commands, requests, or questions you can act on. If the text is NOT a "
    "genuine actionable request — that includes bare pleasantries or acknowledgements ('thank you', "
    "'thanks', 'okay', 'you', 'bye', 'cool'), lone stray words ('speed'), filler, or anything nonsensical "
    "or blatantly unrelated — then it is almost certainly a false trigger: do NOT respond, do NOT thank "
    "them back, do NOT ask for clarification. Reply with EXACTLY "
    f"'{IGNORE_SENTINEL}' and nothing else. BUT a clear, well-formed command or question is ALWAYS a real "
    "request — even if it is identical to one you just handled (a deliberate retry), AND even if it is "
    "preceded or surrounded by a garbled fragment or a misheard wake word (e.g. a stray 'aspie' / 'raspy' "
    "at the start). In that case, mentally strip the junk and ACT on the real command — do NOT discard the "
    "whole utterance. Because this is speech-to-text, individual WORDS are also often misheard as near-"
    "homophones — correct them to the intended meaning using the conversation context (e.g. 'permit'→"
    "'commit', 'lock'→'log', a word that phonetically matches an obvious action or a staged/logged/listed "
    "item) instead of taking the garbled word literally or asking what they meant. "
    f"Use '{IGNORE_SENTINEL}' ONLY when there is NO actionable request anywhere in the text. This also "
    "covers YOU talking but NOT commanding — testing, narrating, thinking aloud, chatting, or explicitly "
    f"saying you don't need anything done: for ALL of those, reply with EXACTLY '{IGNORE_SENTINEL}'. NEVER "
    "explain that there's nothing to do or that it isn't a command (no 'this is background talk', no "
    "'nothing to log') — that explanation gets read ALOUD, which is exactly what must not happen.]"
)

# Voice (ESP/Mac) turns: the worker speaks the final WRITTEN reply itself — programmatically, via the Pi
# audio service (warm Kokoro → the TV) — and pushes it to Jared's phone. No `say` tool, no separate spoken
# phrase. So the reply must be one clean past-tense line. Appended to fly/mac turns (the quick-add FAB has
# its own terser directive; typed web turns get neither). See voice-pipeline/ARCHITECTURE.md.
VOICE_REPLY_DIRECTIVE = (
    "\n\n[Voice command — your reply is spoken aloud and pushed to your phone. Full sentences are fine, but "
    "answer ONLY what was asked and nothing more. A simple action → a quick confirmation ('Logged your weight "
    "at 172.4 pounds.'). A question → just the answer ('About 1,800 calories.') — do NOT volunteer extra info, "
    "progress, remaining amounts, goals, comparisons, or 'you're this far from X.' Never explain your "
    "reasoning, restate the request, narrate what you noticed, or add caveats. Just the answer or what you "
    "did. Plain spoken words only — never any markdown, tables, code, lists, bullets, or symbols. No preamble.]"
)

# Role framing for every hub turn. The base preset is claude_code (a coding agent), so without this
# the model treats requests like a dev session and reaches for Bash/curl. This establishes what it
# actually is. It deliberately gives NO per-tool guidance — each tool's schema documents its own use.
HUB_SYSTEM = (
    "You are the assistant behind Jared's personal life-data hub. This is NOT a coding session and you "
    "are NOT exploring a codebase. Your sole job is to fulfill each request by calling the MCP tools you "
    "have been given to read and update his life data (nutrition, workouts, weight, lists, reminders, "
    "notes, recipes, smart lights, and the like). The tools are the only way to act, and each tool's own "
    "description tells you how to use it — follow those. Bash, curl, a shell, the filesystem, and file "
    "editing are NOT available to you; never attempt them. "
    "GOLDEN RULE: when a request clearly maps to your tools, COMPLETE it in one shot — fill in any "
    "unspecified detail with a reasonable assumption, or look up what you need with the read tools (e.g. "
    "call get_nutrition for 'the meal I had yesterday' and reuse it), instead of refusing or asking the "
    "user to clarify. Only ask when it is genuinely unclear WHAT they want done — never merely because an "
    "amount, a date, or some other detail was left implicit or could simply be looked up. "
    "If truly no available tool can satisfy a request, say so briefly rather than improvising another way."
)

# Full-agent framing — the opposite of HUB_SYSTEM. Used only on the subscription path when "Full agent
# mode" is on (power / custom-local, Mac+Pi). It places NO restriction on capability: the model keeps its
# full native toolset (shell, file editing, web, etc.) AND the data/voice/govee MCP tools, and is told it
# is a general-purpose agent — so Jared can code or do anything by voice. Requests arrive as transcriptions.
AGENT_SYSTEM = (
    "You are Jared's all-purpose assistant, driven by voice. You have your full set of capabilities — you can "
    "write and run code, use the shell, edit files, search the web, and anything else — IN ADDITION to the "
    "data/voice/govee MCP tools for his life data (nutrition, workouts, weight, lists, reminders, notes, "
    "recipes) and smart lights. Use whatever combination of tools the request needs. Requests come from "
    "speech-to-text, so expect occasional mis-hearings; if a request is genuinely unintelligible, say so. "
    "Keep spoken replies brief and to the point."
)


# Govee lights the model can control — pulled from the auto-synced Supabase govee_devices table and
# injected into the system prompt every turn, so the model acts on "the floor lamp" immediately
# instead of burning a round-trip on a list tool. Cached in-process (30 min) to avoid a per-command DB hit.
_govee_cache = {"at": -1e9, "lights": []}
GOVEE_CACHE_TTL = 1800


def _govee_lights() -> list:
    """[(key, display), …] from Supabase govee_devices (hidden=false). key is the tool arg; display matches speech."""
    now = time.monotonic()
    if _govee_cache["lights"] and now - _govee_cache["at"] < GOVEE_CACHE_TTL:
        return _govee_cache["lights"]
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/govee_devices",
                      params={"select": "key,display_name", "hidden": "eq.false", "order": "key"},
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=8)
        out = [(row["key"], row["display_name"]) for row in r.json()]
        _govee_cache["at"], _govee_cache["lights"] = now, out
    except Exception:  # noqa: BLE001 — keep last-known list on a transient failure
        out = _govee_cache["lights"]
    return out


def govee_context() -> str:
    lights = _govee_lights()
    if not lights:
        return ""
    listing = ", ".join(f"{k} ({d})" for k, d in lights)
    return ("Govee lights you can control RIGHT NOW via the govee-lights tools — pass the KEY (the part "
            "before the parentheses) as the `light` arg; do NOT call a list/lookup tool first. Available: "
            + listing + ". If a request matches multiple lights by name (e.g. two 'Bedroom Ceiling'), "
            "control ALL of them.")

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
            log(f"UPSERT {table} failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Heartbeat / priority
# --------------------------------------------------------------------------- #


def heartbeat(db: Supabase) -> None:
    db.upsert(
        "worker_presence",
        {"worker": WORKER_ID, "last_seen": now_iso(), "info": {}},
        on_conflict="worker",
    )


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


def higher_priority_present(db: Supabase) -> bool:
    """True if any worker ranked ABOVE me in WORKER_PRIORITY has a fresh heartbeat."""
    try:
        my_rank = WORKER_PRIORITY.index(WORKER_ID)
    except ValueError:
        # I'm not in the priority list — assume lowest, defer to nobody specific.
        my_rank = len(WORKER_PRIORITY)
    higher = set(WORKER_PRIORITY[:my_rank])
    if not higher:
        return False
    rows = db.get("worker_presence", "select=worker,last_seen")
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.get("worker") in higher:
            ts = _parse_ts(row.get("last_seen", ""))
            if ts and (now - ts).total_seconds() <= HEARTBEAT_FRESH_S:
                return True
    return False


# --------------------------------------------------------------------------- #
# Hub session state
# --------------------------------------------------------------------------- #


def read_hub_session() -> dict | None:
    try:
        if HUB_SESSION_FILE.exists():
            return json.loads(HUB_SESSION_FILE.read_text())
    except Exception as e:  # noqa: BLE001
        log(f"read hub_session.json failed: {e}")
    return None


def write_hub_session(session_id: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HUB_SESSION_FILE.write_text(
            json.dumps({"session_id": session_id, "last_activity_iso": now_iso()})
        )
    except Exception as e:  # noqa: BLE001
        log(f"write hub_session.json failed: {e}")


_reset_cache = {"v": None, "t": 0.0}


def session_reset_s() -> float:
    """Idle budget before the hub conversation resets, from the hub setting `session_reset_s` (cached
    ~30s so it's not a per-command DB hit). Falls back to SESSION_IDLE_RESET_S."""
    now = time.monotonic()
    if _reset_cache["v"] is None or now - _reset_cache["t"] > 30:
        v = SESSION_IDLE_RESET_S
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                          params={"key": "eq.session_reset_s", "select": "value"},
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=5)
            rows = r.json()
            if rows and rows[0].get("value"):
                v = float(rows[0]["value"])
        except Exception:  # noqa: BLE001
            pass
        _reset_cache.update(v=v, t=now)
    return _reset_cache["v"]


_agent_cache = {"mode": None, "cwd": None, "t": 0.0}


def agent_settings() -> dict:
    """Full-agent-mode knobs from app_settings (cached ~15s): `agent_mode` (bool — upgrade the subscription
    path to the unrestricted Claude Code agent) and `agent_cwd` (where that agent runs; default $HOME)."""
    now = time.monotonic()
    if _agent_cache["mode"] is None or now - _agent_cache["t"] > 15:
        mode, cwd = False, os.path.expanduser("~")
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/app_settings",
                          params={"key": "in.(agent_mode,agent_cwd)", "select": "key,value"},
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=5)
            s = {row["key"]: row["value"] for row in r.json()}
            mode = str(s.get("agent_mode", "")).strip().lower() in ("1", "true", "on", "yes")
            if s.get("agent_cwd"):
                cwd = os.path.expanduser(s["agent_cwd"].strip())
        except Exception:  # noqa: BLE001
            pass
        _agent_cache.update(mode=mode, cwd=cwd, t=now)
    return {"mode": _agent_cache["mode"], "cwd": _agent_cache["cwd"]}


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


# --------------------------------------------------------------------------- #
# claude headless invocation
# --------------------------------------------------------------------------- #


def write_mcp_config() -> str:
    env = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
    cfg = {
        "mcpServers": {
            "data": {"command": "node", "args": [DATA_MCP_JS], "env": env},
            "voice": {"command": "node", "args": [VOICE_MCP_JS], "env": env},
            "govee-lights": {"command": GOVEE_PY, "args": [GOVEE_MCP_PY], "env": env},
        }
    }
    fd, path = tempfile.mkstemp(prefix="voice-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def run_claude(transcript: str, resume_session_id: str | None, on_event=None, system_append: str = "",
               model: str = HUB_MODEL, agent: bool = False, cwd: str | None = None) -> dict:
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
        cmd += ["--allowedTools", "mcp__data", "mcp__voice", "mcp__govee-lights"]   # MCP-only; built-ins denied
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

    # Subscription path: `claude -p` must use the logged-in Claude subscription, not an API key. If
    # ANTHROPIC_API_KEY is in the env (it is, so the SDK path can use it), the CLI silently bills the
    # Anthropic API instead — so strip it for this subprocess only. The SDK runner still sees it.
    sub_env = os.environ.copy()
    sub_env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=sub_env, cwd=cwd,  # line-buffered; cwd is where a full agent operates
        )
        # Watchdog: kill the run if it exceeds the timeout, which ends the streaming loop at EOF.
        done = threading.Event()

        def _watchdog():
            if not done.wait(timeout_s):
                timed_out["v"] = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

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
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            etype = ev.get("type")

            if etype == "system":  # init event carries the session id
                if ev.get("session_id"):
                    session_id = ev["session_id"]

            elif etype == "assistant":  # text lives in content[*].text; tool calls in content[*].tool_use
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        assistant_chunks.append(block.get("text", ""))
                    elif block.get("type") == "tool_use" and on_event:
                        try:
                            on_event("tool", {"name": block.get("name", ""), "input": block.get("input")})
                        except Exception:  # noqa: BLE001
                            pass
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
        proc.wait(timeout=5)
        stderr = "".join(err_buf)

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
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
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


# --------------------------------------------------------------------------- #
# (The old warm-Agent-SDK path was removed — it used the claude_code preset, i.e. the full Claude Code
# toolset, and didn't belong to either route. The two strategies are now run_claude + ApiRunner.)


# --------------------------------------------------------------------------- #
# Stripped-down API agent — ONLY the MCP tools, nothing else.
#
# `claude -p` (and the warm Agent SDK on the claude_code preset) hand the model the whole Claude Code
# toolset — Bash, WebSearch, WebFetch, ToolSearch, Write, Cron, ~30 tools — and only DENY the
# non-allowlisted ones at execution time. The model still SEES them and reaches for ToolSearch/Bash,
# which is noise (and invites a tiny model like Haiku to hallucinate). This runner instead calls the
# Anthropic Messages API directly with exactly the data/voice/govee MCP tools as `tools=`, and nothing
# else: no built-ins, no tool discovery, no coding-agent system prompt.
#
# It's a real agentic loop, not one-shot — the model can chain sequential tool calls and always ends
# with a user-facing text reply. MCP servers are started ONCE (warm stdio sessions) and their schemas
# are inlined with the prompt, so a turn is one round-trip (+ tool round-trips), no spin-up per request.
API_MODEL_IDS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-6", "opus": "claude-opus-4-8"}


class ApiRunner:
    def __init__(self):
        self._loop = None
        self._client = None
        self._stack = None        # AsyncExitStack holding the warm MCP stdio sessions
        self._sessions = {}       # server name -> mcp ClientSession
        self._tools = []          # Anthropic tool schemas (only the MCP tools)
        self._dispatch = {}       # anthropic tool name -> (server, real MCP tool name)
        self._messages = []       # conversation history (reset on idle / day rollover)
        self._last = 0.0
        self._day = None

    def _start_loop(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, name="api-loop", daemon=True).start()

    async def _ensure(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        if self._sessions:
            return
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {**os.environ, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
        specs = []
        if os.path.exists(DATA_MCP_JS):  specs.append(("data", "node", [DATA_MCP_JS]))
        if os.path.exists(VOICE_MCP_JS): specs.append(("voice", "node", [VOICE_MCP_JS]))
        if os.path.exists(GOVEE_MCP_PY): specs.append(("govee", GOVEE_PY, [GOVEE_MCP_PY]))
        self._stack = AsyncExitStack()
        for name, cmd, args in specs:
            params = StdioServerParameters(command=cmd, args=args, env=env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[name] = session
            for t in (await session.list_tools()).tools:
                aname = (name + "__" + t.name)[:64]
                self._tools.append({
                    "name": aname,
                    "description": (t.description or "")[:2048],
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                })
                self._dispatch[aname] = (name, t.name)
        log(f"api agent: {len(self._tools)} MCP tools warm ({', '.join(self._sessions)})")

    async def _close(self):
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._stack = None
        self._sessions, self._tools, self._dispatch = {}, [], {}

    def reset(self):
        self._messages = []   # keep MCP warm; just forget the conversation

    async def _call_tool(self, server, real, args):
        try:
            r = await self._sessions[server].call_tool(real, args or {})
            parts = [getattr(c, "text", None) or str(c) for c in (r.content or [])]
            return "\n".join(p for p in parts if p) or "(no output)"
        except Exception as e:  # noqa: BLE001
            return f"tool error: {e}"

    async def _run(self, prompt, system, model, think, on_event, effort="high"):
        await self._ensure()
        self._messages.append({"role": "user", "content": prompt})
        # Cache the stable prefix — the tool schemas (~10k tokens) + system — for 1 hour, so warm hits
        # cost ~10% of input instead of full price every command. The breakpoint sits on the system
        # block; tools precede it in the cache-prefix order (tools → system → messages), so they're
        # cached too. The per-turn directives ride on the USER message (not system), so this prefix
        # stays byte-identical across commands. 1h TTL survives the gaps between voice commands.
        sys_param = ([{"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral", "ttl": "1h"}}] if system else system)
        kwargs = dict(model=API_MODEL_IDS.get(model, model), system=sys_param,
                      tools=self._tools, messages=self._messages,
                      max_tokens=8000 if think else 1024,
                      extra_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"})
        if think:  # adaptive thinking (budget_tokens is rejected on 4.8); effort sets the depth
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": effort}
        final_text = ""
        logged_cache = False
        for _ in range(8):  # agentic loop: chain tool calls until a final text reply
            resp = await self._client.messages.create(**kwargs)
            if not logged_cache:  # verify caching engages: creation on cold, read on warm
                u = getattr(resp, "usage", None)
                if u is not None:
                    log(f"cache: create={getattr(u, 'cache_creation_input_tokens', 0)} "
                        f"read={getattr(u, 'cache_read_input_tokens', 0)} in={getattr(u, 'input_tokens', 0)}")
                logged_cache = True
            self._messages.append({"role": "assistant", "content": resp.content})  # preserves thinking/tool blocks
            tool_results = []
            for b in resp.content:
                if b.type == "text":
                    final_text = b.text
                elif b.type == "tool_use":
                    if on_event:
                        try:
                            on_event("tool", {"name": b.name, "input": b.input})
                        except Exception:  # noqa: BLE001
                            pass
                    server, real = self._dispatch.get(b.name, (None, None))
                    out = await self._call_tool(server, real, b.input) if server else "tool not found"
                    tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            if resp.stop_reason == "tool_use" and tool_results:
                self._messages.append({"role": "user", "content": tool_results})
                kwargs["messages"] = self._messages
                continue
            break
        return {"session_id": None, "text": final_text.strip(), "result": final_text.strip(),
                "ok": bool(final_text.strip()), "raw_error": None}

    def run(self, transcript, system_append="", on_event=None, model="haiku", think=False, effort="high") -> dict:
        self._start_loop()
        now = time.monotonic()
        today = datetime.now().astimezone().date()
        if self._messages and (now - self._last > session_reset_s() or self._day != today):
            self.reset()
        self._last, self._day = now, today
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._run(transcript, system_append, model, think, on_event, effort), self._loop)
            return fut.result(timeout=CLAUDE_TIMEOUT_S + 30)
        except Exception as e:  # noqa: BLE001 — never wedge the worker
            self.reset()
            return {"session_id": None, "text": "", "result": "", "ok": False, "raw_error": f"api run failed: {e}"}


API = ApiRunner()


def chatbox_route(db: Supabase) -> str:
    """Route for the typed chat box ONLY: 'fast' (efficiency), 'power' (accuracy), or 'custom'. Default
    accuracy. The two FAB buttons and the two wake words carry their own tier and ignore this."""
    rows = db.get("app_settings", "key=eq.chatbox_route&select=value")
    if rows and rows[0].get("value") in ("fast", "power", "custom"):
        return rows[0]["value"]
    return "power"


def custom_config(db: Supabase) -> dict:
    """The 'custom' route's knobs (hub Settings → Custom): model (haiku|sonnet|opus), thinking level
    (none|medium|max), and runner (local subscription | api). Sane fallbacks if unset."""
    rows = db.get("app_settings", "key=in.(custom_model,custom_think,custom_runner)&select=key,value")
    s = {r["key"]: r["value"] for r in rows} if isinstance(rows, list) else {}
    model = s.get("custom_model") if s.get("custom_model") in ("haiku", "sonnet", "opus") else "sonnet"
    think = s.get("custom_think") if s.get("custom_think") in ("none", "medium", "max") else "medium"
    runner = s.get("custom_runner") if s.get("custom_runner") in ("local", "api") else "local"
    return {"model": model, "think": think, "runner": runner}


def get_focus(db: Supabase) -> str:
    rows = db.get("voice_focus", "id=eq.1&select=focus")
    if rows and isinstance(rows, list) and rows[0].get("focus"):
        return rows[0]["focus"]
    return "hub"  # safe default


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


def claim_one(db: Supabase) -> dict | None:
    """Find one pending request and atomically claim it. Returns the row or None."""
    if higher_priority_present(db):
        return None

    pending = db.get(
        "voice_requests",
        "status=eq.pending&order=created_at.asc&limit=1&select=id,transcript,source,tier",
    )
    if not pending:
        return None
    req = pending[0]
    rid = req["id"]

    claimed = db.patch(
        "voice_requests",
        f"id=eq.{rid}&status=eq.pending",
        {
            "status": "claimed",
            "claimed_by": WORKER_ID,
            "claimed_at": now_iso(),
            "assigned_to": WORKER_ID,
            "updated_at": now_iso(),
        },
        extra_headers={"Prefer": "return=representation"},
    )
    # Empty representation array => someone else won the race.
    if not claimed:
        return None
    return claimed[0]


def _duplicate_of_recent(db: Supabase, req: dict) -> bool:
    """True if an earlier already-claimed/done request covers the SAME RECORDING WINDOW as this one — i.e.
    a second always-on mic (ESP + Mac) heard the same utterance. Compares the actual [capture_start,
    capture_end] spans (overlap, each padded by DEDUP_TOL_S for clock skew + ESP stream latency), NOT
    enqueue time — so the pair always collapses regardless of transcription speed, while two genuinely
    distinct (sequential, non-overlapping) commands BOTH run even a second apart. Falls back to created_at
    proximity only for a pair where either side predates capture-window stamping."""
    rid = req.get("id")
    s, e = _parse_ts(req.get("capture_start") or ""), _parse_ts(req.get("capture_end") or "")
    ts = _parse_ts(req.get("created_at") or "")
    rows = db.get("voice_requests",
                  "select=id,created_at,status,capture_start,capture_end&order=created_at.desc&limit=12")
    tol = timedelta(seconds=DEDUP_TOL_S)
    for r in rows:
        if r.get("id") == rid or r.get("status") not in ("claimed", "done"):
            continue
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

    # Manual context reset from the hub UI: drop the resumed hub session so the next command starts
    # fresh. The status-hub server already cleared the visible thread and pushed a live reset to
    # every surface — here we just forget the Claude session and ack (no Claude run, no turns).
    if transcript.strip() == RESET_SENTINEL:
        try:
            HUB_SESSION_FILE.unlink(missing_ok=True)
        except Exception as e:
            log(f"reset: unlink hub_session failed: {e}")
        API.reset()   # drop the API agent's conversation
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(context reset)", "updated_at": now_iso()})
        log("hub context reset — session dropped")
        return

    focus = get_focus(db)

    # Focus is a specific Mac session → inject the transcript into that session's
    # wall terminal (the orchestrator owns the live `claude --resume` pty) instead
    # of running a fresh hub `claude -p`. The conversation shows in the xterm panel.
    if focus != "hub":
        log(f"focus={focus!r}: injecting into wall terminal via orchestrator")
        ok, err = inject_to_terminal(transcript)
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            ({"status": "done", "result": "(injected to terminal)", "updated_at": now_iso()}
             if ok else
             {"status": "error", "error": err or "inject failed", "updated_at": now_iso()}),
        )
        return

    # Dedup: multiple always-on listeners (ESP + Mac) hear one utterance, so it lands as several requests
    # a few seconds apart. If a near-simultaneous, near-identical transcript was already handled, drop this
    # one so the command never runs twice. v1 keeps the earlier request; quality-ranked pick comes later.
    if _duplicate_of_recent(db, req):
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(duplicate — dropped)", "updated_at": now_iso()})
        log(f"dropped duplicate request {rid}: {transcript[:60]!r}")
        return

    # (The user turn is written AFTER the model runs — deferred so a wake-word false positive that the
    # model drops never appears in the conversation. See below.)

    # Hub session management.
    state = read_hub_session()
    ag = agent_settings()
    session_agent = ag["mode"] and WORKER_ID != "fly"   # full-agent session → reset manually only
    if should_reset_session(state, agent=session_agent):
        resume_id = None
        log("hub session: starting FRESH (reset rule fired)")
    else:
        resume_id = state["session_id"]
        log(f"hub session: resuming {resume_id}")

    # Surface tool calls live by pushing them straight to the hub's activity bar (transient — not
    # stored in voice_messages, so they never appear in the conversation thread).
    def _emit(kind, payload):
        if kind != "tool":
            return
        name = str(payload.get("name") or "")
        short = name.rsplit("__", 1)[-1] if "__" in name else name
        try:
            args = payload.get("input")
            argstr = json.dumps(args, ensure_ascii=False, separators=(",", ":")) if args else ""
        except Exception:  # noqa: BLE001
            argstr = str(payload.get("input") or "")
        if len(argstr) > 240:
            argstr = argstr[:239] + "…"
        text = (short + (" " + argstr if argstr else "")).strip()
        push_activity(text[:260], "tool")  # direct push → instant activity bar; full width like the rest

    # Per-source prompt directives (ride only on the prompt handed to Claude, never shown in the thread):
    #  · transcribed sources (ESP/Mac/quick-FAB) get false-positive awareness so noise mis-hears drop,
    #  · the quick-add FAB additionally gets the terse fits-in-the-chip reply format.
    source = (req.get("source") or "").strip()
    quick = source == "web-quick"
    audio = source in ("fly", "mac", "web-quick")   # transcribed (mishear-prone); typed text is excluded
    prompt = transcript
    # Give the model the real current date/time (worker-local, i.e. Jared's timezone on Mac/Pi) so any
    # relative date resolves correctly — without it the model defaults to its training era (e.g. logging
    # "yesterday" against 2024). Rides on the prompt, NOT the cached system block, so it can't change the
    # per-command system prefix and break the API's prompt cache.
    _now = datetime.now().astimezone()
    prompt += ("\n\n[Current date and time: " + _now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z") + " ("
               + _now.strftime("%Y-%m-%d") + "). Resolve any relative date/time — 'today', 'yesterday', "
               "'last night', 'this morning', 'last week' — against THIS, never your training data.]")
    if audio:
        prompt += WAKE_NOISE_DIRECTIVE
    if quick:
        prompt += QUICK_REPLY_DIRECTIVE
    if source in ("fly", "mac"):
        prompt += VOICE_REPLY_DIRECTIVE   # one past-tense line; spoken by the Pi audio service + phone push

    # Tier (set by the hub's two FAB buttons): 'fast' = the sparkle button (simple, unambiguous
    # tool routing → Haiku, no thinking); 'power' = the brain button (precision macro estimates →
    # Opus + max thinking via POWER_DIRECTIVE). Anything else (typed card, ESP voice) → HUB_MODEL.
    # Resolve the route. Buttons + wake words set the tier directly; the typed chat box has none, so it
    # follows the chat-box route setting. TWO routes only: fast (efficiency) | power (accuracy).
    tier = (req.get("tier") or "").strip()
    if tier not in ("fast", "power", "custom"):
        tier = chatbox_route(db)
    think = False        # API extended-thinking on/off
    effort = "high"      # API thinking depth when think=True ("medium" | "high")
    runner = ""          # "" = the tier's default device; "local"|"api" only when custom forces it
    if tier == "custom":
        cfg = custom_config(db)
        model, runner = cfg["model"], cfg["runner"]
        if cfg["think"] == "max":           # maximum → ultrathink + high effort
            think = True; effort = "high"
            prompt += POWER_DIRECTIVE        # ultrathink (matters most on the subscription path)
        elif cfg["think"] == "medium":      # medium → a mild think nudge + medium effort
            think = True; effort = "medium"
            prompt += MEDIUM_DIRECTIVE
        # 'none' → think stays False
    elif tier == "power":
        model = "opus"; think = True
        prompt += POWER_DIRECTIVE
    else:
        model = "haiku"
    # Full-agent mode applies only to the subscription path it runs on: power, or custom-local. Never fast
    # (API/Haiku) and never Fly (no subscription / API-only). When on, the run is an unrestricted Claude Code
    # agent in agent_cwd; otherwise the locked-down hub assistant.
    use_agent = ag["mode"] and WORKER_ID != "fly" and (tier == "power" or (tier == "custom" and runner == "local"))
    log(f"route={tier} → model={model} think={think}{(' runner=' + runner) if runner else ''}{' AGENT' if use_agent else ''}")
    ping_esp_led("blink")   # working — the ESP light blinks for EVERY command, not just ESP-sourced ones
    push_activity(transcript[:240], "working")   # bottom-strip: the prompt being worked — fills full width like the result

    # Role framing first, then the live govee device list (when there are lights). Full-agent mode swaps the
    # locked-down hub framing for the unrestricted all-purpose framing.
    sys_ctx = AGENT_SYSTEM if use_agent else HUB_SYSTEM
    govee = govee_context()
    if govee:
        sys_ctx += "\n\n" + govee
    # Two routes, two strategies (see ARCHITECTURE.md):
    #  · EFFICIENCY (fast)  → clean API agent (MCP-only, Haiku), fastest device. On API error, fall back
    #    to the free subscription so Raspy never breaks.
    #  · ACCURACY  (power)  → subscription `claude -p` (Opus + thinking) on Mac → Pi. On Fly there's no
    #    subscription login, so accuracy uses the API there with the same model + thinking.
    if tier == "custom":
        # Custom route honors the chosen runner. 'api' = Anthropic API directly. 'local' = the free
        # subscription (claude -p on Mac/Pi) and it STAYS local — NEVER a silent API fallback (that bills
        # money the user explicitly opted out of). On Fly there's no subscription, so a 'local' command
        # that somehow lands there (only when both Mac AND Pi are down) is refused, not run on the API.
        if runner == "api":
            result = API.run(prompt, system_append=sys_ctx, on_event=_emit, model=model, think=think, effort=effort)
        elif WORKER_ID == "fly":
            log("custom-local reached Fly (Mac+Pi down) — refusing the API; local means local")
            result = {"ok": False, "text": "",
                      "result": "(local route — no subscription worker up; not run on the API)",
                      "raw_error": "no-subscription-worker", "session_id": None}
        else:
            result = run_claude(prompt, resume_id, on_event=_emit, system_append=sys_ctx, model=model,
                                agent=use_agent, cwd=ag["cwd"])
    elif tier == "fast":
        result = API.run(prompt, system_append=sys_ctx, on_event=_emit, model=model, think=False)
        if not result.get("ok"):
            log(f"fast API failed ({result.get('raw_error')}); falling back to subscription")
            result = run_claude(prompt, resume_id, on_event=_emit, system_append=sys_ctx, model=model)
    elif WORKER_ID == "fly":
        result = API.run(prompt, system_append=sys_ctx, on_event=_emit, model=model, think=True)  # Fly: no subscription
    else:
        result = run_claude(prompt, resume_id, on_event=_emit, system_append=sys_ctx, model=model,
                            agent=use_agent, cwd=ag["cwd"])  # subscription, free

    if result.get("session_id"):
        write_hub_session(result["session_id"])
        log(f"captured session_id {result['session_id']}")

    text = (result.get("text") or "").strip()

    # Wake-word false positive: the model flagged the transcript as noise/mishear → drop it silently.
    # No user OR assistant turn is written, so the conversation stays clean; just clear the activity bar.
    if audio and ("__ignore__" in text.lower() or re.sub(r"[^a-z]", "", text.lower()) == "ignore"):
        push_activity("(ignored — likely noise)", "done")
        db.patch("voice_requests", f"id=eq.{rid}",
                 {"status": "done", "result": "(ignored: wake-word false positive)", "updated_at": now_iso()})
        log(f"ignored likely false-positive: {transcript[:60]!r}")
        return

    # Real turn → now write the user bubble (deferred past the noise check), then close the activity bar.
    db.post("voice_messages",
            {"request_id": rid, "focus": "hub", "role": "user", "text": transcript, "done": True})
    push_activity(text[:240], "done")  # close out the activity bar w/ the result (room for the full line)

    if result["ok"]:
        db.post(
            "voice_messages",
            {"request_id": rid, "focus": "hub", "role": "assistant",
             "text": text, "done": True},
        )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "done", "result": text, "updated_at": now_iso()},
        )
        if source in ("fly", "mac") and text:
            notify_phone(text)   # ambient voice command → push the past-tense result to Jared's phone
            db.post("voice_speech", {"text": text})  # speak it aloud via the Pi audio service (warm Kokoro → TV)
        log(f"done request {rid}: {text[:120]!r}")
    else:
        err = result.get("raw_error") or "unknown error"
        # Still surface whatever assistant text we did get, if any.
        if result.get("text"):
            db.post(
                "voice_messages",
                {"request_id": rid, "focus": "hub", "role": "assistant",
                 "text": result["text"], "done": True},
            )
        db.patch(
            "voice_requests",
            f"id=eq.{rid}",
            {"status": "error", "error": err, "updated_at": now_iso()},
        )
        log(f"ERROR request {rid}: {err}")


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
            log(f"realtime listener unavailable ({e}); falling back to {IDLE_POLL_S}s polling")
            return
        self._started = True
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, name="rt-waker", daemon=True).start()
        log("realtime listener starting (event-driven claim on voice_requests INSERT)")

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
                ch.on_postgres_changes("INSERT", callback=self._on_insert,
                                       schema="public", table="voice_requests")
                await ch.subscribe(self._on_sub)
                while _running and self._sock.is_connected:
                    await asyncio.sleep(1.0)
            except Exception as e:  # noqa: BLE001
                log(f"realtime listener error: {e}; retrying in 5s (poll fallback active)")
            try:
                if self._sock:
                    await self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            if _running:
                await asyncio.sleep(5.0)

    def _on_insert(self, _payload) -> None:
        self.event.set()  # a command was enqueued → wake the main loop now

    def _on_sub(self, state, err=None) -> None:
        log(f"realtime subscription: {state}{(' err=' + str(err)) if err else ''}")

    def wait(self, timeout: float) -> bool:
        """Block until an INSERT wakes us (instant) or `timeout` elapses (poll fallback)."""
        fired = self.event.wait(timeout)
        self.event.clear()
        return fired


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

_running = True


def _handle_signal(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    log(f"received signal {signum}; shutting down after current iteration")


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
    same worker and resume the same session. Fires immediately, then every HEARTBEAT_INTERVAL_S."""
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as hb_client:
        hb_db = Supabase(hb_client)
        while not stop_evt.is_set():
            try:
                heartbeat(hb_db)
            except Exception as e:  # noqa: BLE001
                log(f"heartbeat thread error: {e}")
            stop_evt.wait(HEARTBEAT_INTERVAL_S)


def main() -> int:
    if not SUPABASE_KEY:
        print("FATAL: SUPABASE_KEY is required (service_role key). See README.",
              file=sys.stderr)
        return 2

    once = "--once" in sys.argv

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"starting (priority={WORKER_PRIORITY}, state_dir={STATE_DIR}, once={once})")

    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        db = Supabase(client)
        publish_repo_dirs()   # Mac-only: refresh the agent cwd picker's repo list

        if once:
            heartbeat(db)
            processed = one_iteration(db)
            log(f"--once complete (processed={processed})")
            return 0

        waker = RealtimeWaker()
        waker.start()   # event-driven claim; no-op fallback to polling if realtime is unavailable

        # Heartbeat on its own thread so a long command (a multi-minute agent run) never lets this
        # worker's presence go stale — which would let a lower-priority worker poach the user's follow-up
        # onto a different session. The main loop is then free to block on process_request as long as a
        # command takes, and the queued follow-up runs HERE, next, resuming the same conversation.
        hb_stop = threading.Event()
        threading.Thread(target=_heartbeat_loop, args=(hb_stop,), name="heartbeat", daemon=True).start()

        try:
            while _running:
                processed = one_iteration(db)
                # Loop quickly if we just worked (drain the queue); else wait for the next INSERT.
                # waker.wait wakes instantly on a realtime INSERT, else times out after IDLE_POLL_S.
                if not processed:
                    waker.wait(IDLE_POLL_S)
        finally:
            hb_stop.set()

    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
