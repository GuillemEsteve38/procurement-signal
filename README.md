# Procurement Signal

Ingests EU public procurement notices from **TED** (Tenders Electronic Daily) for
a configurable market profile, normalizes a deliberately hostile payload into a
queryable schema, and keeps the raw API response forever so a wrong mapping costs
a re-parse rather than a re-crawl.

Built as a forward-deployed-engineering practice project. The client is fictional
(a Spanish health-sector software vendor); the data, the API quirks, and the
requirements are real. See [SCOPE.md](SCOPE.md) for the one-page spec.

**Status:** ingestion and the extraction eval harness work end to end against
live data. The search UI and deployment are the remaining pieces.

---

## Current state, measured

A real backfill of the `spanish-health-it` profile, run 2026-10-23:

```
window            2026-06-25 .. 2026-10-23 (120 days, 9 chunks)
fetched           2764 notices -> inserted 2764, updated 0, unchanged 0
re-run of last    332 notices  -> inserted 0, updated 0, unchanged 332   (idempotent)
re-ingest after   2764 notices -> inserted 0, updated 2386, unchanged 378
 adding 10 fields                (schema migration + content-hash both exercised)
keyword-relevant  424
with EUR value    2321  (EUR 19.6bn in aggregate)
database size     42.9 MB (raw payloads retained)
wall clock        ~25s including one 429 that retried successfully
field drift       0 unexpected API fields
failed runs       0
```

Field-level coverage on those 2764 rows: title, description, publication date,
buyer name and main CPV are present on **100%**; deadline on 1000 (award and
modification notices legitimately carry none); zero non-EUR currencies in this
profile.

Anomalies raised, all of them real properties of the data rather than bugs:

| Anomaly | Notices | What it means |
|---|---|---|
| `multi_buyer_id_scheme` | 1834 | The buyer carries two identifiers under different national schemes (a DIR3 code and a Spanish NIF). Entity resolution cannot key on one identifier. |
| `no_value` | 443 | No contract value published at any level — common on prior-information notices. |
| `deadline_before_publication` | 20 | Award notices quoting the original tender deadline. Expected here; would be a data error on a contract notice. |

---

## Quick start

```sh
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .          # Linux/macOS: .venv/bin/python

# 30-day ingest of the default profile into data/procurement.sqlite3
.venv/Scripts/python.exe -m procsignal.ingest --days 30

# larger backfill, chunked so it resumes cleanly
.venv/Scripts/python.exe -m procsignal.ingest --since 2026-01-01 --window-days 14

# fetch and normalize without writing notice rows
.venv/Scripts/python.exe -m procsignal.ingest --days 7 --max-pages 1 --dry-run

.venv/Scripts/python.exe -m unittest discover -s tests     # 77 tests
```

No API key is required — the TED Search API is public.

## Extraction and eval

```sh
# build the stratified eval case set from the ingested database
.venv/Scripts/python.exe -m procsignal.evaluate build --limit 50

# score an extractor (stub needs no API key and costs nothing)
.venv/Scripts/python.exe -m procsignal.evaluate run --extractor stub
.venv/Scripts/python.exe -m procsignal.evaluate run --extractor claude --effort medium

# label the three fields TED has no counterpart for
.venv/Scripts/python.exe scripts/label_cases.py --stratum keyword_false_positive_candidate

# run extraction across the database and persist it
.venv/Scripts/python.exe -m procsignal.enrich --extractor stub --limit 20
```

Methodology is in **[docs/EVAL.md](docs/EVAL.md)**, including the design error
the harness caught before any model spend. Summary of that episode:

