"""The Komori MCP server (#490).

A thin, stateless wrapper over the Komori external API (`https://api.komori.app/v1`, #275/#593) —
list-then-drill company research for the Japanese market: filings, 変遷 narrative threads, scenario
trees, signals, news, semantic search over what a company actually wrote in its filings.

Every tool is a direct mapping to one `/v1` endpoint (docs/TOOL-DESIGN.md §1 — this surface is
already shaped as one job per route, so a tool-per-route is the right granularity here, not a
mode-switch over near-duplicates). This module owns tool descriptions, parameter validation the
API itself would reject anyway (so the model gets a fast, cheap error), and threading the API's own
`notice` field and unit-cost headers into every result. It does not re-implement metering, tiering,
or response shaping — `/v1` already does all three.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from komori_mcp.auth import clear_stored_token, get_token
from komori_mcp.client import KomoriApiError, KomoriClient, KomoriResult

mcp = FastMCP(
    "komori",
    instructions=(
        "Komori is a Japan-equities disclosure research tool. Every result carries a standing "
        "notice: this is a research aid, not financial advice. Present findings as things to go "
        "look at — what changed, what a company said, what a filing shows — never as a "
        "recommendation. No buy/sell language, no bear cases on individual names. "
        "Call `whoami` first in a new session to see the account's remaining unit budget."
    ),
)

_client = KomoriClient(token_provider=get_token)


async def _call(fn, *args, **kwargs) -> dict[str, Any]:
    """Runs one API call, retrying exactly once through a fresh browser-login if the stored
    token was revoked or expired. Every result is shaped the same way so the model always finds
    `notice` and unit cost in the same place, and every failure surfaces the API's own message
    (docs/TOOL-DESIGN.md §6) rather than a generic wrapper error."""
    try:
        result = await fn(*args, **kwargs)
    except KomoriApiError as exc:
        if exc.status_code == 401:
            clear_stored_token()
            result = await fn(*args, **kwargs)  # one retry, through a fresh login
        else:
            return {"is_error": True, "status_code": exc.status_code, "error": exc.detail}
    return _shape(result)


def _shape(result: KomoriResult) -> dict[str, Any]:
    out: dict[str, Any] = {"data": result.data}
    if result.notice:
        out["notice"] = result.notice
    if result.units_charged is not None:
        out["_units"] = {"charged": result.units_charged, "remaining": result.units_remaining}
    return out


# --- Identity / discovery -----------------------------------------------------------------------


@mcp.tool()
async def whoami() -> dict:
    """Your account, tier, and remaining unit budget. Call this first in a new session — it's
    free and tells you how much room you have before spending units on threads, filing analyses,
    scenarios, or section searches."""
    return await _call(_client.get, "/me")


@mcp.tool()
async def search(q: str, limit: int = 20) -> dict:
    """Lexical search across company names, threads (titles only), filings, signals, and news.
    Free. Good for finding an identifier (a ticker, a thread id, a filing slug) to drill into —
    NOT for checking whether a company's filings say something specific. For that, use
    `search_filing_sections`, which searches actual filing text, not titles."""
    return await _call(_client.get, "/search", params={"q": q, "limit": limit})


@mcp.tool()
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


@mcp.tool()
async def get_trending(kind: str | None = None) -> dict:
    """What's moving right now across companies, filings, threads, signals, and news. Free. Good
    cold-start entry point when you don't have a specific company in mind yet. Optionally narrow
    with `kind` via `/trending/taxonomy/{kind}`."""
    if kind:
        return await _call(_client.get, f"/trending/taxonomy/{kind}")
    return await _call(_client.get, "/trending")


@mcp.tool()
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


@mcp.tool()
async def list_companies(limit: int = 50, page: int = 1, sector: str | None = None) -> dict:
    """The company universe as identifiers — ticker, names, sector. Free, cheap. Use `search`
    instead if you're looking for a specific company by name."""
    params: dict[str, Any] = {"limit": limit, "page": page}
    if sector:
        params["sector"] = sector
    return await _call(_client.get, "/companies", params=params)


@mcp.tool()
async def get_company(ticker: str) -> dict:
    """One company's identity (name, sector). Free. `ticker` is always the 4-character TSE code
    (e.g. `7203`), never a 5-character code."""
    return await _call(_client.get, f"/companies/{ticker}")


# --- Filings --------------------------------------------------------------------------------------


@mcp.tool()
async def list_filings(limit: int = 30, before: str | None = None, since: str | None = None, ticker: str | None = None) -> dict:
    """The filing/disclosure feed, optionally scoped to one company and/or a date window. Free.
    Each row carries a `slug` (EDINET filings) or a `disclosure_no` (TDnet disclosures) — that's
    what you pass to `get_filing`/`get_filing_analysis`/`get_filing_scenario`."""
    params: dict[str, Any] = {"limit": limit}
    for key, val in (("before", before), ("since", since), ("ticker", ticker)):
        if val is not None:
            params[key] = val
    return await _call(_client.get, "/filings", params=params)


@mcp.tool()
async def get_filing(ticker: str, slug: str) -> dict:
    """Metadata for one filing (title, doc type, filed date). Free. For the actual analysis
    (rubric scores, own-words, what changed), use `get_filing_analysis` — that one costs a unit."""
    return await _call(_client.get, f"/companies/{ticker}/filings/{slug}")


