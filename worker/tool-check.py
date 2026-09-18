#!/usr/bin/env python3
"""Exercise every MCP tool server the voice worker registers, on this machine.

Why this exists: a tool server can start, answer tools/list and still fail every
real call — gmail-mcp ran sixteen days on a revoked Google token that way, and
the only symptom was the assistant saying "search failed" out loud. Liveness
says nothing here, so this calls a real read-only tool on each server and reports
what came back.

It imports the worker's own write_mcp_config(), so it probes exactly what the
worker launches. A hand-copied server list would drift the first time one moved,
and a probe aimed at the wrong thing is how both of today's failures hid.

Read-only by construction: PROBES names one tool per server that only reads. Do
not add a tool here that writes, sends, feeds or switches anything on.

usage: ~/repos/voice-pipeline/worker/tool-check.py [--json]
exit:  0 all good, 1 any server failed
"""

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# run.sh sources .env before starting the worker; without it the paths and tokens
# below resolve to defaults and this probes a configuration nothing runs.
envfile = HERE / ".env"
if envfile.exists():
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import worker  # noqa: E402  (must follow the .env load above)

# One read-only tool per server, with arguments small enough to stay cheap.
PROBES = {
    "data":            ("list_repos", {}),
    "voice":           ("get_routing", {}),
    "govee-lights":    ("list_lights", {}),
    "petlibro-feeder": ("feeder_status", {}),
    "gmail":           ("search_email", {"query": "in:inbox newer_than:1d", "limit": 1}),
    "messages":        ("list_chats", {"limit": 1}),
}
INIT = {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "tool-check", "version": "1"}}


def probe_stdio(spec, tool, args, timeout=60):
    env = {**os.environ, **spec.get("env", {})}
    proc = subprocess.Popen([spec["command"], *spec.get("args", [])],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env)

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def read():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT})
        read()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = read()
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": tool, "arguments": args}})
        called = read()
        return listed, called
    finally:
        proc.kill()


def probe_http(spec, tool, args, timeout=60):
    headers = {**spec.get("headers", {}), "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    session = {"id": None}

    def rpc(msg):
        h = dict(headers)
        if session["id"]:
            h["mcp-session-id"] = session["id"]
        req = urllib.request.Request(spec["url"], data=json.dumps(msg).encode(),
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as reply:
            body = reply.read().decode()
            session["id"] = reply.headers.get("mcp-session-id") or session["id"]
        events = [l[6:] for l in body.splitlines() if l.startswith("data: ")]
        text = events[-1] if events else body
        return json.loads(text) if text.strip() else None

    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT})
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    called = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": tool, "arguments": args}})
    return listed, called


def check(name, spec):
    """-> (ok, tool_count, detail)."""
    probe = PROBES.get(name)
    if not probe:
        return False, 0, "no read-only probe defined for this server"
    tool, args = probe
    try:
        if spec.get("type") == "http":
            listed, called = probe_http(spec, tool, args)
        else:
            listed, called = probe_stdio(spec, tool, args)
    except Exception as err:
        return False, 0, f"{type(err).__name__}: {err}"

    if not listed or "result" not in listed:
        return False, 0, "no tools/list response (server did not start or crashed)"
    count = len(listed["result"].get("tools", []))
    if not called or "result" not in called:
        err = (called or {}).get("error", {})
        return False, count, f"{tool} got no result: {err.get('message', called)}"
    result = called["result"]
    text = " ".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError"):
        return False, count, f"{tool} failed: {text[:200]}"
    return True, count, f"{tool} ok ({len(text)} chars)"


def main():
    cfg_path = worker.write_mcp_config()
    try:
        servers = json.load(open(cfg_path))["mcpServers"]
    finally:
        os.unlink(cfg_path)

    results, failed = {}, 0
    for name in sorted(servers):
        ok, count, detail = check(name, servers[name])
        results[name] = {"ok": ok, "tools": count, "detail": detail}
        if not ok:
            failed += 1

    if "--json" in sys.argv:
        print(json.dumps({"host": os.uname().nodename, "failed": failed,
                          "servers": results}, indent=2))
    else:
        for name, r in results.items():
            print(f"{'PASS' if r['ok'] else 'FAIL'}  {name:<16} "
                  f"{r['tools']:>3} tools  {r['detail']}")
        missing = sorted(set(PROBES) - set(servers))
        if missing:
            print(f"\nnot registered on this host: {', '.join(missing)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
