"""The browser-login bootstrap (#490).

The pattern is the one `gh auth login --web` / `firebase login` use, not the full MCP OAuth 2.1
spec: this process opens the user's browser to a page on komori.app that is already protected by
Auth0, the signed-in user approves, the backend mints a real `komori_...` token (the same mint
endpoint `/settings` uses) and hands it back over a one-shot localhost listener. No dashboard
config, no dynamic client registration, no pasted token — the flow rides the login that already
works today.

Token storage tries the OS keychain first (`keyring`) and falls back to a file under
`~/.komori/` with owner-only permissions if `keyring` has no backend available (e.g. a headless
Linux box with no secret service running) — never plaintext with default permissions.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_APP_BASE = "https://komori.app"
LOGIN_TIMEOUT_S = 180

_CONFIG_DIR = Path.home() / ".komori"
_TOKEN_FILE = _CONFIG_DIR / "mcp_token.json"
_KEYRING_SERVICE = "komori-mcp"
_KEYRING_USER = "api_token"

_SUCCESS_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding-top:4rem">
<h2>Connected to Komori.</h2><p>You can close this window.</p></body></html>"""


def _try_import_keyring():
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:
        return None


def _load_stored_token() -> str | None:
    keyring = _try_import_keyring()
    if keyring:
        try:
            token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
            if token:
                return token
        except Exception:
            pass
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text()).get("token")
        except Exception:
            return None
    return None


def _store_token(token: str) -> None:
    keyring = _try_import_keyring()
    if keyring:
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
            return
        except Exception:
            pass
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps({"token": token}))
    os.chmod(_TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)


def clear_stored_token() -> None:
    """Called after a 401, so a revoked token does not loop forever."""
    keyring = _try_import_keyring()
    if keyring:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            pass
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()


def _run_login_flow_sync(app_base: str) -> str:
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("state", [None])[0] == state and qs.get("token", [None])[0]:
                result["token"] = qs["token"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_SUCCESS_HTML.encode())
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *_args):
            return  # silence — this is a one-shot local listener, not a service

    # Port 0 = OS-assigned ephemeral port, bound to loopback only. Never 0.0.0.0: this listener
    # exists for exactly one redirect from the user's own browser on this machine.
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port

    callback = f"http://127.0.0.1:{port}/callback"
    authorize_url = f"{app_base}/mcp-authorize?callback={callback}&state={state}"
    webbrowser.open(authorize_url)

    server.timeout = LOGIN_TIMEOUT_S
    server.handle_request()  # blocks for exactly one request, or times out
    server.server_close()

    if "token" not in result:
        raise TimeoutError(
            f"Login did not complete within {LOGIN_TIMEOUT_S}s. "
            f"If your browser didn't open, go to: {authorize_url}"
        )
    return result["token"]


async def login(app_base: str | None = None) -> str:
    """Run the browser-login flow and persist the resulting token. Blocking work runs in a
    thread so it never stalls the MCP server's event loop."""
    base = app_base or os.environ.get("KOMORI_APP_BASE") or DEFAULT_APP_BASE
    token = await asyncio.to_thread(_run_login_flow_sync, base)
    _store_token(token)
    return token


async def get_token(app_base: str | None = None) -> str:
    """The token provider `KomoriClient` calls before every request. A stored token is reused;
    with none stored, this triggers the browser-login flow automatically — the whole point being
    that the first tool call just works, with no separate `komori-mcp login` step to remember."""
    token = _load_stored_token()
    if token:
        return token
    return await login(app_base)
