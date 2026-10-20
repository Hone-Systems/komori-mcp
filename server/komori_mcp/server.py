"""The Komori MCP server (#490).

A thin, stateless wrapper over the Komori external API (`https://api.komori.app/v1`, #275/#593) —
list-then-drill company research for the Japanese market: filings, 変遷 narrative threads, scenario
trees, signals, news, semantic search over what a company actually wrote in its filings.

Every tool is a direct mapping to one `/v1` endpoint (docs/TOOL-DESIGN.md §1 — this surface is
already shaped as one job per route, so a tool-per-route is the right granularity here, not a
mode-switch over near-duplicates). This module owns tool descriptions, parameter validation the
API itself would reject anyway (so the model gets a fast, cheap error), and threading the API's own
`notice` field, unit-cost headers and the komori.app source page into every result. It does not
re-implement metering, tiering, or response shaping — `/v1` already does all three.

TWO TRANSPORTS, ONE TOOL SET
----------------------------
The tools are plain module functions, registered onto a FastMCP server by `build_server`. The
module-level `mcp` is the local (stdio) server with every tool. The hosted server (`http.py`, senki
#1074) builds its own with the read tools only — read-only is the launch scope — and swaps the token
source: locally the token comes from the keychain and a browser login; hosted, it is the exchanged
`/v1` credential for the caller whose OAuth token the request carried. There is no path from the
hosted server to the keychain login.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from opentelemetry import metrics, trace

from komori_mcp import pages
from komori_mcp.auth import clear_stored_token, get_token
from komori_mcp.client import KomoriApiError, KomoriClient, KomoriResult

INSTRUCTIONS = (
    "Komori is a Japan-equities disclosure research tool. Every result carries a standing "
    "notice: this is a research aid, not financial advice. Present findings as things to go "
    "look at — what changed, what a company said, what a filing shows — never as a "
    "recommendation. No buy/sell language, no bear cases on individual names. "
    "Every result also carries `source` — the page on komori.app it came from. When you use a "
    "finding, cite that link and credit it as \"Komori (komori.app)\" so the reader can check it. "
    "Call `whoami` first in a new session to see the account's remaining unit budget."
)

_client = KomoriClient(token_provider=get_token)

# Set by `use_http_tokens` for the hosted server. None = local mode: a 401 clears the stored token
# and retries once through a fresh browser login.
_on_unauthorized: Callable[[], Awaitable[None]] | None = None

_tracer = trace.get_tracer("komori_mcp")
_meter = metrics.get_meter("komori_mcp")
_tool_calls = _meter.create_counter("komori.mcp.tool_calls", description="MCP tool calls by tool and outcome")
_tool_duration = _meter.create_histogram("komori.mcp.tool_duration", unit="s", description="MCP tool call duration")


def use_http_tokens(token_provider: Callable[[], Awaitable[str]], on_unauthorized: Callable[[], Awaitable[None]]) -> None:
    """Switch this process to hosted mode. Called once by `http.create_app()`."""
    global _on_unauthorized
    _client._token_provider = token_provider
    _on_unauthorized = on_unauthorized


async def _call(fn, *args, **kwargs) -> dict[str, Any]:
    """Runs one API call and shapes the result. Locally, a revoked or expired token is retried
    exactly once through a fresh browser login; hosted, the caller's connection is the credential,
    so a 401 means it was revoked mid-request and the client has to reconnect. Every failure —
    including one on the retry itself, or the login flow timing out — surfaces a clear, actionable
    message (docs/TOOL-DESIGN.md §6) rather than an uncaught exception escaping to the MCP
    transport. `args[0]`, when present, is the `/v1` path: it names the result's komori.app page."""
    path = args[0] if args and isinstance(args[0], str) else None
    params = kwargs.get("params")
    retried = False
    while True:
        try:
            result = await fn(*args, **kwargs)
            return _shape(result, path, params)
        except KomoriApiError as exc:
            if exc.status_code == 401 and _on_unauthorized is not None:
                await _on_unauthorized()
                return _error(401, "Komoriとの接続が切れました。クライアントからKomoriに接続し直してください。", path, params)
            if exc.status_code == 401 and not retried:
                retried = True
                clear_stored_token()
                continue  # one retry, through a fresh login
            return _error(exc.status_code, exc.detail, path, params)
        except TimeoutError as exc:
            return {"is_error": True, "error": str(exc), "source": pages.source(None, path, params)}


