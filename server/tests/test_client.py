"""Regression test for the /v1 HTTP client boundary (#490 external code review) — a network
failure must become the same KomoriApiError shape as an API-level error, not an uncaught
httpx exception."""

import pytest

from komori_mcp.client import KomoriApiError, KomoriClient


async def test_unreachable_api_becomes_a_clean_komori_api_error():
    async def token_provider():
        return "komori_fake"

    # Port 1 is a reserved low port nothing listens on — a fast, deterministic connection refusal.
    client = KomoriClient(base_url="http://127.0.0.1:1", token_provider=token_provider)

    with pytest.raises(KomoriApiError) as exc:
        await client.get("/me")

    assert exc.value.status_code == 0
    assert "Could not reach" in exc.value.detail
