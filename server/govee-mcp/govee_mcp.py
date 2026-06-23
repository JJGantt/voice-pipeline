#!/usr/bin/env python3
"""MCP server for controlling Govee smart lights.

Transport is LAN-first with cloud fallback:
  * Each command first discovers the target lamp on the local network via the
    Govee LAN multicast scan. If the lamp answers, the command goes straight to
    it over UDP (~10ms, no rate limits, immune to Govee cloud outages).
  * If the lamp does not answer the scan (LAN control off, lamp off-network, or
    we're away from home), the command falls back to the Govee cloud REST API.

LAN protocol: scan request -> 239.255.255.250:4001, responses arrive on :4002,
device commands/status -> <device_ip>:4003.
"""

import asyncio
import json
import os
import socket
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env-govee")

API_KEY = os.environ["GOVEE_API_KEY"]
BASE_URL = "https://openapi.api.govee.com/router/api/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Govee-API-Key": API_KEY,
}

# LAN protocol constants
LAN_MCAST = ("239.255.255.250", 4001)
LAN_RECV_PORT = 4002
LAN_CMD_PORT = 4003
LAN_SCAN_TIMEOUT = 1.2
LAN_CACHE_TTL = 30.0  # seconds to trust a discovered device IP before re-scanning

# Lights come from Supabase govee_devices (auto-discovered + synced from the Govee cloud API). Falls
# back to manual GOVEE_LIGHT_<NAME>=<sku>,<device_id>,<display_name> env vars if Supabase is unset/down.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://felyggqjjhltwokdfhop.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def _load_lights() -> dict:
    lights = {}
    if SUPABASE_KEY:
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/govee_devices",
                          params={"select": "key,device_id,sku,display_name", "hidden": "eq.false"},
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10)
            for row in r.json():
                lights[row["key"]] = {"sku": row["sku"], "device": row["device_id"], "name": row["display_name"]}
        except Exception:  # noqa: BLE001 — fall through to env config
            pass
    if not lights:
        for key, val in os.environ.items():
            if key.startswith("GOVEE_LIGHT_"):
                try:
                    sku, device, display = val.split(",", 2)
                    lights[key[len("GOVEE_LIGHT_"):].lower()] = {
                        "sku": sku.strip(), "device": device.strip(), "name": display.strip()}
                except ValueError:
                    pass
    return lights


LIGHTS = _load_lights()

