"""One claimed request: provenance, routing, the prompt, the run, and what is written back."""

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone, timedelta

import httpx

from . import runstate
from .claim import _duplicate_of_recent, claim_one, finish, hold
from .config import CREDITS_API_KEY, FALLBACK_OUTAGE_S, HUB_EFFORT, HUB_MODEL, RESET_SENTINEL, SUPABASE_KEY, SUPABASE_URL, WORKER_ID, _NOTHING_SAID
from .db import Supabase
from .governor import maybe_auto_disable_wakefree
from .live import agent_settings, get_focus, read_session, recent_context, session_file, should_reset_session, write_session
from .logs import _parse_ts, alert_failure, log, log_error, now_iso
from .outbound import inject_to_terminal, ping_esp_led, push_activity
from .presence import claude_unready, hand_off
from .prompts import ADDRESSING_DIRECTIVE, AGENT_SYSTEM, ALT_TRANSCRIPT_DIRECTIVE, ATTACH_DIRECTIVE, DELIBERATE_DIRECTIVE, NO_PRESS_DIRECTIVE, NUTRITION_DIRECTIVE, READ_DIRECTIVE, SHARED_STATE_NOTE, SHARE_DIRECTIVE, TOOLS_DIRECTIVE, is_ignore, read_again, take_read_marker
from .runners import CODEX_TIMEOUT_S, SDK, SDK_CREDITS, codex_ready, limit_refused, run_claude, run_codex, run_subscription, set_cmd_ts


# PROVENANCE: was this command started by a deliberate act of the user's hand? Only such a command may reach
# anything that can do unbounded work in the user's name: a Claude session (start_session, follow_up, a pty
# inject into a focused one), the unrestricted full agent (--dangerously-skip-permissions), Codex, or the
# Console-billed credits runner. Everything a microphone heard by itself (always-on capture, a wake word
# the TV can say, a watch or phone listener) is not, because nothing guarantees the user was the one talking.
#
# A row is manual only when it says so twice: its `source` is one of these labels, each of which is only
# ever written by a press or by typing, AND it is not stamped `always_on`. Anything else, a new capture
# path, a label a client chose, an unstamped row, is treated as not manual: FAIL CLOSED. Adding a label
# here is a claim that every writer of it is a button or a keyboard; check them all first.
#   watch-cmd       the watch's Hub control (status-hub apps/Watch/Hub.swift; `watch` is its listener)
#   watch-fallback  the same press, through Supabase's voice-fallback function when the hub is down
#   web, web-quick  the hub's mic buttons and its typed box (/api/voice-command default, /api/voice-text)
#   share           the iPhone share sheet (/api/voice-text shared:true)
MANUAL_SOURCES = frozenset({"watch-cmd", "watch-fallback", "web", "web-quick", "share"})


def is_manual(req: dict) -> bool:
    """True only for a command started by a press or typed (see MANUAL_SOURCES). Fails closed."""
    return not req.get("always_on") and (req.get("source") or "").strip() in MANUAL_SOURCES


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


def about_directive(db: Supabase, about_id: str) -> str:
    """A command spoken from an answer opened in the inbox (status-hub docs/WATCH.md, "The
    inbox") is about THAT answer, which may be days old and long out of the replayed context, so
    the question and the answer ride on the prompt. A lookup that fails sends the command without them:
    the words alone are still his command, and the error is reported."""
    try:
        [ans] = db.get("voice_messages", f"id=eq.{about_id}&select=text,request_id,session_id,session_label,created_at") or [None]
        if not ans:
            return ""
        ask = None
        if ans.get("request_id"):
            [ask] = db.get("voice_messages", f"role=eq.user&request_id=eq.{ans['request_id']}&select=text&limit=1") or [None]
        who = (f"the session \"{ans.get('session_label') or ans['session_id']}\" ({ans['session_id']})"
               if ans.get("session_id") else "you")
        q = ((ask or {}).get("text") or "").strip()
        return ("\n\n[He said this while looking at an earlier answer, opened on its own from his answers "
                "inbox, so it is about that answer — a follow-up to it, not a new subject. It was written by "
                + who + " at " + str(ans.get("created_at") or "?") + "."
                + (" What he had asked:\n" + q[:4000] + "\n" if q else "")
                + " The answer:\n" + (ans.get("text") or "").strip()[:12000] + "\n]")
    except Exception as e:  # noqa: BLE001
        log_error(f"about_directive: could not read answer {about_id}: {e}")
        return ""


