---
name: claim-audit
description: Use when the user brings in an outside claim about a Japanese-listed company — their own thesis, a broker note, a media article — and wants it checked against the company's actual filings. Trigger on "audit this claim", "is this true about X", "check this thesis against the filings", "fact-check this note".
---

# Claim audit

Sort every claim into exactly one of three buckets. Never say who's right — only what the filings
do and don't say.

- **Supported** — a filing passage says this, close to directly.
- **Extrapolation** — a filing passage is related, but the claim goes further than what's written.
- **Not addressed** — no passage found; note whether the search actually covered this company (see
  the coverage caveat below) before treating that as a real negative.

## Steps

1. Break the incoming claim into its individual factual assertions — one sentence, one company
   each, usually. A claim spanning several companies needs one pass per company.
2. Resolve each company to its ticker (`search` if you only have a name).
3. For each assertion, call `search_filing_sections(q=<the assertion as a question or key phrase>,
   tickers=<the one ticker>)`. This is the ONLY tool for this job — `search` indexes thread titles,
   not filing text, and will make you think a claim is unaddressed when it's actually right there
   in the filing.
   - This call takes a few seconds, not milliseconds. That's normal.
   - If it 503s, the search is still running — wait, don't immediately retry (a retry inside a few
     minutes joins the same scan; retrying instantly just queues behind it).
   - `object_type` can narrow the search if you know which kind of statement you're checking
     (e.g. `capital_allocation` for a capex claim, `external_exposures` for a supply-chain claim).
4. Read the returned excerpts and their `score`. A high-scoring, closely-matching excerpt supports
   the claim; a lower-scoring, tangentially-related one is the extrapolation case; no hits (after
   confirming this company is actually indexed — a repeat search with a very generic query for the
   same ticker should return *something* if it's indexed) is the not-addressed case.
5. For every claim you can rule on, cite the exact excerpt, its `filed_date`, and the `slug` —
   that's the audit trail; without it, the verdict is an assertion, not a check. A reader should be
   able to call `get_filing(ticker, slug)` themselves and land on the source.

## Output shape

One line per claim: **the claim** → **bucket** → **quoted excerpt + date + slug**, or **not
addressed** with the coverage caveat noted if relevant. No overall verdict on the outside source's
credibility — that's the reader's call, this only checks the individual factual assertions.

## What NOT to do

- Don't run a corpus-wide search — `search_filing_sections` requires `tickers` for a reason (an
  unscoped search doesn't complete). Always resolve to a specific company first.
- Don't treat a zero-hit result as proof the filings are silent on something without noting that
  the API can't currently distinguish "no match" from "this company isn't indexed yet."
- Don't rephrase the claim into something easier to confirm — audit what was actually said.
