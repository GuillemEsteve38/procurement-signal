# Scope — Procurement Signal (TED ingestion + requirement extraction)

**Status:** week 1 of 2 · **Owner:** Guillem Esteve · **Last updated:** 2026-10-23

## The client (fictional, deliberately)

"Sanitas Digital" — a 25-person Spanish software vendor selling clinical and
administrative applications to regional health authorities (Servicio Madrileño de
Salud, Servei Català de la Salut, SAS, etc.).

Today one person spends roughly a day a week manually checking the TED portal and
PLACSP for tenders worth bidding on. They miss notices, they find out about
deadlines late, and they have no history to answer "which buyers actually award to
companies our size?"

This client is fictional, but **the requirements are not invented**: they are read
off real public tender documents on TED and PLACSP. Where a decision depends on a
requirement nobody wrote down, that assumption is recorded in the Assumptions
section below rather than silently resolved.

## The problem, stated as the client would

> We need to know, the morning it is published, about every tender in Spain that
> involves health-sector software — what it is, who is buying, how much it is
> worth, and when the deadline is. And we need to be able to look back over the
> last two years and see who buys what.

## Success metrics

| # | Metric | Target (week 2) |
|---|---|---|
| 1 | Notices matching the profile that reach the database | ≥ 99% of what the TED API returns for the same query |
| 2 | Freshness | New notices visible within 24h of TED publication |
| 3 | Extraction accuracy on the eval set (budget, deadline, CPV, lot count) | ≥ 90% exact match on 50 hand-labelled notices |
| 4 | Cost per 100 notices extracted | Measured and reported in the README (no target — it must simply be known) |
| 5 | Ingestion is restartable | A run killed halfway re-runs with no duplicates and no gaps |

Metric 5 is the one that actually decides whether this is a toy. Everything in
`store.py` is built around it.

## In scope

- Ingest TED notices for a configurable profile (CPV families + buyer country + date window)
- Normalize the hostile parts of the payload into a queryable schema
- Retain the raw API response per notice, forever, alongside the normalized row
- LLM extraction of structured requirements from the free-text description
- An eval harness with a versioned case set, scoring accuracy, cost and latency
- A search + filter UI good enough to demo in 3 minutes
- A deployable container with env-var config and a health endpoint

## Out of scope (explicitly, so it stays out)

- PLACSP ingestion — TED already covers above-threshold Spanish notices, and a
  second source doubles the normalization work for marginal coverage. Revisit only
  if contracts below the EU threshold turn out to matter.
- Bid writing, document generation, or anything that produces a submission
- User accounts, multi-tenancy, billing
- Full-text ingestion of tender PDFs (the notice metadata and descriptions are
  enough to decide whether a tender is worth opening)
- Real-time push notifications — a daily digest is sufficient and far cheaper

## Assumptions

1. **Health-sector relevance is approximated by CPV family, not by buyer type.**
   CPV 72xxxxxx (IT services) + 48xxxxxx (software packages) filtered to
   `buyer-country=ESP`, then narrowed by keyword. A real client would hand over a
   list of target buyers; absent one, CPV is the defensible proxy.
2. **Award and modification notices are kept, not filtered out.** They are useless
   for bidding but they are the entire basis of "who buys what" (metric, backlog
   item 3). `notice_type` is stored so the UI can filter them out of the bid feed.
3. **Multilingual titles collapse to Spanish, falling back to English.** TED
   returns up to 24 translations per field; storing all of them would triple the
   database for no decision-relevant information. The raw payload keeps the rest.

## Data source — verified 2026-10-23

`POST https://api.ted.europa.eu/v3/notices/search` — **no API key, no auth**.

- Expert query language: `classification-cpv=72000000 AND buyer-country=ESP AND publication-date>=20260901 SORT BY publication-number DESC`
- `paginationMode: "ITERATION"` returns an `iterationNextToken` for scroll pagination
- **1833 selectable field names.** There is no documented field list and no metadata
  endpoint (`/v3/notices/fields` → 400). The way to get the list is to request a
  bogus field: the 400 body enumerates every supported value. `scripts/dump_fields.py`
  does this and writes `docs/ted_fields.txt`.
- Errors are structured, with `type`, `fieldName`, and line/column positions — good
  enough to distinguish a bad query (fix the code) from a transient failure (retry).
- Query volume at the time of writing: 1198 notices matching the profile since
  2026-09-01; ~402k historical notices under CPV 72 alone.

### Why this payload is the interesting part

A single real notice (737081-2026, Servicio Madrileño de Salud, €7,053,801.73):

```
notice-title         = {"spa": "...", "hun": "...", ...22 more languages}
publication-date     = "2026-10-23+02:00"          <- a date carrying a UTC offset
total-value          = 7053801.73                   <- currency is a *separate* field
total-value-cur      = ["EUR"]                      <- ...and it is an array
place-of-performance = ["ES300","ESP","ES300","ES300","ES300","ESP","ESP","ESP"]
classification-cpv   = ["72000000","72212900","72000000", ...]   <- per-lot, with repeats
description-lot      = {"spa": ["lot 1 text", "lot 2 text", "lot 3 text"]}
buyer-identifier     = ["50494010087781", "Q2822001J"]   <- two different ID schemes
```

Every text field is a language-keyed map. Array cardinality varies per lot and
carries meaningless repetition. Value and currency live in different fields with
different shapes. Dates are not dates.

Turning that into something queryable **is** the engineering work, and it is the
part that most closely resembles real deployed work against an enterprise system.

## Week plan

**Week 1 — ingestion (this week)**
- [x] Verify the data source, enumerate the field vocabulary
- [x] Scope doc
- [ ] API client: expert-query builder, scroll pagination, retry/backoff, structured error handling
- [ ] Normalizer: language collapse, cardinality collapse, date and money parsing, anomaly flagging
- [ ] Store: raw + normalized tables, idempotent upsert, ingest run log
- [ ] Backfill the profile and report row counts + anomaly counts

**Week 2 — extraction, eval, demo**
- [ ] Extraction via the Claude API with structured output (budget, deadline, lot count, required certifications, submission language)
- [ ] Eval harness: 50 hand-labelled notices, per-field scoring, cost and latency per run
- [ ] FastAPI + HTMX search UI
- [ ] Dockerfile, env-var config, `/healthz`, runbook
- [ ] 3-minute demo recording + a "what broke" note

## Known risks

| Risk | Mitigation |
|---|---|
| TED field semantics are undocumented; a field may not mean what its name suggests | Raw payload is retained, so any mis-mapping is re-derivable without re-fetching |
| Query returns more than the scroll token will page through | Ingest is windowed by publication date; a window that hits the cap is split and retried |
| LLM extraction is confidently wrong on Spanish legal phrasing | Eval set is built before the extractor is tuned, and includes deliberately adversarial notices |
| Windows `cp1252` default encoding silently corrupts Spanish text | Every file open is explicitly UTF-8; there is a regression test for it |
