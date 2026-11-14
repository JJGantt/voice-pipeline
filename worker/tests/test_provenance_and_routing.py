"""The press-provenance gate and the routing it decides.

Rule: anything that reaches the worker without a manual press cannot start or message a Claude session,
cannot send a text or an email (or switch the JaredBot auto-replier on), and never reaches Codex or the
credits runner. Every turn runs on the fenced SDK client; nothing spawns an unrestricted `claude -p`.
Manual = a source only a press or a keyboard writes, AND not always_on. Everything else fails closed.
"""

import time

import pytest

from fakes import ago

# (source, always_on, manual?) for every capture path that writes voice_requests, plus the unknowns.
CASES = [
    ("watch-cmd", False, True),       # the watch's Hub control
    ("watch-fallback", False, True),  # the same press through Supabase's voice-fallback function
    ("web", False, True),             # hub mic button / typed box
    ("web-quick", False, True),       # hub navbar mic
    ("share", False, True),           # iPhone share sheet
    ("web", None, True),              # typed rows carry no always_on at all
    ("watch", True, False),           # the watch's always-on listener
    ("phone", True, False),           # the phone's listener
    ("fly", True, False),             # ESP wake-free capture
    ("fly", False, False),            # ESP wake word: the TV can say it
    ("mac", True, False),             # Mac listener / wake-free
    ("mac", False, False),
    ("web", True, False),             # a press label stamped always-on is not trusted
    ("ipad", False, False),           # a label a client invented
    ("", False, False),               # no provenance at all
    (None, None, False),
]


@pytest.mark.parametrize("source,always_on,manual", CASES)
def test_is_manual_fails_closed(load, source, always_on, manual):
    h = load()
    req = {"source": source} if always_on is None else {"source": source, "always_on": always_on}
    assert h.w.is_manual(req) is manual


def claimed(h, source, always_on, transcript="start a session to fix the build", **kw):
    row = dict(transcript=transcript, source=source, always_on=always_on, status="claimed",
               claimed_by=h.w.WORKER_ID, claim_epoch=h.w.WORKER_EPOCH, created_at=ago(3), updated_at=ago(3), failed_on=[])
    row.update(kw)
    return dict(h.pg.insert("voice_requests", **row))


@pytest.mark.parametrize("source,always_on,manual", [c for c in CASES if c[0]])
def test_every_turn_runs_on_the_fenced_client_with_its_provenance(load, source, always_on, manual):
    h = load(WORKER_ID="pi")
    sdk = h.install_runners()
    h.w.process_request(h.db, claimed(h, source, always_on))
    assert h.claude_calls() == []
    assert len(sdk.calls) == 1 and sdk.calls[0]["manual"] is manual
    assert (h.w.NO_PRESS_DIRECTIVE in sdk.calls[0]["prompt"]) is not manual


SESSION_CALLS = [("mcp__data__start_session", {"prompt": "x"}), ("mcp__data__follow_up", {"prompt": "x"}),
                 ("mcp__data__resume_session", {"session": "s-1"}), ("mcp__data__transfer_session", {"session": "s-1"})]
SEND_CALLS = [("mcp__messages__send_message", {"to": "Mom", "text": "x"}),
              ("mcp__gmail__send_email", {"to": "a@b.c", "body": "x"}),
              ("mcp__voice__set_jaredbot", {"on": True})]


@pytest.mark.parametrize("source,always_on,manual", [c for c in CASES if c[0] and c[0] != "watch-fallback"])
def test_the_fence_refuses_sessions_and_sends_unless_pressed(load, source, always_on, manual):
    h = load(WORKER_ID="fly")
    sdk = h.install_runners(h.runner(tools=SESSION_CALLS + SEND_CALLS + [("mcp__data__get_lists", {})]))
    h.w.process_request(h.db, claimed(h, source, always_on))
    decisions = sdk.calls[0]["decisions"]
    for name, _ in SESSION_CALLS + SEND_CALLS:
        assert (decisions[name] != "deny") is manual, name
    assert decisions["mcp__data__get_lists"] == "pass"   # ordinary tools are untouched


def test_runner_defaults_are_the_restrictive_ones(load):
    h = load()
    r = h.w.SdkRunner()
    for name, args in SESSION_CALLS + SEND_CALLS:
        assert r.gate(name, args)[0] == "deny", name


def test_silencing_jaredbot_stays_open_to_any_mic(load):
    r = load().w.SdkRunner()
    assert r.gate("mcp__voice__set_jaredbot", {"on": False})[0] == "pass"
    assert r.gate("mcp__voice__set_jaredbot", {"on": True})[0] == "deny"


def test_codex_refuses_without_a_press_even_if_called_directly(load):
    assert load().w.run_codex("rm -rf ~", manual=False)["ok"] is False


