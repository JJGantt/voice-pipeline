"""Token checks for the control routes and the device socket. Tokens come from headers only, never the
query string, which ends up in proxy logs and shell history."""

import hmac


def token_ok(given, want: str) -> bool:
    """Constant-time token check. An unset `want` never matches."""
    return bool(want) and isinstance(given, str) and hmac.compare_digest(given.encode(), want.encode())


def _bearer(headers, header: str) -> str | None:
    if headers is not None:
        tok = headers.get(header)
        if tok:
            return tok
        auth = headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
    return None


def http_token(request) -> str | None:
    """The token on a control request: `X-Token: <token>` or `Authorization: Bearer <token>`."""
    return _bearer(getattr(request, "headers", None), "X-Token")


def client_token(ws) -> str | None:
    """The device token on the ws handshake: the `X-Device-Token` header, or `Authorization: Bearer ...`.
    The firmware sends the header (firmware/src/main.cpp). websockets 15: the handler's ServerConnection
    has `.request.headers`."""
    return _bearer(getattr(getattr(ws, "request", None), "headers", None), "X-Device-Token")