def last_flagged(db: Supabase, mode: str) -> str | None:
    """The newest answer in the inbox that the hub itself wrote (a relayed session reply is the session's,
    never something a hub reply revises)."""
    try:
        [r] = db.get("voice_messages", f"focus=eq.hub&mode=eq.{mode}&role=eq.assistant&needs_read=is.true"
                     "&session_id=is.null&order=created_at.desc&limit=1&select=id") or [None]
        return r and r["id"]
    except Exception as e:  # noqa: BLE001
        log_error(f"last_flagged: {e}")
        return None


def unflag(db: Supabase, mid: str) -> None:
    """Take an answer out of the inbox because a clarified answer to the same question replaces it
    (`[[read again]]`). The row stays in the thread; it only stops being an inbox row."""
    try:
        db.patch("voice_messages", f"id=eq.{mid}", {"needs_read": False})
        log(f"inbox: {mid} replaced by a clarified answer")
    except Exception as e:  # noqa: BLE001
        log_error(f"unflag {mid}: {e}")


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
    # STARTED (not ~30s later at pipeline end), so a voice-logged set lands at the user's rest-clock moment.
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
        finish(db, req, {"status": "done", "result": "(context reset)"})
        log(f"{'agent' if reset_agent else 'hub'} context reset — session dropped")
        return

    # A command that is only "thank you" is never run: from a microphone it is what Whisper writes for a
    # clip with no speech in it, and typed it asks for nothing. The transcribers already drop it (server.py,
    # mac-listener, status-hub's transcribe.js); this catches every other way a row gets here.
    if _NOTHING_SAID.match(transcript):
        finish(db, req, {"status": "error", "error": "nothing said"})
        log(f"ignored {transcript!r}: nothing said")
        return

    # THE HUB DID NOT ANSWER. The watch sent this through the voice-fallback edge function; it is run
    # here on Opus with every tool, says so, and the first one of an outage starts a session to find out
    # why. See fallback_directive.
    fallback = (req.get("source") or "") == "watch-fallback"
    runstate._hub_unreachable = fallback

    # Started by a press or typed? Only then may this command reach a session, the unrestricted agent,
    # Codex or the credits runner. Fails closed: see MANUAL_SOURCES.
    manual = is_manual(req)

    # A focused Mac session receives typed-into-its-terminal words, so only a pressed command goes there;
    # anything a microphone heard by itself is answered as an ordinary hub turn instead.
    focus = "hub" if fallback or not manual else get_focus()

    # Focus is a specific Mac session → inject the transcript into that session's
    # wall terminal (the orchestrator owns the live `claude --resume` pty) instead
    # of running a fresh hub `claude -p`. The conversation shows in the xterm panel.
    if focus != "hub":
        log(f"focus={focus!r}: injecting into wall terminal via orchestrator")
        ok, err = inject_to_terminal(transcript)
        if not ok and hand_off(db, req, err or "inject failed"):
            return   # only the Mac runs the orchestrator; another worker may reach it
        finish(db, req, {"status": "done", "result": "(injected to terminal)"} if ok else
               {"status": "error", "error": err or "inject failed"})
        return

    # Dedup: multiple always-on listeners (ESP + Mac) hear one utterance, so it lands as several requests
    # a few seconds apart. If an overlapping capture was already handled, drop this one so the command
    # never runs twice. Only an always-on capture can be double-heard: a press, a watch command or a
    # typed message comes from one device on purpose, so it skips the check and the read it costs.
    if req.get("always_on") and _duplicate_of_recent(db, req):
        finish(db, req, {"status": "done", "result": "(duplicate — dropped)"})
        log(f"dropped duplicate request {rid}: {transcript[:60]!r}")
        return

    # (The user turn is written AFTER the model runs — deferred so a wake-word false positive that the
    # model drops never appears in the conversation. See below.)

    # Context. Full-agent (coding) mode keeps a real CLI --resume session — its context (open files, long
    # tool chains) can't be reconstructed from replies. The HUB conversation does NOT use --resume: each
    # turn is a FRESH process handed the recent transcript (recent_context), so the two models never share a
    # session and can't cross-contaminate. Only agent mode reads/writes a persisted session id.
    ag = agent_settings()
    # The full agent runs with --dangerously-skip-permissions, so a command a microphone heard by itself
    # never gets it, even with agent mode on: it is answered on the fenced hub client.
    session_agent = ag["mode"] and WORKER_ID != "fly" and manual
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

    _emit = _activity_emitter()
    _hold = lambda for_s=0.0, tool=None: hold(db, req, for_s, tool)  # noqa: E731 — the lease, renewed by this turn

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
    prompt = build_prompt(req, transcript, ambient=ambient, manual=manual)
    if req.get("about_id"):
        prompt += about_directive(db, req["about_id"])

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
    log(f"route={'fallback→opus agent' if fallback else 'agent' if use_agent else hub_route} src={source}"
        f" manual={'y' if manual else 'n'}")
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

    result = _run_turn(db, req, prompt, ctx, manual=manual, fallback=fallback,
                       use_agent=use_agent, resume_id=resume_id, agent_cwd=ag["cwd"],
                       read_dir=att_tmp if att_paths else None, on_event=_emit, hold=_hold)

    if att_tmp:
        shutil.rmtree(att_tmp, ignore_errors=True)   # staged photos served their turn

    if result.get("session_id") and session_agent:   # only full-agent mode keeps a CLI session; hub replays ctx
        write_session(True, result["session_id"])
        log(f"captured agent session_id {result['session_id']}")

    _record_outcome(db, req, result, prompt, transcript=transcript, attachments=attachments,
                    manual=manual, ambient=ambient, fallback=fallback, session_agent=session_agent,
                    on_event=_emit, hold=_hold)