def test_watch_fallback_runs_on_the_fenced_client_with_the_troubleshooting_directive(load):
    h = load(WORKER_ID="pi")
    sdk = h.install_runners(h.runner(tools=[("mcp__data__start_session", {"prompt": "x"})]))
    h.w.process_request(h.db, claimed(h, "watch-fallback", False, transcript="log a banana"))
    assert h.claude_calls() == []
    call = sdk.calls[0]
    assert call["manual"] is True and "FALLBACK:" in call["prompt"] and "start ONE session" in call["prompt"]
    assert call["decisions"]["mcp__data__start_session"] != "deny"   # it may open the troubleshooting session


@pytest.mark.parametrize("source,always_on,injected", [("watch-cmd", False, True), ("fly", False, False),
                                                       ("phone", True, False)])
def test_a_focused_session_receives_only_pressed_commands(load, source, always_on, injected):
    h = load(WORKER_ID="fly")
    h.pg.tables["voice_focus"] = [{"id": 1, "focus": "some-mac-session"}]
    sdk = h.install_runners()
    h.w.process_request(h.db, claimed(h, source, always_on, transcript="yes go ahead"))
    inj = [e for e in h.pg.external if e["url"].startswith("http://orchestrator.test/inject")]
    if injected:
        assert len(inj) == 1 and sdk.calls == []
        assert inj[0]["headers"].get("x-token") == "orch-token" and "token=" not in inj[0]["url"]
    else:
        assert inj == [] and len(sdk.calls) == 1


@pytest.mark.parametrize("source,always_on,credits", [("watch-cmd", False, True), ("fly", False, False),
                                                      ("watch", True, False)])
def test_usage_limit_moves_only_pressed_commands_onto_credits(load, source, always_on, credits):
    h = load(WORKER_ID="fly", ANTHROPIC_API_KEY="console-key")
    # The key is taken out of the environment at import: the CLI and the SDK prefer it over the
    # subscription, so anything spawned that inherited it would bill the API silently.
    assert "ANTHROPIC_API_KEY" not in __import__("os").environ and h.w.CREDITS_API_KEY == "console-key"
    sdk = h.runner(ok=False)
    sdk.limit_hit = True
    h.install_runners(sdk)
    h.w.process_request(h.db, claimed(h, source, always_on, transcript="log a banana"))
    assert (len(h.credits.calls) == 1) is credits
    if credits:
        assert h.credits.calls[0]["manual"] is True


@pytest.mark.parametrize("source,always_on,key,on_fast", [("watch-cmd", False, "console-key", True),
                                                        ("watch-cmd", False, "", False),
                                                        ("watch", True, "console-key", False)])
def test_the_fast_route_runs_only_pressed_commands_on_the_fast_client(load, source, always_on, key, on_fast):
    h = load(WORKER_ID="fly", ANTHROPIC_API_KEY=key)
    sdk = h.install_runners()
    h.w.process_request(h.db, claimed(h, source, always_on, transcript="log a banana", route="fast"))
    assert len(h.fast.calls) == (1 if on_fast else 0)
    assert len(sdk.calls) == (0 if on_fast else 1)
    assert h.fast.credits and h.fast.fast   # fast mode is only ever billed to the key


@pytest.mark.parametrize("tools,falls_back", [((), True), ((("mcp__data__add_to_list", {"text": "x"}),), False)])
def test_a_failed_fast_turn_moves_to_the_standard_route_only_if_it_did_nothing(load, tools, falls_back):
    h = load(WORKER_ID="fly", ANTHROPIC_API_KEY="console-key")
    sdk = h.install_runners(fast=h.runner(fast=True, ok=False, tools=tools))
    h.w.process_request(h.db, claimed(h, "watch-cmd", False, transcript="add x", route="fast"))
    assert len(sdk.calls) == (1 if falls_back else 0)


def test_a_request_without_the_fast_route_never_touches_the_fast_client(load):
    h = load(WORKER_ID="fly", ANTHROPIC_API_KEY="console-key")
    sdk = h.install_runners()
    h.w.process_request(h.db, claimed(h, "watch-cmd", False, transcript="log a banana"))
    assert h.fast.calls == [] and len(sdk.calls) == 1


def test_machine_clients_send_tokens_in_headers_never_the_url(load):
    h = load(LED_BASE_URL="http://led.test", LED_TOKEN="led-token", HUB_TOKEN="hub-token",
             HUB_URL="http://hub.test")
    h.w.push_activity("hello")
    h.w.ping_esp_led("off")
    for _ in range(50):   # the LED ping is fire-and-forget on a thread
        if any(e["url"].startswith("http://led.test") for e in h.pg.external):
            break
        time.sleep(0.02)
    hub = next(e for e in h.pg.external if e["url"].startswith("http://hub.test"))
    led = next(e for e in h.pg.external if e["url"].startswith("http://led.test"))
    assert hub["headers"].get("x-hub-token") == "hub-token" and "token" not in hub["url"]
    assert led["headers"].get("x-token") == "led-token" and "token" not in led["url"]
