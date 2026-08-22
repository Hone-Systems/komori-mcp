"""Thin HTTP client for the Komori external API (`/v1`).

Every tool in `server.py` goes through here. This module owns exactly two things: attaching the
bearer token to every request, and reading `X-Komori-Units-Charged`/`X-Komori-Units-Remaining` off
the response so tool results can tell the calling model what a call actually cost, without a
separate `/v1/me` round trip on every call. It does not shape responses or know what any endpoint
means — that belongs in `server.py`, one tool at a time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.komori.app/v1"


class KomoriApiError(Exception):
    """Raised with the API's own `detail` message, so a tool can hand the model the same
    actionable error text `/v1` already wrote (docs/TOOL-DESIGN.md §6) — never a generic wrapper
    string that throws away why the call failed."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


@dataclass
class KomoriResult:
    data: Any
    notice: str | None
    units_charged: int | None
    units_remaining: int | None


class KomoriClient:
    def __init__(self, base_url: str | None = None, token_provider=None):
        self.base_url = (base_url or os.environ.get("KOMORI_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        # A callable, not a fixed token: the caller may need to re-run the browser-login flow
        # mid-session if a token was revoked, and a fixed string can never recover from that.
        self._token_provider = token_provider

    async def request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> KomoriResult:
        token = await self._token_provider() if self._token_provider else None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.request(
                    method, f"{self.base_url}{path}", params=params, json=json, headers=headers
                )
        except httpx.RequestError as exc:
            # DNS failure, connection refused, TLS error, read timeout — none of these are the
            # API answering with an error, they're the API not being reachable at all. Routed
            # through the same KomoriApiError shape (status 0, no real HTTP status) so `_call`'s
            # single error-handling chokepoint covers this without a second except clause, and a
            # network blip never masquerades as a 401 that would wrongly trigger a re-login.
            raise KomoriApiError(
                0, f"Could not reach the Komori API ({exc.__class__.__name__}). Check your network and try again."
            ) from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise KomoriApiError(resp.status_code, str(detail))

        try:
            body = resp.json()
        except ValueError as exc:
            raise KomoriApiError(resp.status_code, "The API returned a response that wasn't valid JSON.") from exc

        # A malformed unit header is a metering-display problem, not a reason to fail an
        # otherwise-successful read — the caller still gets its data, just without a units figure.
        charged_raw = resp.headers.get("X-Komori-Units-Charged")
        remaining_raw = resp.headers.get("X-Komori-Units-Remaining")
        try:
            units_charged = int(charged_raw) if charged_raw is not None else None
            units_remaining = int(remaining_raw) if remaining_raw is not None else None
        except ValueError:
            units_charged = units_remaining = None

        return KomoriResult(
            data=body.get("data"),
            notice=body.get("notice"),
            units_charged=units_charged,
            units_remaining=units_remaining,
        )

    async def get(self, path: str, params: dict | None = None) -> KomoriResult:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None) -> KomoriResult:
        return await self.request("POST", path, json=json)

    async def put(self, path: str) -> KomoriResult:
        return await self.request("PUT", path)

    async def delete(self, path: str) -> KomoriResult:
        return await self.request("DELETE", path)

    async def patch(self, path: str, json: dict | None = None) -> KomoriResult:
        return await self.request("PATCH", path, json=json)