def _activity_emitter():
    """The on_event callback for one turn's runner: narration and tool calls go to the hub's activity bar.
    Transient: none of it is stored in voice_messages. Marking the request acted is the lease's job
    (`claim.hold`), done before the tool runs rather than when it is reported."""

    # Surface tool calls live by pushing them straight to the hub's activity bar (transient — not
    # stored in voice_messages, so they never appear in the conversation thread).

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
        try:
            args = payload.get("input")
            argstr = json.dumps(args, ensure_ascii=False, separators=(",", ":")) if args else ""
        except Exception:  # noqa: BLE001
            argstr = str(payload.get("input") or "")
        if len(argstr) > 240:
            argstr = argstr[:239] + "…"
        text = (short + (" " + argstr if argstr else "")).strip()
        push_activity(text[:260], "tool")  # direct push → instant activity bar; full width like the rest

    return _emit


def build_prompt(req: dict, transcript: str, *, ambient: bool, manual: bool) -> str:
    """The prompt for one command: the user's words, the clock to anchor relative times to, and the directives
    for how it reached us. Rides on the prompt, NOT the cached system block, so it never changes the
    per-command system prefix and breaks the prompt cache."""
    prompt = transcript
    # Give the model a clock so any relative date/time resolves correctly — without it the model defaults
    # to its training era (e.g. logging "yesterday" against 2024). Pure wall-clock, no timezone concept.
    # Priority, BEST ANCHOR FIRST — what we want is the moment the user SPOKE, not the moment we got here:
    #  1. capture_start — when the mic started recording, i.e. when the user started talking. That's what
    #     relative arithmetic has to hang off: "a timer for 11 minutes" means 11 minutes from when the user
    #     said it. Stamped UTC by the capture side, so convert into this machine's local wall clock to
    #     read like the other two branches.
    #  2. client_now — the REQUESTING DEVICE's own clock (phone/browser), for typed commands that carry
    #     no recording window; better than ours because the worker is only "wherever the user is" when the
    #     request came from home.
    #  3. this worker's clock — last resort (and what typed/local commands legitimately land on). For a
    #     voice command it's the wrong anchor: it reads at claim/transcription time, ~25s after the user
    #     started speaking on a HEALTHY run and much later when the Mac worker is down and the job falls
    #     through to the Pi or Fly, so every "in N minutes" would quietly lose that lag.
    # To the SECOND, in both the readable clock and the literal occurred_at value: a set's stamp is the
    # zero of the watch's m:ss rest clock, and a minute-precision stamp let the model pass HH:MM:00,
    # which started that clock up to 59s before the user had finished the set.
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
        prompt += ADDRESSING_DIRECTIVE
    else:
        prompt += DELIBERATE_DIRECTIVE
    if not manual:
        prompt += NO_PRESS_DIRECTIVE
    prompt += READ_DIRECTIVE
    alt = (req.get("alt_transcript") or "").strip()
    if alt:
        prompt += ALT_TRANSCRIPT_DIRECTIVE.format(alt=alt)
    return prompt


