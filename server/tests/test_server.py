"""Regression tests for the tool-call error/retry boundary (`_call`, #490 first recheck pass)
and the cross-company theme tools (#634).

The theme tools are thin pass-throughs over `/v1`, so the regression net pins the CONTRACT an
agent sees: which endpoint each tool maps to, what parameters it forwards, and that the themes
mode of `get_company_threads` fails loud when a caller mixes it with the thread filters
(docs/TOOL-DESIGN.md §6 — a silently-ignored filter would read as "the company has no softened
themes").
"""

import komori_mcp.server as server
from komori_mcp.client import KomoriApiError, KomoriResult


class _FakeClient:
    """Records every request the tools make; answers with a canned success."""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.result = KomoriResult(
            data={"ok": True}, notice=None, units_charged=None, units_remaining=None
        )

    async def get(self, path, params=None):
        self.calls.append((path, params))
        return self.result


def _fake(monkeypatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(server, "_client", fake)
    return fake


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


# --- #634: cross-company themes ----------------------------------------------------------------


async def test_list_themes_maps_to_the_free_index_endpoint(monkeypatch):
    fake = _fake(monkeypatch)
    await server.list_themes(min_reach=5, section="contested", q="漏洩")
    assert fake.calls == [(
        "/themes",
        {"min_reach": 5, "limit": 50, "skip": 0, "section": "contested", "q": "漏洩"},
    )]


async def test_get_theme_maps_to_the_metered_detail_endpoint(monkeypatch):
    fake = _fake(monkeypatch)
    await server.get_theme("theme-1", limit=20, skip=40, polarity="NEGATIVE")
    assert fake.calls == [(
        "/themes/theme-1",
        {"limit": 20, "skip": 40, "polarity": "NEGATIVE"},
    )]


async def test_company_threads_themes_mode_calls_the_company_themes_endpoint(monkeypatch):
    fake = _fake(monkeypatch)
    await server.get_company_threads("7203", themes=True)
    assert fake.calls == [("/companies/7203/themes", None)]


async def test_company_threads_themes_mode_refuses_thread_filters(monkeypatch):
    fake = _fake(monkeypatch)
    result = await server.get_company_threads("7203", themes=True, move="SOFTENED")
    assert result["is_error"] is True, "a silently-ignored filter would read as a false finding"
    assert "絞り込み" in result["error"]
    assert fake.calls == [], "no API call may happen for a rejected combination"


async def test_company_threads_default_mode_is_unchanged(monkeypatch):
    fake = _fake(monkeypatch)
    await server.get_company_threads("7203", move="SOFTENED", since="FY2023")
    assert fake.calls == [(
        "/companies/7203/threads",
        {"move": "SOFTENED", "since": "FY2023"},
    )]