def _error(status_code: int, detail: str, path: str | None, params: dict | None) -> dict[str, Any]:
    return {"is_error": True, "status_code": status_code, "error": detail, "source": pages.source(None, path, params)}


def _shape(result: KomoriResult, path: str | None = None, params: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"data": result.data}
    if result.notice:
        out["notice"] = result.notice
    if result.units_charged is not None:
        out["_units"] = {"charged": result.units_charged, "remaining": result.units_remaining}
    out["source"] = pages.source(result.data, path, params)
    return out


def _instrumented(fn):
    """A span and two metrics per tool call. `opentelemetry-api` only: with no SDK installed (a local
    stdio install) every call here is a no-op; the hosted image installs the SDK first. Never the
    token, never a user id on a metric label."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        started = time.monotonic()
        with _tracer.start_as_current_span("mcp.tool") as span:
            span.set_attribute("mcp.tool.name", fn.__name__)
            result = await fn(*args, **kwargs)
            outcome = "error" if isinstance(result, dict) and result.get("is_error") else "ok"
            span.set_attribute("mcp.tool.outcome", outcome)
            if isinstance(result, dict):
                if result.get("status_code") is not None:
                    span.set_attribute("http.status_code", result["status_code"])
                if result.get("_units"):
                    span.set_attribute("komori.units_charged", result["_units"].get("charged") or 0)
            _tool_calls.add(1, {"tool": fn.__name__, "outcome": outcome})
            _tool_duration.record(time.monotonic() - started, {"tool": fn.__name__})
            return result

    return wrapper


# --- Identity / discovery -----------------------------------------------------------------------


async def whoami() -> dict:
    """Your account, tier, and remaining unit budget. Call this first in a new session — it's
    free and tells you how much room you have before spending units on threads, filing analyses,
    scenarios, or section searches."""
    return await _call(_client.get, "/me")


async def search(q: str, limit: int = 20) -> dict:
    """Lexical search across company names, threads (titles only), filings, signals, and news.
    Free. Good for finding an identifier (a ticker, a thread id, a filing slug) to drill into —
    NOT for checking whether a company's filings say something specific. For that, use
    `search_filing_sections`, which searches actual filing text, not titles."""
    return await _call(_client.get, "/search", params={"q": q, "limit": limit})


async def search_filing_sections(
    q: str,
    tickers: str,
    object_type: str | None = None,
    doc_type: str | None = None,
    filed_after: str | None = None,
    filed_before: str | None = None,
    top_n: int = 10,
) -> dict:
    """Semantic search over what these companies actually WROTE in their filings — the tool for
    checking a claim against the primary source, not `search` (which only indexes thread titles).

    `tickers` is REQUIRED, and that's a capability statement, not a limitation: a search across
    the whole corpus doesn't work (times out), but one company answers in under a second — so ask
    per company, up to 10 tickers at once (comma-separated).

    LATENCY: this takes seconds, not milliseconds — it's a real scan on another machine, not a
    lookup. A slow response is normal, not a hang.

    ON A 503: do NOT retry. The scan is still running; a retry within a few minutes joins the same
    in-flight scan rather than starting a new one, so retrying immediately only lengthens the
    queue. Wait, then retry once.

    A zero-hit result can mean "no passage matches" OR "this company isn't indexed yet" — the API
    doesn't currently distinguish the two, so don't report a zero-hit result as a confirmed
    negative finding without noting that caveat.

    `object_type` narrows to one of: supply_chain, products, technologies, capital_allocation,
    company_bets, external_exposures, capability_building, regulatory_positioning.
    """
    params: dict[str, Any] = {"q": q, "tickers": tickers, "top_n": top_n}
    for key, val in (
        ("object_type", object_type),
        ("doc_type", doc_type),
        ("filed_after", filed_after),
        ("filed_before", filed_before),
    ):
        if val is not None:
            params[key] = val
    return await _call(_client.get, "/sections/search", params=params)


async def get_trending(kind: str | None = None) -> dict:
    """What's moving right now across companies, filings, threads, signals, and news. Free. Good
    cold-start entry point when you don't have a specific company in mind yet. Optionally narrow
    with `kind` via `/trending/taxonomy/{kind}`."""
    if kind:
        return await _call(_client.get, f"/trending/taxonomy/{kind}")
    return await _call(_client.get, "/trending")


async def get_changes(since: str | None = None, limit: int = 50) -> dict:
    """What has changed since a cursor — new filings, disclosures, threads, news, signal moves.
    Free. Pass back the `next_cursor` from a prior call to page forward; omitting `since` resumes
    from where this token last left off, but store the cursor yourself if your process might die
    mid-page — the server's memory of your position is a convenience, not a guarantee."""
    params: dict[str, Any] = {"limit": limit}
    if since:
        params["since"] = since
    return await _call(_client.get, "/changes", params=params)


