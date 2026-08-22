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
import time
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

# This local page, not `/mcp-authorize` on komori.app, is the LAST thing the user actually sees —
# the retail page redirects here the moment it has a token, so this is the frame that stays on
# screen. It needs the same visual care as the retail flow it's the tail end of, not a bare
# placeholder — reuses the same ring-draw success mark, accent, and type as the Komori retail UI
# (docs/retail-DESIGN.md: light-only, Zen Kaku Gothic New, blue accent, no card nesting) so the
# whole login reads as one continuous, deliberate moment rather than ending on a different product.
_SUCCESS_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Komori</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --color-surface: #faf8f3;
    --color-text: #1a1815;
    --color-text-muted: #6f6862;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--color-surface); color: var(--color-text);
    font-family: "Zen Kaku Gothic New", -apple-system, sans-serif;
  }
  .card {
    display: flex; flex-direction: column; align-items: center; gap: 18px;
    padding: 0 24px; text-align: center;
    animation: rise 0.5s cubic-bezier(0.22, 0.61, 0.36, 1) both;
  }
  h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.01em; margin: 0; }
  p { font-size: 15px; color: var(--color-text-muted); margin: 0; }
  @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes ring { to { stroke-dashoffset: 0; } }
  @keyframes check { to { stroke-dashoffset: 0; } }
  .ring { animation: ring 0.8s cubic-bezier(0.2, 0.8, 0.2, 1) 0.1s forwards; }
  .check { animation: check 0.45s ease 0.75s forwards; }
</style>
</head>
<body>
  <div class="card">
    <svg width="72" height="72" viewBox="0 0 84 84" aria-hidden="true">
      <circle cx="42" cy="42" r="38" fill="#EAF3EE"/>
      <circle cx="42" cy="42" r="38" fill="none" stroke="#2C7A5B" stroke-width="3.5"
              stroke-linecap="round" stroke-dasharray="239" stroke-dashoffset="239" class="ring"/>
      <path d="M27 43 l10 10 l20 -23" fill="none" stroke="#2C7A5B" stroke-width="5"
            stroke-linecap="round" stroke-linejoin="round"
            stroke-dasharray="52" stroke-dashoffset="52" class="check"/>
    </svg>
    <h1>Komoriに接続しました</h1>
    <p>このウィンドウは閉じて構いません。</p>
  </div>
</body>
</html>"""

_INVALID_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>Komori</title>
<link href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@500&display=swap" rel="stylesheet">
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#faf8f3; color:#6f6862; font-family:"Zen Kaku Gothic New",-apple-system,sans-serif; }
  p { font-size:15px; }
</style></head>
<body><p>この接続先は無効です。</p></body></html>"""


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
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_INVALID_HTML.encode())

        def log_message(self, *_args):
            return  # silence — this is a one-shot local listener, not a service

    # Port 0 = OS-assigned ephemeral port, bound to loopback only. Never 0.0.0.0: this listener
    # exists for exactly one redirect from the user's own browser on this machine.
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port

    callback = f"http://127.0.0.1:{port}/callback"
    authorize_url = f"{app_base}/mcp-authorize?callback={callback}&state={state}"
    webbrowser.open(authorize_url)

    # Looped rather than a single `handle_request()`: one stray request on the ephemeral port
    # (a browser prefetch, an unrelated local probe) before the real callback would otherwise
    # consume the listener's one shot and strand the actual login until the timeout.
    # `Handler.do_GET` only ever sets `result["token"]` on a state match, so an invalid request
    # gets its 400 and the loop keeps waiting for the real one.
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while "token" not in result:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        server.timeout = remaining
        server.handle_request()
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


# FastMCP can dispatch several tool calls concurrently. Without single-flighting, every one of
# them would see "no stored token" on the very first calls of a session and independently open
# its own browser window and mint its own personal API token — several durable credentials for
# one login. `_login_lock` serialises the check-and-launch decision; `_login_task` is the one
# in-flight login every concurrent caller then awaits together, so exactly one browser opens and
# one token is minted no matter how many tool calls arrive at once.
_login_lock = asyncio.Lock()
_login_task: "asyncio.Task[str] | None" = None


async def get_token(app_base: str | None = None) -> str:
    """The token provider `KomoriClient` calls before every request. A stored token is reused;
    with none stored, this triggers the browser-login flow automatically — the whole point being
    that the first tool call just works, with no separate `komori-mcp login` step to remember."""
    global _login_task

    token = _load_stored_token()
    if token:
        return token

    async with _login_lock:
        # Re-check inside the lock: a waiter that queued behind another caller's login may find
        # it already finished and stored a token by the time it gets in.
        token = _load_stored_token()
        if token:
            return token
        if _login_task is None or _login_task.done():
            _login_task = asyncio.create_task(login(app_base))
        task = _login_task

    try:
        # `asyncio.shield`, not a bare `await task`: without it, cancelling ONE caller (e.g. the
        # MCP client cancelling a single tool call) propagates into the shared task itself and
        # cancels every other waiter's login along with it — a caller's cancellation must not be
        # able to take down a login two other callers are legitimately waiting on.
        return await asyncio.shield(task)
    finally:
        async with _login_lock:
            # Only clear once the task has actually finished. A caller whose await was cut short
            # by shielded cancellation reaches this `finally` before the still-running task does
            # — clearing unconditionally here would let a new caller start a SECOND login while
            # the first is still in flight, which is the exact bug this whole mechanism exists to
            # prevent, just reachable through early cancellation instead of the initial race.
            if _login_task is task and task.done():
                _login_task = None
