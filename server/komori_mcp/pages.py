"""Where on komori.app a tool result lives — the link-back every result carries (senki#1074).

Komori publishes this server so that research done in an agent leads people back to the page it came
from. Every tool result therefore names its source page. The API's own canonical URL wins when the
object has one; otherwise the page is derived from the `/v1` path the tool called, which is the one
thing every tool has in common — so there is a single mapping here instead of one per tool.

Links carry a campaign tag so komori.app can count the visits this server sends; the `/v1` URLs
themselves stay canonical.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

SITE = "https://komori.app"
ATTRIBUTION = "Komori (komori.app)"
_UTM = {"utm_source": "komori-mcp", "utm_medium": "mcp"}

# `/v1` path -> komori.app page. First match wins, so the more specific patterns come first.
_ROUTES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^/companies/(?P<t>[^/]+)/filings/(?P<slug>[^/]+)(/(analysis|scenario))?$"), "/company/{t}/filing/{slug}"),
    (re.compile(r"^/companies/(?P<t>[^/]+)/disclosures/(?P<no>[^/]+)$"), "/company/{t}/disclosure/{no}"),
    (re.compile(r"^/companies/(?P<t>[^/]+)/threads/(?P<id>[^/]+)$"), "/company/{t}/threads/{id}"),
    (re.compile(r"^/companies/(?P<t>[^/]+)(/.*)?$"), "/company/{t}"),
    (re.compile(r"^/themes/(?P<id>[^/]+)$"), "/themes/{id}"),
    (re.compile(r"^/themes$"), "/themes"),
    (re.compile(r"^/news/(?P<id>[^/]+)$"), "/news/{id}"),
    (re.compile(r"^/news$"), "/news"),
    (re.compile(r"^/signals/(?P<key>[^/]+)$"), "/signals/{key}"),
    (re.compile(r"^/signals$"), "/signals"),
    (re.compile(r"^/filings$"), "/filings"),
    (re.compile(r"^/watchlists(/.*)?$"), "/watchlist"),
    (re.compile(r"^/notifications(/.*)?$"), "/notifications"),
    (re.compile(r"^/me$"), "/settings"),
    (re.compile(r"^/companies$"), "/screener"),
]


def tagged(url: str) -> str:
    """Add the campaign tag to a komori.app URL, keeping any query it already has."""
    sep = "&" if urlsplit(url).query else "?"
    return f"{url}{sep}{urlencode(_UTM)}"


def page_for(path: str, params: dict | None = None) -> str:
    """The komori.app page for a `/v1` call. Unknown paths land on the home page, never nowhere."""
    params = params or {}
    if path == "/search" and params.get("q"):
        return f"{SITE}/search?{urlencode({'q': params['q']})}"
    if path in ("/filings", "/news") and params.get("ticker"):
        return f"{SITE}/company/{quote(str(params['ticker']), safe='')}"
    if path == "/sections/search" and params.get("tickers"):
        first = str(params["tickers"]).split(",")[0].strip()
        return f"{SITE}/company/{quote(first, safe='')}"
    for pattern, template in _ROUTES:
        match = pattern.match(path)
        if match:
            return SITE + template.format(**{k: quote(v, safe="") for k, v in match.groupdict().items() if v})
    return f"{SITE}/"


def _is_komori(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == urlsplit(SITE).netloc


def source(data: Any, path: str | None, params: dict | None = None) -> dict[str, str]:
    """The `source` block of a result. `komori_url` first (news carries the PUBLISHER's article in
    `url`, so `url` is only trusted when it is itself on komori.app), then the path mapping."""
    url = None
    if isinstance(data, dict):
        if _is_komori(data.get("komori_url")):
            url = data["komori_url"]
        elif _is_komori(data.get("url")):
            url = data["url"]
    if url is None:
        url = page_for(path, params) if path else f"{SITE}/"
    return {"name": ATTRIBUTION, "url": tagged(url)}