# --- Companies ------------------------------------------------------------------------------------


async def list_companies(limit: int = 50, page: int = 1, sector: str | None = None) -> dict:
    """The company universe as identifiers — ticker, names, sector. Free, cheap. Use `search`
    instead if you're looking for a specific company by name."""
    params: dict[str, Any] = {"limit": limit, "page": page}
    if sector:
        params["sector"] = sector
    return await _call(_client.get, "/companies", params=params)


async def get_company(ticker: str) -> dict:
    """One company's identity (name, sector). Free. `ticker` is always the 4-character TSE code
    (e.g. `7203`), never a 5-character code."""
    return await _call(_client.get, f"/companies/{ticker}")


# --- Filings --------------------------------------------------------------------------------------


async def list_filings(limit: int = 30, before: str | None = None, since: str | None = None, ticker: str | None = None) -> dict:
    """The filing/disclosure feed, optionally scoped to one company and/or a date window. Free.
    Each row carries a `slug` (EDINET filings) or a `disclosure_no` (TDnet disclosures) — that's
    what you pass to `get_filing`/`get_filing_analysis`/`get_filing_scenario`."""
    params: dict[str, Any] = {"limit": limit}
    for key, val in (("before", before), ("since", since), ("ticker", ticker)):
        if val is not None:
            params[key] = val
    return await _call(_client.get, "/filings", params=params)


async def get_filing(ticker: str, slug: str) -> dict:
    """Metadata for one filing (title, doc type, filed date). Free. For the actual analysis
    (rubric scores, own-words, what changed), use `get_filing_analysis` — that one costs a unit."""
    return await _call(_client.get, f"/companies/{ticker}/filings/{slug}")


async def get_filing_analysis(ticker: str, slug: str) -> dict:
    """What Komori concluded about one filing: a rubric score across several dimensions (change
    intensity, strategic shift, capital action, etc.), the company's own words on key topics, and
    what moved since the prior filing. Costs 1 unit — free forever after the first read, and free
    if the company is on your watchlist."""
    return await _call(_client.get, f"/companies/{ticker}/filings/{slug}/analysis")


async def get_filing_scenario(ticker: str, slug: str) -> dict:
    """The cause-and-effect scenario tree read off one filing: a thesis, the mechanism behind it,
    the filing text it rests on, and named consequences with which other companies are exposed
    and how. Costs 1 unit under the same free-forever/watchlist-exempt rule as filing analysis."""
    return await _call(_client.get, f"/companies/{ticker}/filings/{slug}/scenario")


# --- 変遷 threads -----------------------------------------------------------------------------


