"""Regression tests for the tool-call error/retry boundary (`_call`, #490 first recheck pass)."""

import komori_mcp.server as server
from komori_mcp.client import KomoriApiError


async def test_a_second_401_returns_a_clean_error_instead_of_raising(monkeypatch):
    monkeypatch.setattr(server, "clear_stored_token", lambda: None)
    calls = {"n": 0}

    async def always_401(*args, **kwargs):
        calls["n"] += 1
        raise KomoriApiError(401, "Invalid token")

    result = await server._call(always_401)

    assert result == {"is_error": True, "status_code": 401, "error": "Invalid token"}
    assert calls["n"] == 2, "must retry exactly once through a fresh login before giving up"


async def test_a_login_timeout_returns_a_clean_error_instead_of_raising():
    async def raises_timeout(*args, **kwargs):
        raise TimeoutError("Login did not complete within 180s.")

    result = await server._call(raises_timeout)

    assert result == {"is_error": True, "error": "Login did not complete within 180s."}
