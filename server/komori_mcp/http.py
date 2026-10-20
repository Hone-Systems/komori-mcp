"""The hosted Komori MCP server — Streamable HTTP behind OAuth 2.1 (senki#1074).

`https://mcp.komori.app/mcp`. People connect it from Claude, ChatGPT or an IDE; the client finds our
authorization server through the protected-resource metadata this app serves (RFC 9728), the person
signs in on komori.app and approves, and the client calls this server with an access token bound to
it (RFC 8707). Read-only at launch: only the read tools are registered, and the API refuses writes
for this credential regardless.

PER REQUEST
-----------
1. Validate: the bearer token is INTROSPECTED at the authorization server (RFC 7662) on every request
   and never cached here. A connection revoked in komori.app's 設定 must fail the very next request —
   `tools/list` included — with an HTTP 401, which only an uncached check can promise.
2. Upstream credential (tool calls): the MCP spec forbids passing the client's token on to `/v1`. The
   access token is exchanged (RFC 8693) for a short `/v1` token for the same person, and that one is
   cached in memory until it expires. `/v1` meters and limits it as that person.

Configuration is the environment, and every required value fails startup loudly when absent — a
network-reachable MCP server with no authorization would serve anyone.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry import metrics
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from komori_mcp import server
from komori_mcp.client import KomoriApiError

logger = logging.getLogger(__name__)

SCOPE = "komori:read"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

_DEFAULT_HOSTS = "mcp.komori.app,localhost,localhost:*,127.0.0.1,127.0.0.1:*,mcp,mcp:*"
# Origin validation is a transport-spec MUST (DNS rebinding). Server-side clients (Claude.ai's and
# ChatGPT's backends) send no Origin and pass; browser clients do, and must be on this list.
_DEFAULT_ORIGINS = "https://claude.ai,https://claude.com,https://chatgpt.com,http://localhost:*,http://127.0.0.1:*"

_meter = metrics.get_meter("komori_mcp")
_evictions = _meter.create_counter(
    "komori.mcp.exchange_cache_evictions", description="Exchanged /v1 credentials evicted from the cache before expiry"
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set to run the hosted (streamable-http) server")
    return value


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def _basic(client_id: str, secret: str) -> str:
    raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class UpstreamUnavailable(KomoriApiError):
    """Komori's authorization server could not issue the `/v1` credential (down, misconfigured, or
    refusing our own client secret). A server-side fault: reported to the caller as a failed tool
    call (503), counted as a failure, never mistaken for the caller's connection being revoked."""

    def __init__(self, detail: str):
        super().__init__(503, detail)


