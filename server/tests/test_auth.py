"""Regression tests for the browser-login bootstrap (#490 external code review).

Three findings from the review, each pinned here:
  - concurrent first calls independently opening their own browser-login and minting
    several personal API tokens (single-flighting)
  - a caller's cancellation propagating into the shared login and cancelling every other
    waiter (asyncio.shield)
  - the related edge shield alone doesn't cover: a caller cancelled early clearing the
    in-flight task while it is still running for everyone else
  - the login listener's single handle_request() call letting one stray request on the
    ephemeral port strand the real login until timeout
"""

import asyncio
import re
import threading
import time
import urllib.error
import urllib.request

import pytest

import komori_mcp.auth as auth


@pytest.fixture(autouse=True)
def _clean_singleflight_state(monkeypatch):
    """Every test gets its own single-flight state and never touches the real token store."""
    monkeypatch.setattr(auth, "_login_task", None)
    monkeypatch.setattr(auth, "_load_stored_token", lambda: None)
    monkeypatch.setattr(auth, "_store_token", lambda token: None)


async def test_concurrent_get_token_single_flights_login(monkeypatch):
    calls = {"n": 0}

    async def fake_login(app_base=None):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return f"komori_fake_{calls['n']}"

    monkeypatch.setattr(auth, "login", fake_login)

    results = await asyncio.gather(*[auth.get_token() for _ in range(5)])

    assert calls["n"] == 1, "five concurrent callers must trigger exactly one login"
    assert len(set(results)) == 1, "every caller must receive the same minted token"


async def test_cancelling_one_caller_does_not_cancel_others(monkeypatch):
    calls = {"n": 0}
    started = asyncio.Event()

    async def slow_login(app_base=None):
        calls["n"] += 1
        started.set()
        await asyncio.sleep(0.3)
        return "komori_fake_survivor"

    monkeypatch.setattr(auth, "login", slow_login)

    task_a = asyncio.create_task(auth.get_token())
    task_b = asyncio.create_task(auth.get_token())
    await started.wait()

    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    result_b = await task_b
    assert result_b == "komori_fake_survivor"
    assert calls["n"] == 1, "A's cancellation must not have forced a second login"


async def test_a_caller_arriving_after_an_early_cancellation_joins_the_running_login(monkeypatch):
    """Shielding alone stops A's cancellation from killing the task, but A's `finally` still
    runs on the way out — if it clears `_login_task` unconditionally, a caller arriving right
    after A gets cancelled starts a second login even though the first is still in flight."""
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_login(app_base=None):
        calls["n"] += 1
        started.set()
        await release.wait()
        return "komori_fake_result"

    monkeypatch.setattr(auth, "login", slow_login)

    task_a = asyncio.create_task(auth.get_token())
    await started.wait()
    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    task_c = asyncio.create_task(auth.get_token())
    await asyncio.sleep(0.01)
    release.set()
    result_c = await task_c

    assert result_c == "komori_fake_result"
    assert calls["n"] == 1, "a caller arriving after an early cancellation started a duplicate login"


def test_listener_survives_an_invalid_request_before_the_real_one(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_TIMEOUT_S", 6)
    captured = {}
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: captured.setdefault("url", url))

    result = {}

    def run():
        result["token"] = auth._run_login_flow_sync("http://fake-app-base")

    t = threading.Thread(target=run)
    t.start()

    deadline = time.monotonic() + 3
    while "url" not in captured and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "url" in captured, "listener never announced its callback URL"

    match = re.search(r"callback=http://127\.0\.0\.1:(\d+)/callback&state=([\w-]+)", captured["url"])
    port, state = match.group(1), match.group(2)

    # An invalid request must not consume the listener's only shot at answering.
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?state=wrong&token=bogus", timeout=2)
    except urllib.error.HTTPError:
        pass  # the 400 is expected; the point under test is that the listener survives it
    assert t.is_alive(), "an invalid request killed the listener before the real callback arrived"

    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?state={state}&token=komori_real_token", timeout=2)
    t.join(timeout=8)
    assert result.get("token") == "komori_real_token"
