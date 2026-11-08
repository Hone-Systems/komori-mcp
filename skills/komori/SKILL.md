---
name: komori
description: Research Japanese-listed companies (TSE / 東証) from their primary disclosures with the Komori MCP server or REST API — EDINET filings (有価証券報告書, 四半期報告書), TDnet disclosures (決算短信, 適時開示), 変遷 narrative threads that track a commitment across years of filings, filing analyses, scenario trees, themes, news, and semantic search over filing text. Use when the user asks about a Japanese stock, a 4-digit TSE ticker, a Japanese company's filings or guidance, what changed in a disclosure, or wants a claim checked against what the company actually wrote.
---

# Komori — Japan-equities disclosure research

[Komori](https://komori.app) reads every EDINET filing and TDnet disclosure from Japanese-listed
companies and turns them into structured, citable research: what the company said, when it said
it, and how the wording moved over time. This skill tells you how to connect to it and how to use
it well.

It is a research aid, not financial advice. Present findings as things to go look at, never as
recommendations. Don't use buy/sell language.

## 1. Connect

Check whether Komori tools (`whoami`, `search`, `get_company_threads`, …) are already available.
If they are, skip to step 2.

If not, install the MCP server. It's free with a komori.app account. The first tool call opens
komori.app in the browser to log in, so there's no token to paste.

**Claude Code** (plugin, which also bundles these skills):
```
/plugin marketplace add Hone-Systems/komori-mcp
/plugin install komori
```

**Codex**:
```
codex plugin marketplace add Hone-Systems/komori-mcp
codex plugin add komori
```

**Cursor / Windsurf / any stdio MCP client** (needs [uv](https://docs.astral.sh/uv/)):
```json
{
  "mcpServers": {
    "komori": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Hone-Systems/komori-mcp#subdirectory=server", "komori-mcp"]
    }
  }
}
```

VS Code and Zed config shapes are in the
[komori-mcp README](https://github.com/Hone-Systems/komori-mcp#install). After adding the
server, the user has to restart or reload the client so it picks up the tools.

**REST instead of MCP.** Every tool maps to one endpoint under `https://api.komori.app/v1`. The
full contract is at `GET https://api.komori.app/v1/openapi.json`. Send
`Authorization: Bearer <token>` with a personal API token the user creates at komori.app → 設定 →
APIトークン. Never ask the user to paste a token into chat. Read it from an env var they set.
Responses are wrapped in `{"data": ...}`. The `X-Komori-Units-Charged` and
`X-Komori-Units-Remaining` headers report metering.

## 2. Ground rules

- **Call `whoami` first** in a new session. It's free and returns the account tier and remaining
  unit budget.
- **Tickers are the 4-character TSE code** (`7203` for Toyota, `285A` for newer listings), never
  the 5-digit code. If you only have a name, resolve it with `search`.
- **Cite the `source`.** Every result carries `source`, the komori.app page it came from. When
  you use a finding, link it and credit it as "Komori (komori.app)" so the reader can check it.
- **Keep the `notice`.** Every result carries a standing not-advice notice. Don't drop it when
  you summarize.
- **Quote, don't paraphrase,** when the wording is the finding. Japanese filing language is
  hedged on purpose. 「目指す」 and 「目指していく方針」 are different commitments.

## 3. What costs a unit

Most reads are free. A few cost 1 unit. Company-scoped unit reads are free forever on re-read,
and free for any company on one of the user's watchlists. Themes span many companies, so the
watchlist exemption doesn't apply to them.

| Free | 1 unit |
|---|---|
| `whoami`, `search`, `list_companies`, `get_company`, `list_filings`, `get_filing`, `list_news`, `list_signals`, `get_trending`, `get_changes`, `get_market_summary`, `list_themes`, watchlist tools | `get_company_threads` (once per company, any filters), `get_thread`, `get_filing_analysis`, `get_filing_scenario`, `search_filing_sections`, `get_news_story`, `get_signal`, `get_theme` |

If the user is researching a handful of names repeatedly, suggest putting them on a watchlist
(`create_watchlist` / `add_to_watchlist`). After that, reads on those companies stop costing units.

## 4. Which tool for which question

| Question | Tool |
|---|---|
| "Find company X" / name → ticker | `search(q)` |
| "What did they file recently?" | `list_filings(ticker=…)` → `get_filing(ticker, slug)` |
| "What changed in this filing?" | `get_filing_analysis(ticker, slug)`, which returns rubric scores, the company's own words, and what moved since the prior filing |
| "What's the cause-and-effect story in this filing?" | `get_filing_scenario(ticker, slug)` |
| "How has their story on Y evolved?" | `get_company_threads(ticker, kind=…, move=…)` → `get_thread(ticker, thread_id)` |
| "Does the filing actually say Z?" | `search_filing_sections(q, tickers=…)`, a semantic search over filing **text** (`search` only indexes titles) |
| "Which companies share this issue?" | `list_themes` → `get_theme`, or `get_company_threads(ticker, themes=True)` |
| "What's moving / what's new?" | `get_trending`, `get_changes(since=…)`, `list_news` |

### 変遷 threads (the core object)

A thread is one narrative topic, such as a margin target, a risk, or a capex plan, tracked across
every filing that touches it. Each event has a date (`when`), a verbatim `quote`, a
plain-language `note`, and a `move`: `INTRODUCED`, `RAISED`, `REAFFIRMED`, `SOFTENED`,
`DETERIORATED`, `ACHIEVED`, and others. The move sequence is the story. A thread that goes
`INTRODUCED → REAFFIRMED → SOFTENED` tells you where to look before you read a single quote.

- `move` matches any event in a thread's history. `latest_move` matches only the most recent one.
- `since` takes `FY2020`, `FY2020-Q3`, or a calendar date.
- Filters cost the same single unit as fetching everything, so always narrow.
- An unknown `move` value returns 400 with the valid list. It does not return empty.

### `search_filing_sections` specifics

- `tickers` is required (up to 10, comma-separated). An unscoped corpus-wide search times out.
- It takes seconds, so a slow answer is normal. **On a 503, don't retry immediately.** The scan is
  still running, and a retry inside a few minutes joins it. Wait, then retry once.
- Zero hits can mean "not said" or "not indexed yet". Say so instead of reporting a confirmed
  negative.
- `object_type` narrows to: `supply_chain`, `products`, `technologies`, `capital_allocation`,
  `company_bets`, `external_exposures`, `capability_building`, `regulatory_positioning`.

## 5. Workflows

Three companion skills in this repo cover the common research jobs end to end:

- **`claim-audit`**: check a thesis, broker note, or article against the filings, sorting each
  claim into supported, extrapolation, or not addressed.
- **`language-drift`**: trace how one commitment's wording shifted across a company's filing
  history.
- **`pre-position-checklist`**: before digging into a name, build a list of what to verify from
  its thread history.

Install all of them with `npx skills add Hone-Systems/komori-mcp`.

## Output

Lead with the finding, then the evidence: the quote, its date, and the `source` link. Use
Japanese for quotes and English (or the user's language) for your explanation. End with the
not-advice notice. Never close with a verdict on whether the stock is good.