class AuthorizationServer:
    """The two confidential-client calls this server makes to Komori's authorization server."""

    def __init__(self, base: str, client_id: str, client_secret: str, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self._auth = _basic(client_id, client_secret)
        self._timeout = timeout

    async def _post(self, path: str, form: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(f"{self.base}{path}", data=form, headers={"Authorization": self._auth})

    async def introspect(self, token: str) -> dict:
        resp = await self._post("/oauth/introspect", {"token": token})
        if resp.status_code != 200:
            # Our own credential was refused, or the AS is broken: a server fault, not the caller's.
            raise RuntimeError(f"introspection failed with HTTP {resp.status_code}")
        return resp.json()

    async def exchange(self, token: str) -> dict:
        try:
            resp = await self._post(
                "/oauth/token",
                {"grant_type": TOKEN_EXCHANGE, "subject_token": token, "subject_token_type": ACCESS_TOKEN_TYPE},
            )
        except httpx.HTTPError as exc:
            logger.error("token exchange unreachable: %s", type(exc).__name__)
            raise UpstreamUnavailable("Komoriに一時的に接続できません。少し待ってからもう一度お試しください。") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code == 400 and isinstance(body, dict) and body.get("error") == "invalid_grant":
            raise KomoriApiError(401, "this connection is no longer valid")
        if resp.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            logger.error("token exchange failed (HTTP %s, usable body: %s)", resp.status_code, isinstance(body, dict))
            raise UpstreamUnavailable("Komoriに一時的に接続できません。少し待ってからもう一度お試しください。")
        return body


class IntrospectionVerifier:
    """`TokenVerifier` for the SDK's bearer middleware. Inactive -> None -> HTTP 401."""

    def __init__(self, authz: AuthorizationServer, resource: str):
        self.authz = authz
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = await self.authz.introspect(token)
        if not claims.get("active") or claims.get("aud") != self.resource:
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id") or "",
            scopes=(claims.get("scope") or "").split(),
            expires_at=claims.get("exp"),
            resource=claims.get("aud"),
            subject=claims.get("sub"),
            claims={"iss": claims.get("iss"), "grant_id": claims.get("grant_id")},
        )


class ExchangeCache:
    """Exchanged `/v1` credentials, keyed by the SHA-256 of the access token they came from.

    A credential store, not a validation cache: every request was already introspected. Bounded at
    `max_entries` (an entry is ~200 bytes, so 10 000 is ~2 MB on a 512 MB instance); an eviction costs
    one extra exchange and is counted, so a cap that is too small shows up as a metric, not a mystery.
    """

    def __init__(self, authz: AuthorizationServer, max_entries: int):
        self.authz = authz
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def get(self, access_token: str) -> str:
        key = _key(access_token)
        hit = self._entries.get(key)
        if hit and hit[1] > time.monotonic():
            self._entries.move_to_end(key)
            return hit[0]
        issued = await self.authz.exchange(access_token)
        # A few seconds of margin so a credential is never sent in the instant it expires.
        expires = time.monotonic() + max(1, int(issued.get("expires_in") or 0) - 5)
        self._entries[key] = (issued["access_token"], expires)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            _evictions.add(1, {"reason": "capacity"})
        return issued["access_token"]

    def evict(self, access_token: str) -> None:
        if self._entries.pop(_key(access_token), None) is not None:
            _evictions.add(1, {"reason": "unauthorized"})


class ScopeChallenge:
    """Adds `scope="komori:read"` to the SDK's `WWW-Authenticate` challenge (the MCP spec says
    servers SHOULD include it; the SDK's middleware does not)."""

    def __init__(self, app: ASGIApp, scope: str):
        self.app = app
        self.scope = scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_scope(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in (401, 403):
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"www-authenticate" and b"scope=" not in value:
                        value = value + f', scope="{self.scope}"'.encode()
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_scope)


def create_app() -> ASGIApp:
    issuer = _required("KOMORI_OAUTH_ISSUER").rstrip("/")
    resource = _required("KOMORI_MCP_RESOURCE").rstrip("/")
    secret = _required("KOMORI_MCP_CLIENT_SECRET")
    _required("KOMORI_API_BASE")
    internal = os.environ.get("KOMORI_OAUTH_INTERNAL_BASE", "").strip() or issuer
    client_id = os.environ.get("KOMORI_MCP_CLIENT_ID", "komori-mcp-server")
    cache_max = int(os.environ.get("KOMORI_MCP_EXCHANGE_CACHE_MAX", "10000"))

    authz = AuthorizationServer(internal, client_id, secret)
    cache = ExchangeCache(authz, cache_max)

    async def token_provider() -> str:
        verified = get_access_token()
        if verified is None:
            raise KomoriApiError(401, "no verified access token on this request")
        return await cache.get(verified.token)

    async def on_unauthorized() -> None:
        verified = get_access_token()
        if verified is not None:
            cache.evict(verified.token)

    server.use_http_tokens(token_provider, on_unauthorized)

    mcp = server.build_server(
        include_writes=False,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        token_verifier=IntrospectionVerifier(authz, resource),
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource,
            required_scopes=[SCOPE],
            validate_token_resource=True,
            service_documentation_url="https://github.com/Hone-Systems/komori-mcp#hosted-server",
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_list("KOMORI_MCP_ALLOWED_HOSTS", _DEFAULT_HOSTS),
            allowed_origins=_list("KOMORI_MCP_ALLOWED_ORIGINS", _DEFAULT_ORIGINS),
        ),
    )

    metadata: dict[str, Any] = {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Komori",
        "resource_documentation": "https://github.com/Hone-Systems/komori-mcp#hosted-server",
    }

    async def protected_resource(_: Request) -> JSONResponse:
        return JSONResponse(metadata, headers={"Cache-Control": "public, max-age=3600"})

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app: Starlette = mcp.streamable_http_app()
    # Our own protected-resource metadata, served at the resource's path (RFC 9728 §3.1) AND at the
    # root (clients that probe it first), ahead of the SDK's copy. The SDK renders the issuer through
    # its URL type, which appends "/" to a root URL — and RFC 8414 clients compare the issuer byte
    # for byte with the authorization server's own `issuer` (which has no slash). Serving the exact
    # string is the fix; the SDK's route stays registered behind ours, unreachable.
    metadata_path = urlsplit(str(build_resource_metadata_url(AnyHttpUrl(resource)))).path
    for path in dict.fromkeys((metadata_path, "/.well-known/oauth-protected-resource")):
        app.router.routes.insert(0, Route(path, protected_resource, methods=["GET"]))
    logger.info(
        "komori-mcp hosted server ready (resource=%s issuer=%s metadata=%s)",
        resource,
        issuer,
        build_resource_metadata_url(resource),
    )
    wrapped: ASGIApp = ScopeChallenge(app, SCOPE)
    return CORSMiddleware(
        wrapped,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Mcp-Protocol-Version", "Mcp-Session-Id", "Last-Event-ID"],
        expose_headers=["WWW-Authenticate", "Mcp-Session-Id", "Mcp-Protocol-Version"],
    )
