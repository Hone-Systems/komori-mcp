"""The hosted server's authorization boundary (senki#1074).

Runs the real ASGI app against a real local HTTP stand-in for Komori's authorization server and
`/v1` (a Starlette app on an ephemeral port — the same wire calls production makes, not a mocked
transport). Breaks if: an unauthenticated call stops getting a 401 that tells the client where to
authorise (RFC 9728 `resource_metadata` + the `scope` the spec asks for); a connection revoked on
komori.app keeps working because something cached its validation — the FIRST request after the
revoke must 401, `tools/list` included, even with a warm exchange cache; a write tool appears on the
hosted server; a result loses its komori.app source link; the Host check stops refusing foreign hosts;
startup proceeds without its authorization settings.
"""

import base64
import socket
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

SECRET = "test-secret"
RESOURCE = "http://localhost/mcp"


class _AuthorizationServer:
    """Komori's AS + `/v1`, reduced to the four calls the hosted server makes."""

    def __init__(self):
        self.active: set[str] = set()
        self.exchanged: dict[str, str] = {}
        self.revoked_upstream: set[str] = set()
        self.introspections = 0
        self.exchanges = 0
        self.exchange_down = False
        self.api_down = False

    def _authorised(self, request: Request) -> bool:
        expected = "Basic " + base64.b64encode(f"komori-mcp-server:{SECRET}".encode()).decode()
        return request.headers.get("authorization") == expected

    async def introspect(self, request: Request):
        if not self._authorised(request):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        token = (await request.form()).get("token")
        self.introspections += 1
        if token not in self.active:
            return JSONResponse({"active": False})
        return JSONResponse(
            {"active": True, "sub": "user-1", "scope": "komori:read", "aud": RESOURCE, "exp": int(time.time()) + 3600,
             "client_id": "client-1", "grant_id": "grant-1", "iss": "http://localhost"}
        )

    async def token(self, request: Request):
        if not self._authorised(request):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        form = await request.form()
        if self.exchange_down:
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        if form.get("subject_token") not in self.active:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        self.exchanges += 1
        upstream = f"komori_oxt_{self.exchanges}"
        self.exchanged[upstream] = form["subject_token"]
        return JSONResponse({"access_token": upstream, "expires_in": 600, "token_type": "Bearer"})

    async def company(self, request: Request):
        if self.api_down:
            return JSONResponse({"detail": "サーバーエラーが発生しました"}, status_code=503)
        bearer = request.headers.get("authorization", "")[7:]
        if bearer not in self.exchanged or bearer in self.revoked_upstream:
            return JSONResponse({"detail": "Invalid token"}, status_code=401)
        ticker = request.path_params["ticker"]
        return JSONResponse({"data": {"ticker": ticker, "url": f"https://komori.app/company/{ticker}"}, "notice": "n"})

    def app(self):
        return Starlette(
            routes=[
                Route("/oauth/introspect", self.introspect, methods=["POST"]),
                Route("/oauth/token", self.token, methods=["POST"]),
                Route("/v1/companies/{ticker}", self.company),
            ]
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def stub():
    state = _AuthorizationServer()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(state.app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    state.base = f"http://127.0.0.1:{port}"
    yield state
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def client(stub, monkeypatch):
    monkeypatch.setenv("KOMORI_OAUTH_ISSUER", "http://localhost")
    monkeypatch.setenv("KOMORI_OAUTH_INTERNAL_BASE", stub.base)
    monkeypatch.setenv("KOMORI_MCP_RESOURCE", RESOURCE)
    monkeypatch.setenv("KOMORI_MCP_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("KOMORI_API_BASE", f"{stub.base}/v1")
    from komori_mcp import http as hosted
    from komori_mcp import server

    monkeypatch.setattr(server._client, "base_url", f"{stub.base}/v1")
    # `create_app` switches the module to hosted mode; recording the local values first makes
    # monkeypatch put them back, so the stdio tests that run after this module see stdio.
    monkeypatch.setattr(server, "_on_unauthorized", server._on_unauthorized)
    monkeypatch.setattr(server._client, "_token_provider", server._client._token_provider)
    with TestClient(hosted.create_app(), base_url="http://localhost") as test_client:
        yield test_client


def _rpc(client, token, method, params=None, host=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)


def test_no_token_is_a_401_that_says_where_to_authorise_and_what_to_ask_for(client):
    resp = _rpc(client, None, "tools/list")
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="komori:read"' in challenge


def test_protected_resource_metadata_is_served_at_the_path_and_the_root(client):
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        doc = client.get(path).json()
        # Exact strings: RFC 8414 clients compare the issuer byte for byte, and the SDK's own copy
        # of this document would have said "http://localhost/".
        assert doc["resource"] == RESOURCE, path
        assert doc["authorization_servers"] == ["http://localhost"], path
        assert doc["scopes_supported"] == ["komori:read"], path
    assert client.get("/healthz").json() == {"ok": True}


def test_the_hosted_server_lists_read_tools_only_each_marked_read_only(client, stub):
    stub.active.add("komori_oat_list")
    resp = _rpc(client, "komori_oat_list", "tools/list")
    assert resp.status_code == 200, resp.text
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert len(tools) == 22
    assert not names & {"create_watchlist", "add_to_watchlist", "set_notification_delivery"}
    assert all(t["annotations"]["readOnlyHint"] is True and t.get("title") for t in tools)


def test_a_tool_result_carries_its_komori_source_page(client, stub):
    stub.active.add("komori_oat_call")
    resp = _rpc(client, "komori_oat_call", "tools/call", {"name": "get_company", "arguments": {"ticker": "7203"}})
    assert resp.status_code == 200, resp.text
    text = resp.json()["result"]["content"][0]["text"]
    assert '"url": "https://komori.app/company/7203?utm_source=komori-mcp&utm_medium=mcp"' in text
    assert '"name": "Komori (komori.app)"' in text


def test_revocation_401s_the_very_next_request_even_with_a_warm_exchange_cache(client, stub):
    stub.active.add("komori_oat_revoke")
    first = _rpc(client, "komori_oat_revoke", "tools/call", {"name": "get_company", "arguments": {"ticker": "6758"}})
    assert first.status_code == 200
    exchanges = stub.exchanges
    stub.active.discard("komori_oat_revoke")
    assert _rpc(client, "komori_oat_revoke", "tools/list").status_code == 401
    assert _rpc(client, "komori_oat_revoke", "tools/call", {"name": "get_company", "arguments": {"ticker": "6758"}}).status_code == 401
    assert stub.exchanges == exchanges, "a revoked token must never reach the exchange"


def test_an_upstream_401_evicts_the_cached_credential(client, stub):
    stub.active.add("komori_oat_evict")
    assert _rpc(client, "komori_oat_evict", "tools/call", {"name": "get_company", "arguments": {"ticker": "9984"}}).status_code == 200
    upstream = f"komori_oxt_{stub.exchanges}"
    stub.revoked_upstream.add(upstream)
    second = _rpc(client, "komori_oat_evict", "tools/call", {"name": "get_company", "arguments": {"ticker": "9984"}})
    assert '"status_code": 401' in second.json()["result"]["content"][0]["text"]
    before = stub.exchanges
    _rpc(client, "komori_oat_evict", "tools/call", {"name": "get_company", "arguments": {"ticker": "9984"}})
    assert stub.exchanges == before + 1, "the evicted credential was reused instead of re-exchanged"


def test_a_foreign_host_header_is_refused(client, stub):
    stub.active.add("komori_oat_host")
    assert _rpc(client, "komori_oat_host", "tools/list", host="evil.example").status_code == 421


def test_startup_refuses_to_run_without_its_authorization_settings(monkeypatch):
    from komori_mcp import http as hosted

    for name in ("KOMORI_OAUTH_ISSUER", "KOMORI_MCP_RESOURCE", "KOMORI_MCP_CLIENT_SECRET", "KOMORI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="KOMORI_OAUTH_ISSUER"):
        hosted.create_app()


def test_an_upstream_outage_is_a_flagged_failure_that_still_names_its_komori_page(client, stub):
    """Code review (QA second opinion): a failed exchange used to surface as raw exception text with no
    `source` and no failure metric, and a /v1 503 came back with isError=false — an outage looked like
    data. Both must be MCP errors carrying the source link, and the next call must recover."""
    stub.active.add("komori_oat_outage")
    stub.exchange_down = True
    try:
        down = _rpc(client, "komori_oat_outage", "tools/call", {"name": "get_company", "arguments": {"ticker": "4063"}})
    finally:
        stub.exchange_down = False
    result = down.json()["result"]
    assert down.status_code == 200 and result["isError"] is True
    assert result["structuredContent"]["status_code"] == 503
    assert result["structuredContent"]["source"]["url"].startswith("https://komori.app/company/4063")

    stub.api_down = True
    try:
        api_down = _rpc(client, "komori_oat_outage", "tools/call", {"name": "get_company", "arguments": {"ticker": "4063"}})
    finally:
        stub.api_down = False
    assert api_down.json()["result"]["isError"] is True
    assert api_down.json()["result"]["structuredContent"]["status_code"] == 503

    recovered = _rpc(client, "komori_oat_outage", "tools/call", {"name": "get_company", "arguments": {"ticker": "4063"}})
    assert recovered.json()["result"].get("isError") in (False, None)


def test_tool_outcomes_separate_a_callers_mistake_from_an_outage():
    from komori_mcp import server

    assert server._outcome({"data": 1}) == "ok"
    assert server._outcome({"is_error": True, "status_code": 404}) == "error"
    assert server._outcome({"is_error": True, "status_code": 429}) == "error"
    assert server._outcome({"is_error": True, "status_code": 503}) == "failure"
    assert server._outcome({"is_error": True, "status_code": 0}) == "failure"
    assert server._outcome({"is_error": True, "error": "login timed out"}) == "failure"
