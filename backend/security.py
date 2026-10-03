"""
Local-only access control for the EasyScript backend.

The server binds to 127.0.0.1, but any web page open in the user's browser can
still send requests to localhost (form posts need no CORS preflight, and
WebSockets ignore CORS entirely). So every request must carry a per-launch
secret: the X-EasyScript-Token header, or ?token= where a header can't be set
(<audio src>, WebSocket URLs).

Who knows the secret:
  - the Premiere panel reads it from ~/.easyscript/token-<port> (via ExtendScript)
  - the standalone app gets it injected into the page it serves (main.py)

A Host-header allowlist additionally blocks DNS-rebinding, where a hostile
domain re-resolves to 127.0.0.1 to make its requests look same-origin.

Development: EASYSCRIPT_TOKEN pins the secret; EASYSCRIPT_AUTH=off disables the
check entirely (never ship that).
"""

import hmac
import json
import os
import secrets
from urllib.parse import parse_qs

TOKEN_HEADER = "x-easyscript-token"
APP_DIR = os.path.join(os.path.expanduser("~"), ".easyscript")

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_exempt_paths = {"/health"}
_token = None


def get_token():
    global _token
    if _token is None:
        _token = os.environ.get("EASYSCRIPT_TOKEN") or secrets.token_urlsafe(32)
    return _token


def auth_enabled():
    return os.environ.get("EASYSCRIPT_AUTH", "on").strip().lower() not in ("off", "0", "false", "no")


def add_exempt_paths(paths):
    """Paths reachable without the token — only things that are safe to hand
    to any web page (health check, the standalone app's own page/assets)."""
    _exempt_paths.update(paths)


def token_file_path(port):
    return os.path.join(APP_DIR, f"token-{int(port)}")


def write_token_file(port):
    """Publish the secret for the panel. Per-port, so a standalone app and the
    panel backend running side by side don't overwrite each other's token."""
    path = token_file_path(port)
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(get_token())
    os.replace(tmp, path)
    return path


def host_allowed(host_header):
    if not host_header:
        return True  # HTTP/1.0 clients; browsers always send Host
    h = host_header.strip().lower()
    if h.startswith("["):
        name = h.split("]", 1)[0] + "]"
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return name in _ALLOWED_HOSTS


def token_valid(candidate):
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), get_token().encode("utf-8"))


class LocalAccessMiddleware:
    """Pure ASGI middleware so it covers both HTTP and WebSocket scopes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}

        if not host_allowed(headers.get("host", "")):
            await self._deny(scope, send, 403, "Forbidden host")
            return

        # CORS preflights carry no credentials by design; the CORS middleware
        # (outermost) answers them.
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if not auth_enabled() or scope.get("path", "") in _exempt_paths:
            await self.app(scope, receive, send)
            return

        token = headers.get(TOKEN_HEADER, "")
        if not token:
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            token = (qs.get("token") or [""])[0]
        if not token_valid(token):
            await self._deny(scope, send, 401, "Missing or invalid EasyScript token")
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, send, status, message):
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake 403.
            await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            return
        body = json.dumps({"error": message, "auth": status == 401}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# Origins allowed to read responses: the CEP panel (file:// → "null"), the
# standalone app and local dev servers. The token is still required — this
# only stops other origins from reading what they can't authenticate for.
CORS_ORIGIN_REGEX = r"^(null|file://.*|https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?)$"
