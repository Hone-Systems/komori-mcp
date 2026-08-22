---
name: language-drift
description: Use to track how a Japanese-listed company's wording on one specific commitment or risk has shifted across its filing history — where hedging crept in, where a target quietly lost its date, where a risk moved from footnote to headline. Trigger on "how has X's language on Y changed", "language drift", "has this commitment softened", "track this thread over time".
---

# Language drift

Single-company, single-thread, forensic — no cross-referencing against other companies needed. The
whole analysis rides on one structural fact: every thread event already carries an explicit `move`
classification (INTRODUCED, RAISED, SOFTENED, DETERIORATED, REAFFIRMED, ACHIEVED, and others), so
you're reading a labeled sequence, not inferring drift from prose.

## Steps

1. Resolve the company (`search` if needed) and find the thread. If you already know roughly what
   it's about, `get_company_threads(ticker, kind=..., move=SOFTENED)` or similar narrows straight
   to candidates — narrowing costs the same 1 unit as fetching everything, so always narrow when
   you have any idea what you're looking for.
2. Call `get_thread(ticker, thread_id)` for the full event history.
3. Walk the events **in order**. For each one, note: the date (`when`), the `move`, the verbatim
   `quote`, and the plain-language `note`. The `move` sequence itself is the drift — e.g.
   INTRODUCED → REAFFIRMED → REAFFIRMED → SOFTENED tells the story before you even compare the
   quotes.
4. Compare quotes across consecutive SOFTENED/DETERIORATED events specifically for:
   - **Hedging creeping in** — a flat statement gaining qualifiers ("を目指す" turning into
     "を目指していく方針である" turning into language that drops a firm target for a directional
     one).
   - **A date quietly disappearing** — an earlier quote names a fiscal year or date, a later one
     restates the same commitment without it.
   - **Section movement** — if `get_filing_analysis` is available for the relevant filings, check
     whether the topic moved between rubric dimensions or changed weight/prominence between
     periods.
5. Present the sequence chronologically, quote-by-quote, letting the reader see the drift happen —
   don't just state a conclusion.

## Output shape

A dated timeline: `date — move — quote`, for the full thread history, with a one-line note calling
out exactly where hedging or a lost date appears in the sequence. Cite `ticker`/`thread_id` (and
`ticker`/`slug` for any filing referenced) so it's checkable against the source.

## What NOT to do

- Don't summarize away the individual quotes — the verbatim wording IS the finding here; a
  paraphrase defeats the point of a language-drift analysis.
- Don't editorialize on why the language changed unless a filing explicitly says so — describe the
  shift, don't speculate about motive.