async def get_company_threads(
    ticker: str,
    ids: str | None = None,
    move: str | None = None,
    kind: str | None = None,
    latest_move: str | None = None,
    since: str | None = None,
    themes: bool = False,
) -> dict:
    """The company's 変遷 threads — narrative topics tracked across its filing/disclosure history
    (a margin-recovery thread, a risk that's been building, a target that's quietly lost its
    date). Costs 1 unit for the company, charged once no matter how many times you re-read or
    filter it — narrowing with `move`/`kind`/`latest_move`/`since` costs exactly the same as
    fetching everything, so always narrow when you can; there's no reason to fetch all 100+
    threads to find the four you actually want.

    Pass `themes=True` instead to get the cross-company THEMES this company sits in — the same
    management subject resolved across many filers, with how THIS company relates to it and in
    which direction (e.g. the leaked-customer-data theme and whether this company files it as a
    headwind). Costs the same 1 unit for the company, free if it is on your watchlist. The
    thread filters/`ids` do not apply in themes mode — passing any of them with `themes=True` is
    an error, not a silent no-op. An empty list means the company is in no themes (or is not
    covered by the grouping lane yet) — not a confirmed negative.

    `move` matches ANY event in a thread's history; `latest_move` matches only the most recent
    one — different questions (comma-separated or repeated, both accepted). Valid moves include
    INTRODUCED, RAISED, SOFTENED, DETERIORATED, REAFFIRMED, ACHIEVED, and others — an unknown
    value 400s with the full list rather than silently returning nothing, so a rejected filter
    means "check your spelling," not "nothing matched."

    `since` accepts a fiscal year (`FY2020`, `FY2020-Q3`) or a calendar date.

    Pass `ids` (comma-separated thread ids) instead of a filter to fetch specific threads in full
    (1 unit each) — `ids` and the filters can't be combined in one call.
    """
    if themes:
        if any(v is not None for v in (ids, move, kind, latest_move, since)):
            # The same error shape `_call` returns, so the model sees one contract.
            return {
                "is_error": True,
                "error": "themes=True のときはスレッドの絞り込み（ids / move / kind / "
                "latest_move / since）は指定できません。テーマを取得する場合は ticker だけを"
                "指定してください。",
                "source": pages.source(None, f"/companies/{ticker}/themes"),
            }
        return await _call(_client.get, f"/companies/{ticker}/themes")
    params: dict[str, Any] = {}
    for key, val in (("ids", ids), ("move", move), ("kind", kind), ("latest_move", latest_move), ("since", since)):
        if val is not None:
            params[key] = val
    return await _call(_client.get, f"/companies/{ticker}/threads", params=params)


async def get_thread(ticker: str, thread_id: str) -> dict:
    """One thread's full quote history — every event, dated, with the company's verbatim words
    and a plain-language note on what changed. Costs 1 unit under the usual free-forever rule."""
    return await _call(_client.get, f"/companies/{ticker}/threads/{thread_id}")


# --- Cross-company themes (#634) ---------------------------------------------------------------


async def list_themes(
    min_reach: int = 10,
    episodic: bool | None = None,
    section: str | None = None,
    q: str | None = None,
    limit: int = 50,
    skip: int = 0,
) -> dict:
    """The cross-company themes index — one management subject resolved across many companies
    ("leaked customer data" as ONE theme spanning 173 filers, with the direction each filed it
    in). Free. Themes are ranked by how many companies are in each; drill into one with
    `get_theme(theme_id)`.

    `min_reach` is the minimum number of companies a theme must span; the default floor of 10
    keeps one- and two-company groups off the list (they are not themes yet) — lower it to widen.

    `section` narrows to one index partition: `contested` (at least 3 companies filing it as
    helping AND at least 3 as hurting), `housekeeping`, `episodic` (tied to a dated event), or
    `broad`.

    `q` searches theme names and descriptions, within the reach floor already applied. A
    zero-hit `q` means no theme matches the term — not an error.
    """
    params: dict[str, Any] = {"min_reach": min_reach, "limit": limit, "skip": skip}
    for key, val in (("episodic", episodic), ("section", section), ("q", q)):
        if val is not None:
            params[key] = val
    return await _call(_client.get, "/themes", params=params)