NAMED_COLORS = {
    "red": (255, 0, 0),
    "green": (0, 200, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "warm white": (255, 200, 120),
    "yellow": (255, 220, 0),
    "orange": (255, 100, 0),
    "purple": (140, 0, 255),
    "pink": (255, 60, 180),
    "cyan": (0, 220, 255),
    "magenta": (255, 0, 200),
    "teal": (0, 180, 150),
    "lavender": (160, 120, 255),
    "coral": (255, 80, 60),
    "lime": (120, 255, 0),
    "gold": (255, 180, 0),
    "indigo": (75, 0, 200),
}


def rgb_to_int(r: int, g: int, b: int) -> int:
    return (r << 16) | (g << 8) | b


def int_to_rgb(value: int) -> tuple:
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def parse_color(color_str: str) -> int:
    s = color_str.strip().lower()
    if s in NAMED_COLORS:
        return rgb_to_int(*NAMED_COLORS[s])
    if s.startswith("#"):
        h = s[1:]
        return rgb_to_int(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    parts = s.split(",")
    if len(parts) == 3:
        return rgb_to_int(*[int(p.strip()) for p in parts])
    raise ValueError(
        f"Unknown color '{color_str}'. Use a name (blue, red, purple…), hex (#FF0000), or r,g,b values."
    )


# ---------------------------------------------------------------------------
# LAN transport
# ---------------------------------------------------------------------------

_lan_cache: dict = {}  # device_id -> (ip, timestamp)


def _open_recv_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("0.0.0.0", LAN_RECV_PORT))
    return s


def _lan_scan_blocking(timeout: float = LAN_SCAN_TIMEOUT, want: str | None = None) -> dict:
    """Multicast scan for LAN-enabled Govee devices. Returns {device_id: ip}. Early-exits the instant
    `want` (a specific device_id) answers, so resolving one known light is ~one round-trip (~0.2s)
    instead of waiting out the whole timeout window every command."""
    found = {}
    recv = None
    send = None
    try:
        recv = _open_recv_socket()
        recv.settimeout(timeout)
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        send.sendto(
            json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode(),
            LAN_MCAST,
        )
        deadline = time.time() + timeout + 0.3
        while time.time() < deadline:
            try:
                data, addr = recv.recvfrom(2048)
            except socket.timeout:
                break
            try:
                d = json.loads(data.decode()).get("msg", {}).get("data", {})
            except (ValueError, UnicodeDecodeError):
                continue
            dev = d.get("device")
            if dev:
                found[dev] = d.get("ip") or addr[0]
                if want and want in found:
                    break       # got the light we were after — stop waiting on the timeout
    finally:
        if recv is not None:
            recv.close()
        if send is not None:
            send.close()
    return found


def _resolve_lan_ip_blocking(device_id: str) -> str | None:
    """Return the current LAN IP for a device, or None if it isn't reachable."""
    cached = _lan_cache.get(device_id)
    if cached and (time.time() - cached[1]) < LAN_CACHE_TTL:
        return cached[0]
    now = time.time()
    for dev, ip in _lan_scan_blocking(want=device_id).items():
        _lan_cache[dev] = (ip, now)
    cached = _lan_cache.get(device_id)
    if cached and (time.time() - cached[1]) < LAN_CACHE_TTL:
        return cached[0]
    return None


def _lan_send_blocking(ip: str, msg: dict) -> None:
    """Fire a single LAN command packet (fire-and-forget UDP)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), (ip, LAN_CMD_PORT))
    finally:
        s.close()


def _lan_status_blocking(ip: str, timeout: float = 1.0) -> dict | None:
    """Query a lamp's live state over LAN. Returns the devStatus data dict or None."""
    recv = None
    send = None
    try:
        recv = _open_recv_socket()
        recv.settimeout(timeout)
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.sendto(json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode(), (ip, LAN_CMD_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = recv.recvfrom(2048)
            except socket.timeout:
                return None
            try:
                m = json.loads(data.decode()).get("msg", {})
            except (ValueError, UnicodeDecodeError):
                continue
            if m.get("cmd") == "devStatus":
                return m.get("data", {})
    finally:
        if recv is not None:
            recv.close()
        if send is not None:
            send.close()
    return None


# ---------------------------------------------------------------------------
# Cloud transport
# ---------------------------------------------------------------------------

async def _cloud_control(sku: str, device: str, cap_type: str, instance: str, value) -> dict:
    payload = {
        "requestId": str(uuid.uuid4()),
        "payload": {
            "sku": sku,
            "device": device,
            "capability": {"type": cap_type, "instance": instance, "value": value},
        },
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE_URL}/device/control", json=payload, headers=HEADERS)
        return r.json()


async def _cloud_state(sku: str, device: str) -> dict:
    payload = {
        "requestId": str(uuid.uuid4()),
        "payload": {"sku": sku, "device": device},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE_URL}/device/state", json=payload, headers=HEADERS)
        data = r.json()
        return {c["instance"]: c["state"]["value"] for c in data["payload"]["capabilities"]}


# ---------------------------------------------------------------------------
# Unified send: LAN-first, cloud fallback
# ---------------------------------------------------------------------------

async def _send(info: dict, lan_msg: dict, cloud_cap: tuple, success_label: str) -> str:
    """Try LAN first; fall back to the cloud capability if the lamp isn't on the LAN.

    cloud_cap is (cap_type, instance, value).
    """
    ip = await asyncio.to_thread(_resolve_lan_ip_blocking, info["device"])
    if ip:
        await asyncio.to_thread(_lan_send_blocking, ip, lan_msg)
        return f"{success_label} (via LAN)."
    result = await _cloud_control(info["sku"], info["device"], *cloud_cap)
    if result.get("code") == 200:
        return f"{success_label} (via cloud)."
    return f"Failed (LAN unreachable, cloud error): {result}"


mcp = FastMCP("govee-lights")


@mcp.tool()
async def list_lights() -> str:
    """List all configured lights and their current state (online, power, brightness).

    State is read over LAN when the lamp is reachable locally, otherwise via the cloud.
    """
    reachable = await asyncio.to_thread(_lan_scan_blocking)
    now = time.time()
    for dev, ip in reachable.items():
        _lan_cache[dev] = (ip, now)

    lines = []
    for name, info in LIGHTS.items():
        ip = reachable.get(info["device"])
        if ip:
            status = await asyncio.to_thread(_lan_status_blocking, ip)
            if status is not None:
                power = "on" if status.get("onOff") == 1 else "off"
                brightness = status.get("brightness", "?")
                lines.append(
                    f"{name} ({info['name']}): online=True, power={power}, "
                    f"brightness={brightness}% [LAN {ip}]"
                )
                continue
        # Cloud fallback for state
        try:
            state = await _cloud_state(info["sku"], info["device"])
            online = state.get("online", False)
            power = "on" if state.get("powerSwitch") == 1 else "off"
            brightness = state.get("brightness", "?")
            lines.append(
                f"{name} ({info['name']}): online={online}, power={power}, "
                f"brightness={brightness}% [cloud]"
            )
        except Exception as e:
            lines.append(f"{name} ({info['name']}): state unavailable ({e})")
    return "\n".join(lines)


@mcp.tool()
async def set_power(light: str, state: str) -> str:
    """Turn a light on or off.

    Args:
        light: Light name (e.g. 'floor_lamp')
        state: 'on' or 'off'
    """
    if light not in LIGHTS:
        return f"Unknown light '{light}'. Available: {list(LIGHTS.keys())}"
    info = LIGHTS[light]
    value = 1 if state.lower() in ("on", "1", "true") else 0
    return await _send(
        info,
        {"msg": {"cmd": "turn", "data": {"value": value}}},
        ("devices.capabilities.on_off", "powerSwitch", value),
        f"Turned {info['name']} {state}",
    )


@mcp.tool()
async def set_brightness(light: str, brightness: int) -> str:
    """Set brightness of a light.

    Args:
        light: Light name (e.g. 'floor_lamp')
        brightness: 1–100
    """
    if light not in LIGHTS:
        return f"Unknown light '{light}'. Available: {list(LIGHTS.keys())}"
    info = LIGHTS[light]
    brightness = max(1, min(100, brightness))
    return await _send(
        info,
        {"msg": {"cmd": "brightness", "data": {"value": brightness}}},
        ("devices.capabilities.range", "brightness", brightness),
        f"Set {info['name']} brightness to {brightness}%",
    )


@mcp.tool()
async def set_color(light: str, color: str) -> str:
    """Set the color of a light.

    Args:
        light: Light name (e.g. 'floor_lamp')
        color: Color name (red, blue, green, purple, orange, pink, cyan, white, warm white,
               yellow, teal, lavender, coral, lime, gold, indigo, magenta),
               hex string (#FF0000), or r,g,b values (255,0,0)
    """
    if light not in LIGHTS:
        return f"Unknown light '{light}'. Available: {list(LIGHTS.keys())}"
    info = LIGHTS[light]
    try:
        color_int = parse_color(color)
    except ValueError as e:
        return str(e)
    r, g, b = int_to_rgb(color_int)
    return await _send(
        info,
        {"msg": {"cmd": "colorwc", "data": {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}}},
        ("devices.capabilities.color_setting", "colorRgb", color_int),
        f"Set {info['name']} to {color}",
    )


@mcp.tool()
async def set_color_temp(light: str, kelvin: int) -> str:
    """Set color temperature of a light in Kelvin. 2000K = warm/candlelight, 4000K = neutral, 6500K = daylight, 9000K = cool blue-white.

    Args:
        light: Light name (e.g. 'floor_lamp')
        kelvin: Color temperature in Kelvin (2000–9000)
    """
    if light not in LIGHTS:
        return f"Unknown light '{light}'. Available: {list(LIGHTS.keys())}"
    info = LIGHTS[light]
    kelvin = max(2000, min(9000, kelvin))
    return await _send(
        info,
        {"msg": {"cmd": "colorwc", "data": {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": kelvin}}},
        ("devices.capabilities.color_setting", "colorTemperatureK", kelvin),
        f"Set {info['name']} color temperature to {kelvin}K",
    )


if __name__ == "__main__":
    mcp.run()