def _run_turn(db: Supabase, req: dict, prompt: str, ctx: str, *, manual: bool, fallback: bool,
              use_agent: bool, resume_id, agent_cwd: str, read_dir, on_event, hold) -> dict:
    """Run the turn on the route its provenance and mode allow, and return the runner's result."""
    # Dispatch — one route, the same on every worker (Pi, Mac, and Fly, which holds its own subscription
    # token). Everything runs on the Claude SUBSCRIPTION; there is no paid path. A run that fails
    # transiently is retried (`run_subscription`), and one that still fails surfaces as an error. `ctx`
    # (the replayed hub conversation) rides on a FRESH conversation; there is no --resume for hub turns.
    # agent → the unrestricted coding agent (its own --resume session). Everything else, photos included,
    # → the warm SDK client. A failure that called no tool is handed to another worker below rather than
    # ended here.
    hub_prompt = ctx + prompt   # ctx is "" in agent mode (uses --resume) or when there's no live conversation
    unready = claude_unready()
    if unready:
        result = {"session_id": None, "text": "", "result": "", "ok": False, "raw_error": unready}
    elif fallback and manual:
        # Opus, the unrestricted agent, a FRESH run with the day's conversation replayed — whatever broke,
        # it can handle the command and see enough to say what is wrong. Never the --resume agent session.
        fb_prompt = (ctx or recent_context()) + prompt + fallback_directive(db, req["id"])
        result = run_subscription(
            lambda: run_claude(fb_prompt, None, manual=manual, on_event=on_event, hold=hold,
                               system_append=AGENT_SYSTEM + "\n\n" + SHARED_STATE_NOTE,
                               model="opus", cwd=os.path.expanduser("~")),
            "subscription Opus (fallback)")
    elif use_agent:
        result = run_claude(prompt, resume_id, manual=manual, on_event=on_event, hold=hold,
                            system_append=AGENT_SYSTEM + "\n\n" + SHARED_STATE_NOTE, model="opus",
                            cwd=agent_cwd)
    else:
        # The warm SDK client, not a per-command `claude -p`: that spawn (CLI + four stdio MCP servers)
        # measured 3.6s on the Pi before the model saw a token; the warm client pays it once.
        run_hub = lambda sdk: sdk.run(hub_prompt + NUTRITION_DIRECTIVE + TOOLS_DIRECTIVE, on_event=on_event,  # noqa: E731
                                      manual=manual, read_dir=read_dir, hold=hold)
        result = run_subscription(lambda: run_hub(SDK), "subscription hub")
        if result.get("ok"):
            SDK_CREDITS.close()   # the subscription answers again; the credits client has no more work
        elif manual and limit_refused(result) and CREDITS_API_KEY:
            # Every worker shares the one account, so handing this on would only fail again elsewhere.
            log(f"subscription limit hit ({result.get('raw_error')}) — running on API credits")
            push_activity("On API credits")
            result = run_hub(SDK_CREDITS)
            result["_limit"] = True
            result["_engine"] = "credits"
        if not result.get("ok") and not result.get("used_tools") and manual and codex_ready() and hold(CODEX_TIMEOUT_S):
            log_error(f"Claude could not answer ({result.get('raw_error')}) — running the turn on Codex")
            push_activity("On Codex")
            result = {**run_codex(hub_prompt + NUTRITION_DIRECTIVE + TOOLS_DIRECTIVE, manual=manual), "_limit": result.get("_limit"),
                      "_engine": "codex"}
            if not result.get("ok"):
                log_error(f"codex fallback failed: {result.get('raw_error')}")
    return result