async def get_theme(
    theme_id: str,
    limit: int = 50,
    skip: int = 0,
    relation: str | None = None,
    polarity: str | None = None,
    sector: str | None = None,
) -> dict:
    """One theme in full: the theme itself, the member companies (paginated), the per-sector /
    per-channel / per-year breakdowns, and neighbouring themes. Costs 1 unit per theme — free
    forever after the first read, and a 180-member theme costs the same as a 3-member one (the
    member list is one object, paged, not many objects). No watchlist exemption: a theme spans
    many companies, so there is no single ticker to exempt.

    `theme_id` is the opaque id from `list_themes`.

    `relation`, `polarity` and `sector` narrow the MEMBER list only, never the theme; a filtered
    page reports what the filter matched. `polarity` values: POSITIVE / NEGATIVE / NEUTRAL.
    """
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    for key, val in (("relation", relation), ("polarity", polarity), ("sector", sector)):
        if val is not None:
            params[key] = val
    return await _call(_client.get, f"/themes/{theme_id}", params=params)


# --- News / signals ---------------------------------------------------------------------------


async def list_news(limit: int = 30, ticker: str | None = None) -> dict:
    """The news feed, optionally scoped to one company. Free."""
    params: dict[str, Any] = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return await _call(_client.get, "/news", params=params)


async def get_news_story(story_id: str) -> dict:
    """One full news story. Costs 1 unit under the usual free-forever rule."""
    return await _call(_client.get, f"/news/{story_id}")


async def list_signals() -> dict:
    """The signal catalog — every Komori signal's key, name, and category. Free."""
    return await _call(_client.get, "/signals")


async def get_signal(key: str) -> dict:
    """One signal's description and stats (IC, sharpe, hit rate). Costs 1 unit. This is catalog
    detail, not the per-company audit trail — it doesn't say what a specific company scores."""
    return await _call(_client.get, f"/signals/{key}")


# --- Market -----------------------------------------------------------------------------------


async def get_market_summary() -> dict:
    """Current market state — indices, sector performance. Free."""
    return await _call(_client.get, "/market/summary")


# --- Watchlists (the only writes) -------------------------------------------------------------


async def list_watchlists() -> dict:
    """Your saved watchlists. Free. Every company on any of your watchlists is exempt from
    metering everywhere else in this API — adding a company here is how you make its threads,
    filing analyses, and scenarios free to re-read."""
    return await _call(_client.get, "/watchlists")


async def create_watchlist(name: str, tickers: str | None = None) -> dict:
    """Create a watchlist, optionally pre-populated with companies (comma-separated tickers —
    resolve names to tickers with `search` first if you only have names). Free. Creating a
    populated list also starts a backtest and a recap in the background, same as doing it on
    komori.app — the response includes their job ids."""
    body: dict[str, Any] = {"name": name}
    if tickers:
        body["tickers"] = [t.strip() for t in tickers.split(",") if t.strip()]
    return await _call(_client.post, "/watchlists", body)


async def add_to_watchlist(watchlist_id: str, ticker: str) -> dict:
    """Add one company to a watchlist. Free, idempotent — adding a company already on the list is
    a no-op, not an error."""
    return await _call(_client.put, f"/watchlists/{watchlist_id}/companies/{ticker}")


# --- Watchlist notifications (one tool per kind) -------------------------------------------------
#
# One tool per KIND of notification rather than a single mode-switch tool (docs/TOOL-DESIGN.md §1's
# default), by the product owner's explicit ask: "notify me about news on my list" should map to one
# obviously-named tool. All of them hit `PUT /v1/watchlists/{id}/notifications/{kind}`, which merges
# into the list's rule server-side — nothing is validated or merged here.


