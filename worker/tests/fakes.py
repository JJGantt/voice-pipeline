"""Fakes for the services the worker talks to, and nothing else.

Supabase is replaced at the HTTP layer by `FakePostgrest`, an httpx transport that answers PostgREST
requests from in-memory tables, so the worker's own REST wrapper, query strings and compare-and-swap
guards all run for real. Claude is replaced by a stand-in CLI (`fake_claude.py`, for the `claude -p`
agent path) and by `ScriptedRunner` (for the warm SDK client), which keeps every line of SdkRunner except
the one method that talks to the model, and runs each scripted tool call through the real fence.
"""

import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, parse_qsl

import httpx

SUPABASE_HOST = "supabase.test"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def ago(seconds: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _ts(v):
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", v):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    return v


def _scalar(raw: str, sample):
    if raw == "null":
        return None
    if isinstance(sample, bool) or raw in ("true", "false"):
        return raw == "true"
    if isinstance(sample, (int, float)):
        try:
            return type(sample)(raw)
        except ValueError:
            return raw
    return _ts(raw)


def _arr(raw: str) -> list:
    inner = raw.strip()[1:-1]
    return [x.strip().strip('"') for x in inner.split(",") if x.strip()]


def _match(row: dict, col: str, expr: str) -> bool:
    neg = expr.startswith("not.")
    if neg:
        expr = expr[4:]
    op, _, raw = expr.partition(".")
    v = row.get(col)
    if op == "is":
        res = v is None if raw == "null" else v is (raw == "true")
        return not res if neg else res
    if op == "in":
        vals = [x.strip().strip('"') for x in raw.strip()[1:-1].split(",")]
        res = v is not None and str(v) in vals
        return not res if neg else res
    if op == "cs":
        want = _arr(raw)
        res = v is not None and all(w in (v or []) for w in want)
        return not res if neg else res
    if v is None:
        return False   # SQL: a comparison with NULL is never true, negated or not
    if raw.startswith("{"):
        other = _arr(raw)
        a, b = sorted(v or []), sorted(other)
    else:
        a, b = _ts(v), _scalar(raw, v)
        if isinstance(a, datetime) != isinstance(b, datetime):
            a, b = str(v), raw
    import operator
    res = {"eq": operator.eq, "neq": operator.ne, "gt": operator.gt, "gte": operator.ge,
           "lt": operator.lt, "lte": operator.le}[op](a, b)
    return not res if neg else res


class FakePostgrest:
    """In-memory PostgREST. `tables[name]` is a list of row dicts; `log` records every request."""

    RESERVED = {"select", "order", "limit", "offset", "on_conflict"}

    def __init__(self):
        self.tables: dict[str, list] = {}
        self.log: list = []
        self.external: list = []   # requests to anything other than Supabase (hub, LED, orchestrator)
        self.transport = httpx.MockTransport(self.handle)
        self.before = []   # callables(method, table, query) run before a request is answered: races

    def rows(self, table: str) -> list:
        return self.tables.setdefault(table, [])

    def insert(self, table: str, **row) -> dict:
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", iso(datetime.now(timezone.utc)))
        self.rows(table).append(row)
        return row

    def get(self, table: str, id_: str) -> dict:
        return next(r for r in self.rows(table) if r.get("id") == id_)

    def _select(self, table, params):
        rows = [r for r in self.rows(table)
                if all(_match(r, k, v) for k, v in params if k not in self.RESERVED)]
        order = dict(params).get("order")
        if order:
            for part in reversed(order.split(",")):
                col, _, direction = part.partition(".")
                desc = direction.startswith("desc")
                present = [r for r in rows if r.get(col) is not None]
                missing = [r for r in rows if r.get(col) is None]
                present.sort(key=lambda r: _ts(r.get(col)), reverse=desc)
                rows = (missing + present) if desc else (present + missing)
        off = int(dict(params).get("offset") or 0)
        lim = dict(params).get("limit")
        rows = rows[off:off + int(lim)] if lim else rows[off:]
        return rows

    def handle(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        if u.hostname != SUPABASE_HOST:
            self.external.append({"method": request.method, "url": str(request.url),
                                  "headers": dict(request.headers),
                                  "body": request.content.decode() if request.content else ""})
            return httpx.Response(200, text="ok")
        if not u.path.startswith("/rest/v1/"):
            return httpx.Response(404)
        table = u.path[len("/rest/v1/"):]
        params = parse_qsl(u.query, keep_blank_values=True)
        self.log.append((request.method, table, u.query))
        for hook in list(self.before):
            hook(request.method, table, u.query)
        prefer = request.headers.get("prefer", "")
        if request.method == "GET":
            return httpx.Response(200, json=self._select(table, params))
        body = json.loads(request.content or b"null")
        if request.method == "PATCH":
            hit = self._select(table, [p for p in params if p[0] not in ("order", "limit")])
            for r in hit:
                r.update(body)
            return httpx.Response(200, json=hit) if "return=representation" in prefer else httpx.Response(204)
        if request.method == "POST":
            items = body if isinstance(body, list) else [body]
            conflict = dict(params).get("on_conflict")
            out = []
            for item in items:
                existing = next((r for r in self.rows(table) if conflict and r.get(conflict) == item.get(conflict)), None)
                if existing is not None:
                    existing.update(item)
                    out.append(existing)
                else:
                    out.append(self.insert(table, **item))
            return httpx.Response(201, json=out) if "return=representation" in prefer else httpx.Response(201)
        return httpx.Response(405)


def scripted_runner_class(SdkRunner):
    """A SdkRunner whose model is a script: every tool call goes through the real fence (`gate`) with the
    flags the real `run()` set for this turn, and the decisions are recorded."""

    class ScriptedRunner(SdkRunner):
        def __init__(self, tools=(), reply="Done.", ok=True, **kw):
            super().__init__(**kw)
            self.script = list(tools)
            self.reply, self.ok = reply, ok
            self.calls: list = []

        def warm(self):   # connecting is talking to Claude
            return None

        def _attempt(self, transcript, on_event):
            turn = {"prompt": transcript, "manual": self._manual, "ambient": self._ambient, "decisions": {}}
            self.calls.append(turn)
            for name, args in self.script:
                decision, _ = self.gate(name, args)
                turn["decisions"][name] = decision
                if decision != "deny" and on_event:
                    on_event("tool", {"name": name, "input": args})
            return {"session_id": None, "text": self.reply if self.ok else "", "result": self.reply,
                    "ok": self.ok, "used_tools": bool(self.script), "raw_error": None if self.ok else "boom"}

    return ScriptedRunner