> The original design extracted budget and submission deadline from the notice
> text and scored them against TED's structured fields. The first baseline run
> found a budget in 2 of 50 cases and a deadline in **0 of 50**. Measuring the
> input explained it — `description-proc` is a 269-character subject line, not
> the tender dossier, and several notices explicitly say the requirements live
> in a separate PPTP document TED does not carry. Both fields were
> unextractable by construction, and both were already published structured.
>
> The fix was to ask TED for the fields that *do* carry requirement prose
> (award criteria 81% populated on open contract notices, selection criteria
> 70%, exclusion grounds 64%) and to re-scope extraction onto what the text
> supports. This overturned the SCOPE.md assumption that notice metadata alone
> was sufficient.

Current baseline — `StubExtractor`, regexes only, no API key, zero cost:

| field | n | recall | wrong-value | abstain |
|---|---:|---:|---:|---:|
| `cpv_division` | 50 | 56.0% | 44.0% | 0.0% |
| `contract_nature` | 50 | 76.0% | 17.4% | 8.0% |
| `lot_count` | 50 | 92.0% | 8.0% | 0.0% |

`is_health_related`, `work_category` and `required_certifications` have no
structured counterpart in TED and are unscored until labelled — which is why
they are the fields worth extracting. **No Claude numbers are recorded yet:**
there is no Anthropic credential on the machine this was built on. The code
path exists and is exercised by the stub; `--extractor claude` produces the
real figures once a key is set.

## Browsing the data

`scripts/inspect_db.py` opens the database read-only using Python's built-in
`sqlite3` module, so no separate SQLite install is needed.

```sh
.venv/Scripts/python.exe scripts/inspect_db.py                    # overview
.venv/Scripts/python.exe scripts/inspect_db.py schema notices     # column list
.venv/Scripts/python.exe scripts/inspect_db.py runs               # ingest history + coverage gaps
.venv/Scripts/python.exe scripts/inspect_db.py anomalies          # anomaly counts, schema drift
.venv/Scripts/python.exe scripts/inspect_db.py top --open-only    # biggest open tenders
.venv/Scripts/python.exe scripts/inspect_db.py notice 737081-2026 # one normalized row
.venv/Scripts/python.exe scripts/inspect_db.py raw 737081-2026    # ...and its raw payload
.venv/Scripts/python.exe scripts/inspect_db.py sql "SELECT buyer_name, COUNT(*) n FROM notices GROUP BY 1 ORDER BY n DESC LIMIT 10"
```

The `sql` subcommand accepts only `SELECT`/`WITH`/`PRAGMA`/`EXPLAIN`, and the
connection is opened with `mode=ro`, so a stray query cannot damage the ingest
database.

