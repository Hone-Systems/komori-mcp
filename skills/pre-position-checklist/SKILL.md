---
name: pre-position-checklist
description: Use before deciding whether to look further into a Japanese-listed company — builds a checklist of what to verify from its actual filing history, not a recommendation. Trigger on "should I look into X", "what should I check before I decide on X", "pre-position checklist for X".
---

# Pre-position checklist

The point of this skill is **what to go verify**, never **what to decide**. Nothing here says buy,
sell, or "looks good" — it hands back a list of things a careful reader would check, each backed by
a real quote and a real date, so the reader can judge for themselves.

## Steps

1. Resolve the company. If you only have a name, call `search` to get its 4-character ticker.
2. Call `get_company_threads(ticker)` with no filters first — it's free to look at every thread's
   summary (title, kind, category, how many events). Scan for threads that look load-bearing:
   repeated targets, risk threads with several events, anything whose `moves` list shows
   SOFTENED or DETERIORATED.
3. For every thread that looks load-bearing, call `get_thread(ticker, thread_id)` — this is where
   the unit gets spent, so be selective; 3-5 threads is usually enough for a checklist, not all of
   them.
4. For each thread's full event history, write one checklist line per pattern you find:
   - **A target repeated across consecutive filings** — "this margin-recovery target has appeared
     in N consecutive filings, most recently dated {date}: {quote}."
   - **A move that changed direction** — "as of {date}, wording on {topic} shifted from {earlier
     quote} to {quote} — worth reading the surrounding filing for why."
   - **Something introduced recently with no history yet** — "first appears in the {date} filing,
     nothing to compare it against yet."
5. Pull `get_filing_analysis` for the most recent filing that touched the most significant thread —
   the rubric dimensions and "own words" section often name exactly what changed and why.
6. Close with the disclaimer every tool result already carries (`notice` field) — don't drop it
   when you synthesize.

## Output shape

A short list, one line per thing to verify, each with: **what**, **since when**, **the exact
quote**, and a **pointer** (`ticker`/`slug` or `ticker`/`thread_id`) so the reader can pull the
primary source themselves. Never a summary judgment on the company as a whole.

## What NOT to do

- Don't call `get_company_threads` with `ids` for every thread just to be thorough — that's N
  units for information the free summary call already gave you. Filter with `move`/`kind` first,
  or eyeball the summary list, before spending a unit on any individual thread.
- Don't turn a pattern into a verdict ("this is concerning") — turn it into a question ("this is
  worth checking: has this margin target held up in the filing after this one?").