def _switch(watchlist_id: str, kind: str, body: dict[str, Any]):
    return _call(_client.put, f"/watchlists/{watchlist_id}/notifications/{kind}", body)


async def get_watchlist_notifications(watchlist_id: str) -> dict:
    """What notifications are switched on for one of your watchlists: documents and news (list-wide
    and/or per company), the daily performance and risk thresholds, and `delivery` — whether they
    will actually be delivered (account-wide switch, email address on file, suppressed email). Free.
    `watchlist_id` is the `id` from `list_watchlists`, not the list's name."""
    return await _call(_client.get, f"/watchlists/{watchlist_id}/notifications")


async def set_watchlist_document_notifications(watchlist_id: str, enabled: bool, ticker: str | None = None) -> dict:
    """Switch notifications for NEW DOCUMENTS — statutory filings (有価証券報告書, 四半期/半期報告書)
    and timely disclosures (適時開示 such as 決算短信) — on or off for one of your watchlists. Free.
    Emailed in a batch at most once an hour.

    Without `ticker` it applies to every company on the list; with `ticker` (4-digit code, must
    already be on the list) only to that company. Switching OFF without a ticker also clears any
    per-company document notifications on that list. If `coverage_note` comes back, the list-wide
    setting already covers that company, so the per-company switch changes nothing.

    The result's `delivery` says whether anything will actually arrive: if `delivery.note` is
    set, tell the user plainly (for example, notifications are switched off for the whole
    account). Never call `set_notification_delivery` unless the user asks for that.
    """
    body: dict[str, Any] = {"enabled": enabled}
    if ticker:
        body["ticker"] = ticker
    return await _switch(watchlist_id, "documents", body)


async def set_watchlist_news_notifications(watchlist_id: str, enabled: bool, ticker: str | None = None) -> dict:
    """Switch NEWS notifications (news stories naming a company on the list) on or off for one of
    your watchlists. Free. Emailed in a batch at most every six hours.

    Without `ticker` it applies to every company on the list; with `ticker` (4-digit code, must
    already be on the list) only to that company. Switching OFF without a ticker also clears any
    per-company news notifications on that list. If `coverage_note` comes back, the list-wide
    setting already covers that company, so the per-company switch changes nothing.

    The result's `delivery` says whether anything will actually arrive: if `delivery.note` is
    set, tell the user plainly (for example, notifications are switched off for the whole
    account). Never call `set_notification_delivery` unless the user asks for that.
    """
    body: dict[str, Any] = {"enabled": enabled}
    if ticker:
        body["ticker"] = ticker
    return await _switch(watchlist_id, "news", body)


async def set_watchlist_performance_notification(
    watchlist_id: str, enabled: bool, percent: float | None = None, direction: str | None = None
) -> dict:
    """Switch the daily PERFORMANCE threshold on or off for one of your watchlists. Free.

    Performance is the list's one-day price change as a whole, in PERCENT with its sign.
    `direction="above"` fires when it rises above `percent`; `"below"` fires when it falls to or
    below `percent`. A FALL needs a NEGATIVE number: "tell me if my list drops 3% in a day" is
    percent=-3, direction="below"; "if it gains more than 2%" is percent=2, direction="above".
    `percent` and `direction` are required when enabling.

    Checked once a day after the close; it fires on the day the value CROSSES to that side of
    the line, not again on each day it stays there, and the first reading after saving only sets
    the baseline. Setting it again replaces the previous threshold. To switch off, pass
    enabled=false alone.

    The result's `delivery` says whether anything will actually arrive: if `delivery.note` is
    set, tell the user plainly (for example, notifications are switched off for the whole
    account). Never call `set_notification_delivery` unless the user asks for that.
    """
    return await _switch(watchlist_id, "performance", _threshold_body(enabled, percent, direction))


