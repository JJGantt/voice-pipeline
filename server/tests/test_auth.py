"""Token auth on the voice server: every control route and the device socket.

Tokens are read from headers only (X-Token / Authorization: Bearer for the HTTP routes, X-Device-Token /
Authorization for the socket), never from the query string, and the server will not start without each
token configured, so nothing is open by default.
"""

import asyncio
import os
import sys
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "shared"))   # endpointing.py, which assemble.sh copies in for Fly
os.environ.update({"GROQ_API_KEY": "x", "LED_TOKEN": "led-secret", "VERIFY_TOKEN": "verify-secret",
                   "VOICE_DEVICE_TOKEN": "device-secret", "SUPABASE_SERVICE_KEY": "k",
                   "SUPABASE_URL": "http://supabase.test", "OPS_TOKEN": "", "OPS_URLS": "http://127.0.0.1:9"})
import server  # noqa: E402


class Req:
    def __init__(self, path, headers=None):
        self.path, self.headers = path, headers or {}


class Conn:
    def respond(self, status, body):
        return (int(status), body)


def call(path, headers=None):
    return asyncio.run(server.process_request(Conn(), Req(path, headers)))


@pytest.mark.parametrize("path", ["/led-off", "/capture-on", "/wakefree-off", "/enroll-status"])
def test_control_routes_take_the_led_token_from_a_header_only(path):
    assert call(path, {"X-Token": "led-secret"})[0] == 200
    assert call(path, {"Authorization": "Bearer led-secret"})[0] == 200
    assert call(path)[0] == 403
    assert call(path, {"X-Token": "wrong"})[0] == 403
    assert call(path + "?token=led-secret")[0] == 403          # the URL is never read


def test_verify_takes_its_own_token_and_never_the_led_one():
    assert call("/verify")[0] == 403
    assert call("/verify?p=x&token=verify-secret")[0] == 403
    assert call("/verify", {"X-Token": "led-secret"})[0] == 403
    assert call("/verify", {"X-Token": "verify-secret"}) == (400, "p required\n")   # past the gate


class WS:
    def __init__(self, path="/ws/audio", headers=None):
        self.request = Req(path, headers)
        self.remote_address = ("1.2.3.4", 1)
        self.closed = None

    async def close(self, code=None, reason=None):
        self.closed = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.parametrize("ws", [WS(), WS("/ws/audio?token=device-secret"), WS(headers={"X-Device-Token": "nope"})])
def test_device_socket_without_its_token_in_a_header_is_closed(ws):
    asyncio.run(server.connection(ws))
    assert ws.closed == 1008


def test_device_token_header_is_accepted():
    assert server._client_token(WS(headers={"X-Device-Token": "device-secret"})) == "device-secret"
    assert server._client_token(WS(headers={"Authorization": "Bearer device-secret"})) == "device-secret"
    assert server._client_token(WS("/ws/audio?token=device-secret")) is None


@pytest.mark.parametrize("name", ["LED_TOKEN", "VERIFY_TOKEN", "VOICE_DEVICE_TOKEN"])
def test_server_refuses_to_start_without_each_token(monkeypatch, name):
    monkeypatch.setattr(server, name, "")
    with pytest.raises(SystemExit):
        asyncio.run(server.main())