def _record_outcome(db: Supabase, req: dict, result: dict, prompt: str, *, transcript: str, attachments: list,
                    manual: bool, ambient: bool, fallback: bool, session_agent: bool, on_event, hold) -> None:
    """Write the turn back: the thread rows, the request's final state, or a hand-off to another worker.
    Nothing here is written by a worker whose claim was taken back (`finish`)."""
    rid = req["id"]
    if req.get("_lost"):
        log(f"request {rid}: claim was taken back mid-turn; its outcome is the other worker's to write")
        return
    text = (result.get("text") or "").strip()

    # The same net for __IGNORE__ on a turn the user MEANT. Only room-mic turns are taught the sentinel, but a
    # model can still pick it up (it did, from replayed history) — and on a deliberate turn there is no
    # ignore branch to catch it, so it was saved and pushed to the user as the reply. Re-run clean, with no
    # context to mimic; the prompt already carries DELIBERATE_DIRECTIVE.
    if not ambient and is_ignore(text) and not result.get("used_tools"):
        log("deliberate turn answered __IGNORE__ → clean re-run (no context)")
        result = SDK.run(prompt + NUTRITION_DIRECTIVE + TOOLS_DIRECTIVE, on_event=on_event, manual=manual, hold=hold)
        text = (result.get("text") or "").strip()

    again = read_again(text)
    text, needs_read, read_label = take_read_marker(text)
    # A command spoken from an answer opened in the inbox lists under THAT answer
    # (`/api/voice-inbox/:id` follow-ups), so it is never an inbox row of its own: one question, one row.
    if req.get("about_id"):
        needs_read = False

    # A failure here is this machine's, not the command's, until every machine has tried it: hand it on.
    # Nothing has been written to the thread yet, so the worker that takes it writes the whole turn.
    if (not result.get("ok") and not fallback and not req.get("_acted") and not result.get("_limit")
            and not limit_refused(result) and hand_off(db, req, result.get("raw_error") or "unknown error")):
        return

    # Tag every row with the mode so the hub UI can show this mode's thread separately and switch live.
    # Defined up here because the ignore branch below writes a row too.
    msg_mode = "agent" if session_agent else "hub"

    # Wake-word false positive: the model judged the transcript noise or a mishear. No ASSISTANT turn is
    # written — nothing was done and nothing is spoken — but the words are KEPT, flagged `ignored`.
    #
    # They used to be erased entirely, and that made a wrong judgement unrecoverable: the user could not
    # say "do the one you ignored", and a following "answer my question" pointed at something that, as
    # far as the model could see, had never been said. Which is precisely what happened on 2026-08-28.
    if ambient and is_ignore(text):
        if finish(db, req, {"status": "done", "result": "(ignored: wake-word false positive)"},
                  [{"focus": "hub", "mode": msg_mode, "role": "user", "text": transcript, "done": True,
                    "ignored": True, "worker": WORKER_ID}]):
            push_activity("(ignored — likely noise)", "done")
            log(f"ignored likely false-positive (kept in thread): {transcript[:60]!r}")
            maybe_auto_disable_wakefree()
        return

    # Real turn → the user bubble (deferred past the noise check) and the reply, written with the request's
    # final state in one transaction.
    user = {"focus": "hub", "mode": msg_mode, "role": "user", "text": transcript, "done": True,
            **({"attachments": attachments} if attachments else {})}
    if result["ok"]:
        reply = {"focus": "hub", "mode": msg_mode, "role": "assistant", "text": text, "done": True,
                 "worker": WORKER_ID, "needs_read": needs_read,
                 **({"label": read_label} if needs_read and read_label else {}),
                 # Which engine wrote it, when not the subscription: the surfaces colour the reply by it
                 # (credits amber, Codex blue), so a fallback is visible on the reply itself.
                 **({"engine": result["_engine"]} if result.get("_engine") else {})}
        # The last flagged answer, read BEFORE this turn's rows land, so it cannot be this one.
        replaced = last_flagged(db, msg_mode) if needs_read and again else None
        if finish(db, req, {"status": "done", "result": text}, [user, reply]):
            if replaced:
                unflag(db, replaced)
            push_activity(text[:240], "done")  # close out the activity bar w/ the result
            log(f"done request {rid}: {text[:120]!r}")
        return
    err = result.get("raw_error") or "unknown error"
    # Still surface whatever assistant text we did get, if any.
    partial = ([{"focus": "hub", "mode": msg_mode, "role": "assistant", "done": True, "worker": WORKER_ID,
                 "text": take_read_marker(result["text"])[0]}] if result.get("text") else [])
    if finish(db, req, {"status": "error", "error": err}, [user, *partial]):
        push_activity(text[:240], "done")
        log(f"ERROR request {rid}: {err}")
        alert_failure("request", err, f"Request {rid} ({transcript[:200]!r}) ENDED AS AN ERROR here: "
                      + ("it had already acted, so it was not handed on." if req.get("_acted") else
                         f"no healthy worker was left untried (failed_on {req.get('failed_on') or []})."))


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
            finish(db, req, {"status": "error", "error": str(e)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        runstate._lease_deadline = 0.0   # nothing in flight: the heartbeat cannot call this worker stalled
        # /led-off must fire for EVERY command — the blink (ping_esp_led "blink") is universal, so the
        # off has to be too. Gating this to source=='fly' meant a Mac/web command blinked the ESP and
        # NEVER cleared it (e.g. a wake-word false positive: claimed → blink → dropped as noise →
        # stuck blinking forever). The ESP just ignores the ping when there's no light to clear.
        ping_esp_led("off")   # model done (or dropped/errored) → clear the ESP working-light
    return True