async def set_watchlist_risk_notification(
    watchlist_id: str, enabled: bool, percent: float | None = None, direction: str | None = None
) -> dict:
    """Switch the daily RISK threshold on or off for one of your watchlists. Free.

    Risk is the list's annualised volatility in PERCENT (25 means 25%; typical lists sit around
    15-40). "Tell me if my list's risk goes above 30%" is percent=30, direction="above"; "below"
    fires when it falls to or below `percent`. `percent` and `direction` are required when
    enabling.

    Checked once a day after the close; it fires on the day the value CROSSES to that side of
    the line, not again on each day it stays there, and the first reading after saving only sets
    the baseline. Setting it again replaces the previous threshold. To switch off, pass
    enabled=false alone.

    The result's `delivery` says whether anything will actually arrive: if `delivery.note` is
    set, tell the user plainly (for example, notifications are switched off for the whole
    account). Never call `set_notification_delivery` unless the user asks for that.
    """
    return await _switch(watchlist_id, "risk", _threshold_body(enabled, percent, direction))


def _threshold_body(enabled: bool, percent: float | None, direction: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"enabled": enabled}
    if enabled:
        body.update({"percent": percent, "direction": direction})
    return body


async def set_notification_delivery(enabled: bool) -> dict:
    """The ACCOUNT-WIDE notifications switch: off means no notification from any list reaches you
    (neither the komori.app inbox nor email), whatever the list settings say. Free. Call it ONLY when
    the user explicitly asks to turn all their notifications on or off, or agrees after being told it
    is off — never as a side effect of switching a list's notification on. Returns the new
    `delivery` status."""
    return await _call(_client.patch, "/notifications/settings", {"enabled": enabled})


# --- Registration -----------------------------------------------------------------------------------

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# (function, human title, annotations). Order is the order `tools/list` returns.
READ_TOOLS: list[tuple[Callable, str]] = [
    (whoami, "Account and unit budget"),
    (search, "Search Komori"),
    (search_filing_sections, "Search filing text"),
    (get_trending, "What is moving now"),
    (get_changes, "Changes since a cursor"),
    (list_companies, "List companies"),
    (get_company, "Company identity"),
    (list_filings, "Filings and disclosures feed"),
    (get_filing, "Filing metadata"),
    (get_filing_analysis, "Filing analysis"),
    (get_filing_scenario, "Filing scenario tree"),
    (get_company_threads, "Company 変遷 threads or themes"),
    (get_thread, "One 変遷 thread"),
    (list_themes, "Cross-company themes"),
    (get_theme, "One theme"),
    (list_news, "News feed"),
    (get_news_story, "One news story"),
    (list_signals, "Signal catalog"),
    (get_signal, "One signal"),
    (get_market_summary, "Market summary"),
    (list_watchlists, "Your watchlists"),
    (get_watchlist_notifications, "Watchlist notification settings"),
]

WRITE_TOOLS: list[tuple[Callable, str, ToolAnnotations]] = [
    (create_watchlist, "Create a watchlist", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)),
    (add_to_watchlist, "Add a company to a watchlist", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)),
    (set_watchlist_document_notifications, "Document notifications on/off", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)),
    (set_watchlist_news_notifications, "News notifications on/off", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)),
    (set_watchlist_performance_notification, "Performance threshold", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)),
    (set_watchlist_risk_notification, "Risk threshold", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)),
    (set_notification_delivery, "Account-wide notification switch", ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)),
]


def build_server(include_writes: bool, **settings: Any) -> FastMCP:
    """A FastMCP server carrying the read tools, plus the write tools when `include_writes`."""
    server = FastMCP("komori", instructions=INSTRUCTIONS, **settings)
    for fn, title in READ_TOOLS:
        server.add_tool(_instrumented(fn), name=fn.__name__, title=title, annotations=_READ)
    if include_writes:
        for fn, title, annotations in WRITE_TOOLS:
            server.add_tool(_instrumented(fn), name=fn.__name__, title=title, annotations=annotations)
    return server


mcp = build_server(include_writes=True)