@mcp.tool()
async def get_filing_analysis(ticker: str, slug: str) -> dict:
    """What Komori concluded about one filing: a rubric score across several dimensions (change
    intensity, strategic shift, capital action, etc.), the company's own words on key topics, and
    what moved since the prior filing. Costs 1 unit — free forever after the first read, and free
    if the company is on your watchlist."""
    return await _call(_client.get, f"/companies/{ticker}/filings/{slug}/analysis")


@mcp.tool()
async def get_filing_scenario(ticker: str, slug: str) -> dict:
    """The cause-and-effect scenario tree read off one filing: a thesis, the mechanism behind it,
    the filing text it rests on, and named consequences with which other companies are exposed
    and how. Costs 1 unit under the same free-forever/watchlist-exempt rule as filing analysis."""
    return await _call(_client.get, f"/companies/{ticker}/filings/{slug}/scenario")


# --- 変遷 threads -----------------------------------------------------------------------------


@mcp.tool()
async def get_company_threads(
    ticker: str,
    ids: str | None = None,
    move: str | None = None,
    kind: str | None = None,
    latest_move: str | None = None,
    since: str | None = None,
) -> dict:
    """The company's 変遷 threads — narrative topics tracked across its filing/disclosure history
    (a margin-recovery thread, a risk that's been building, a target that's quietly lost its
    date). Costs 1 unit for the company, charged once no matter how many times you re-read or
    filter it — narrowing with `move`/`kind`/`latest_move`/`since` costs exactly the same as
    fetching everything, so always narrow when you can; there's no reason to fetch all 100+
    threads to find the four you actually want.

    `move` matches ANY event in a thread's history; `latest_move` matches only the most recent
    one — different questions (comma-separated or repeated, both accepted). Valid moves include
    INTRODUCED, RAISED, SOFTENED, DETERIORATED, REAFFIRMED, ACHIEVED, and others — an unknown
    value 400s with the full list rather than silently returning nothing, so a rejected filter
    means "check your spelling," not "nothing matched."

    `since` accepts a fiscal year (`FY2020`, `FY2020-Q3`) or a calendar date.

    Pass `ids` (comma-separated thread ids) instead of a filter to fetch specific threads in full
    (1 unit each) — `ids` and the filters can't be combined in one call.
    """
    params: dict[str, Any] = {}
    for key, val in (("ids", ids), ("move", move), ("kind", kind), ("latest_move", latest_move), ("since", since)):
        if val is not None:
            params[key] = val
    return await _call(_client.get, f"/companies/{ticker}/threads", params=params)


@mcp.tool()
async def get_thread(ticker: str, thread_id: str) -> dict:
    """One thread's full quote history — every event, dated, with the company's verbatim words
    and a plain-language note on what changed. Costs 1 unit under the usual free-forever rule."""
    return await _call(_client.get, f"/companies/{ticker}/threads/{thread_id}")


# --- News / signals ---------------------------------------------------------------------------


@mcp.tool()
async def list_news(limit: int = 30, ticker: str | None = None) -> dict:
    """The news feed, optionally scoped to one company. Free."""
    params: dict[str, Any] = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return await _call(_client.get, "/news", params=params)


@mcp.tool()
async def get_news_story(story_id: str) -> dict:
    """One full news story. Costs 1 unit under the usual free-forever rule."""
    return await _call(_client.get, f"/news/{story_id}")


@mcp.tool()
async def list_signals() -> dict:
    """The signal catalog — every Komori signal's key, name, and category. Free."""
    return await _call(_client.get, "/signals")


@mcp.tool()
async def get_signal(key: str) -> dict:
    """One signal's description and stats (IC, sharpe, hit rate). Costs 1 unit. This is catalog
    detail, not the per-company audit trail — it doesn't say what a specific company scores."""
    return await _call(_client.get, f"/signals/{key}")


# --- Market -----------------------------------------------------------------------------------


@mcp.tool()
async def get_market_summary() -> dict:
    """Current market state — indices, sector performance. Free."""
    return await _call(_client.get, "/market/summary")


# --- Watchlists (the only writes) -------------------------------------------------------------


@mcp.tool()
async def list_watchlists() -> dict:
    """Your saved watchlists. Free. Every company on any of your watchlists is exempt from
    metering everywhere else in this API — adding a company here is how you make its threads,
    filing analyses, and scenarios free to re-read."""
    return await _call(_client.get, "/watchlists")


@mcp.tool()
async def create_watchlist(name: str, tickers: str | None = None) -> dict:
    """Create a watchlist, optionally pre-populated with companies (comma-separated tickers —
    resolve names to tickers with `search` first if you only have names). Free. Creating a
    populated list also starts a backtest and a recap in the background, same as doing it on
    komori.app — the response includes their job ids."""
    body: dict[str, Any] = {"name": name}
    if tickers:
        body["tickers"] = [t.strip() for t in tickers.split(",") if t.strip()]
    return await _call(_client.post, "/watchlists", body)


@mcp.tool()
async def add_to_watchlist(watchlist_id: str, ticker: str) -> dict:
    """Add one company to a watchlist. Free, idempotent — adding a company already on the list is
    a no-op, not an error."""
    return await _call(_client.put, f"/watchlists/{watchlist_id}/companies/{ticker}")