For a GUI, [DB Browser for SQLite](https://sqlitebrowser.org/) opens
`data/procurement.sqlite3` directly.

---

## What the data actually looks like

This is the part worth reading. A single real notice (737081-2026, Servicio
Madrileño de Salud, €7,053,801.73, three lots), abridged:

```json
{
  "notice-title":         {"spa": "...", "hun": "...", "eng": "...", ...22 more},
  "publication-date":     "2026-10-23+02:00",
  "total-value":          7053801.73,
  "total-value-cur":      ["EUR"],
  "place-of-performance": ["ES300","ESP","ES300","ES300","ES300","ESP","ESP","ESP"],
  "classification-cpv":   ["72000000","72212900","72000000","72000000", ...],
  "description-lot":      {"spa": ["lot 1 text", "lot 2 text", "lot 3 text"]},
  "buyer-identifier":     ["50494010087781", "Q2822001J"]
}
```

Six distinct problems in nine lines:

1. **Every text field is a language map** of up to 24 translations, whose values
   are sometimes a string and sometimes a list (one entry per lot).
2. **`publication-date` is a calendar date carrying a UTC offset.**
   `date.fromisoformat("2026-10-23+02:00")` raises; `datetime.fromisoformat`
   accepts it only by accident of version. Both are traps.
3. **Amount and currency are separate fields with different shapes** — a bare
   float and a one-element list.
4. **Array fields repeat values once per lot.** `["ES300","ESP","ES300",...]` is
   two distinct places, not eight.
5. **Per-lot arrays can disagree on how many lots exist**, so lot count has to be
   derived from the widest of them.
6. **The same buyer has several identifiers under different national schemes**, so
   there is no single join key for entity resolution.

`src/procsignal/normalize.py` handles all six. Two rules govern it:

- **Never drop a notice because a field surprised us.** Anything unexpected is
  recorded as an anomaly code on the row. The only fatal condition is a missing
  `publication-number`, because without the primary key there is nothing to
  attach the anomaly to.
- **Never guess silently.** Derived values carry their provenance:
  `value_source` records whether a figure is the awarded total, a
  procedure-level estimate, or a sum across lots. A sum across *mixed
  currencies* is refused outright rather than stored as a plausible-looking lie.

---

## Notes on the TED API, since they are not documented anywhere

**Endpoint.** `POST https://api.ted.europa.eu/v3/notices/search` — public, no API
key, no auth header.

**Expert query language.** Field-operator-value joined with `AND`/`OR`, with an
optional trailing sort:

```
(classification-cpv=72000000 OR classification-cpv=48000000)
  AND (buyer-country=ESP)
  AND publication-date>=20260901 AND publication-date<=20261023
  SORT BY publication-number DESC
```

Dates are bare `YYYYMMDD`, *not* ISO-8601. CPV must be the full 8 digits — the
2-digit family prefix is rejected with `QUERY_UNSUPPORTED_FIELD_VALUE`, but
hierarchical matching is implicit, so `72000000` also matches `72212900`.

**`fields` x `limit` must be <= 10,000.** Undocumented, and the error only
appears once you ask for enough fields: 45 fields at a page size of 250 is
11,250 and returns `SEARCH_FIELDS_PER_PAGE_EXCEEDS_MAX_LIMIT`. `TedClient`
derives the page size from the field count rather than making the caller
redo that arithmetic every time a field is added.

**Pagination.** Set `paginationMode: "ITERATION"` and follow the opaque
`iterationNextToken`. Page size up to 250. The scroll ends when the token comes
back null; the last page is empty rather than short. A response can also carry
`timedOut: true`, which means the server truncated the result set — this client
raises on it rather than yielding a short page, because a silently short page
looks exactly like a successful run that lost notices.

**There is no field list and no metadata endpoint.** `/v3/notices/fields` returns
400. But the API validates the `fields` parameter against an allow-list and puts
*the entire allow-list* into the 400 body — so requesting a field that cannot
exist is a documentation endpoint in disguise. `scripts/dump_fields.py` does
exactly that and writes all **1833** selectable field names to
[`docs/ted_fields.txt`](docs/ted_fields.txt).

**Errors are well structured**, in two different shapes. Expert-query errors
return a dict with `type`, `fieldName`, `fieldValue`, and the line/column of the
offending token; request-validation errors return a list of field/message pairs.
That distinction is load-bearing: a 4xx that is not rate limiting means *our
request is wrong* and retrying just repeats the mistake, so the client raises
`TedQueryError` immediately and only retries 408/425/429/5xx with jittered
exponential backoff. The 429 in the backfill above was recovered this way.

---

## Layout

```
src/procsignal/
  profile.py     market definitions; compiles to a TED expert query
  ted.py         API client: retries, scroll pagination, structured errors
  normalize.py   the hostile-payload transform (the actual engineering)
  store.py       SQLite: raw + normalized tables, idempotent upsert, run log
  ingest.py      CLI: date windowing, chunking, reporting
  extract.py     extraction schema, Claude extractor, regex baseline, pricing
  evaluate.py    case-set builder, scorer, run reporting
  enrich.py      CLI: run extraction across the database, resumably
scripts/
  dump_fields.py enumerate the 1833 selectable API fields
  inspect_db.py  read-only database browser (no sqlite3 CLI required)
  label_cases.py label the eval fields TED has no ground truth for
tests/           39 tests, fixtures copied from real API responses
docs/
  ted_fields.txt generated field vocabulary
```

### Database

| Table | Purpose |
|---|---|
| `raw_notices` | Verbatim API payload per notice, plus content hash and fetch count |
| `notices` | The normalized, indexed, queryable row |
| `ingest_runs` | One row per attempt, written at start and updated at finish — a crashed run leaves a `running` row rather than no trace |
| `field_observations` | How often each API key has been seen and when first seen — a key appearing for the first time months in is how a schema change announces itself |
| `extractions` | One row per notice per extractor/model/effort/prompt-version, so two configurations can be compared on the same notice instead of overwriting each other |

Writes are keyed on `publication_number` and compare a sorted-key content hash,
so re-ingesting unchanged data is counted as `unchanged`. The normalized row is
still rewritten, so new relevance keywords or a normalizer fix reach existing
notices without a re-crawl, and `first_seen_at` never drifts. `last_seen_at` is bumped regardless, which is
how "TED still lists this notice" is distinguishable from "we have not looked
since March".

---

## Known limitations

**Keyword relevance has false positives.** The `is_relevant` flag is substring
matching, and it is wrong often enough to matter: a €58.7m *road pavement
maintenance* contract from Palma de Mallorca is tagged health-relevant because
the word "salud" appears somewhere in its description. 118 of 424 relevant
notices match on the single keyword "salud" alone. This is the specific problem
week 2's extraction step and eval set exist to measure — and it is why the flag
is stored rather than used to filter at ingest time. Filtering at read time is
reversible; filtering at ingest time is not.

**Notice titles are often useless.** Real examples: `Contrato de Servicios
(CONTR 2026 0000253907)`. The decision-relevant content is in the description,
which is why extraction operates on the description rather than the title.

**`estimated-value-proc` was empty across all 2764 notices in this sample.**
Either Spanish notices do not populate it or the field means something other
than its name suggests. The raw payloads are retained, so this is answerable
without re-crawling — but it is currently an open question, not a finding.

**The real tender requirements are in a document TED does not carry.** Notice
metadata carries award and selection criteria as short clauses, which is
enough to classify a tender but not to answer "can we actually meet this spec".
That lives in the PPTP, a separate PDF on the buyer's own portal. Ingesting
those is a genuine scope expansion — a per-buyer scraper plus PDF extraction —
and it is deliberately still out of scope. The notice-level fields are enough
for the triage decision the client actually asked for.

**Lot-cardinality and lot-sum code paths are unit-tested but not yet exercised
by real data.** They did not fire once in 2764 notices. They may be dead code
for this profile.

---

## Roadmap

**Done**
- [x] Requirement extraction via the Claude API with JSON-schema-constrained output
- [x] Eval harness: stratified 50-case set, per-field scoring that separates abstention from error, cost and latency per run
- [x] Deterministic regex baseline so the model's numbers have a floor to beat
- [x] Labelling CLI for the three fields TED has no ground truth for

**Next**
- [ ] Label the 50 cases and record the first Claude run (needs an API key)
- [ ] Effort sweep: `low` / `medium` / `high` on the same case set, with cost
- [ ] FastAPI + HTMX search UI
- [ ] Dockerfile, env-var config, `/healthz`, runbook
- [ ] Demo recording and a "what broke" note

**Backlog**
- [ ] Daily incremental run on a schedule
- [ ] Buyer entity resolution across the two identifier schemes
- [ ] "Who buys what" analysis over award notices
- [ ] Alerting on a saved profile

---

## Environment note

Python on Windows defaults file and stdout encoding to the ANSI code page
(cp1252 here), which raises `UnicodeEncodeError` on ordinary accented Spanish.
Every file handle in this project is explicitly UTF-8, `ingest.py` reconfigures
stdio on startup, and there is a regression test that round-trips `FACTORÍA`
through SQLite.
