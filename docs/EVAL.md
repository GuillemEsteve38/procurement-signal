# Extraction eval methodology

Written before the extractor was tuned. That ordering is deliberate, and it
earned its keep immediately: **the first eval run killed half of my original
extraction design before a single model call was paid for.** That episode is
documented below rather than tidied away, because it is the most useful thing
in this file.

## The design error the eval caught

The original plan extracted four fields with "free" ground truth — budget,
submission deadline, lot count, contract nature — by withholding the TED
structured fields that already hold them and scoring the model's reading of
the free text against those. Elegant on paper.

Then the deterministic baseline found a budget in 2 of 50 cases and a deadline
in 0 of 50. Measuring the input directly explained why:

```
TED description-proc, 50 sampled notices
  median length                               269 chars
  contains a money cue (€/eur/importe)          2/50
  contains a date cue (date/plazo/fecha)        0/50
  contains any 4+ digit number                  7/50
```

`description-proc` is a **short subject line, not the tender dossier.** Some
notices say so outright: *"Véase a estos efectos el pliego de prescripciones
técnicas particulares (PPTP)"* — see the separate technical specification
document, which TED does not carry.

So budget and deadline were unextractable **by construction**. Not a model
limitation; the information was never in the input. And both are already
published as structured TED fields, which made extracting them pointless
twice over. An eval written after the fact would have shown a bad score and
invited prompt-tuning against a question that could not be answered.

### What that overturned

SCOPE.md put full-text PDF ingestion out of scope on the grounds that "the
notice metadata and descriptions are enough to decide whether a tender is
worth opening." That assumption was wrong, and the fix had two parts.

**First, ask TED for the right fields.** There was richer text available; the
original profile just never requested it. Measured on 250 Spanish CPV-72
notices, restricted to open contract notices (`cn-*`) because those are what a
bidder acts on:

| Field | Populated | Median chars |
|---|---|---|
| `award-criterion-description-lot` | 83/102 (81%) | 139 |
| `duration-period-value-lot` + unit | 89/102 (87%) | structured |
| `selection-criterion-description-lot` | 71/102 (70%) | 41 |
| `exclusion-grounds-description` | 65/102 (64%) | 74 |
| `renewal-` / `option-description-lot` | 20/102 (20%) | 46–86 |
| `guarantee-required-description-lot` | 9/102 (9%) | 115 |
| `csecurity-clearance-description-lot` | 1/102 (1%) | 249 |

Adding those took the extractor's input from a 269-character subject line to
labelled requirement prose — real award criteria such as *"Clasificación TIER
IV del CPD"* and *"Oferta económica: precio comparativo ponderado"*. 38 of 50
cases now carry requirement text, up to 2,617 characters.

**Second, extract what the text can actually support.** Duration turned out to
be structured too (87% populated), so it joined budget and deadline in the
"don't extract what TED already publishes" pile.

## What is extracted, and why each field is there

| Field | Ground truth | Scoring |
|---|---|---|
| `cpv_division` | TED `main_cpv[:2]` (withheld) | exact, enum |
| `contract_nature` | TED `contract-nature` (withheld) | exact, enum |
| `lot_count` | derived `lot_count` (withheld) | exact |
| `is_health_related` | **human label** | exact |
| `work_category` | **human label** | exact, enum |
| `required_certifications` | **human label** | set equality, normalised |

The first three have independent ground truth at zero labelling cost, on
100% of cases. The extractor sees `title`, `description`, `lot_titles` and the
requirement text, and never sees any of the three truth fields.

The last three have **no counterpart anywhere in TED**, which is precisely why
they are worth extracting — and why they can only be scored against human
judgement. `scripts/label_cases.py` is the labelling CLI; they are excluded
from scoring until labelled rather than silently counted as correct.

`is_health_related` is the one the client actually needs. The ingest-time
keyword filter tags a €58.7m **road-pavement** contract from Palma de Mallorca
as health-related because the word *salud* appears in it, and 118 of 424
"relevant" notices match on that single word. Fixing that is the business case
for the whole extraction step.

## The remaining honest caveat

A model `null` or `"other"` where TED has a value is not automatically an
error — the text may genuinely not say. This caps achievable recall below 100%
by an unknown amount, and that cap is a property of the method, not the model.
So recall alone is not a usable headline. Three numbers are reported together:

- **`wrong_value_rate`** — of the predictions the model actually made, how many
  were wrong. This is the metric that matters for this client: a missing value
  sends someone to read the notice, a *wrong* value sends them to bid on the
  wrong contract. No ceiling problem. **Optimise this one.**
- **`recall`** — of the cases where truth exists, how often the model got it
  right. A trend line across prompt versions, not an absolute.
- **`abstention_rate`** — how often the model declined. Read next to
  `wrong_value_rate`: a model that abstains on everything scores a perfect
  `wrong_value_rate` and is worthless. There is a test asserting exactly that
  (`test_abstaining_everywhere_scores_no_wrong_values_and_zero_recall`).

Any one of the three alone is gameable. All three together are not.

## The baseline exists so the numbers mean something

`StubExtractor` is regexes and keyword rules — no API key, no network, no
cost. It is not a mock; it is the floor. "82% on CPV division" only reads as
good next to what a weekend of regexes achieves:

```
stub, 50 cases, case set v1, prompt v2
  field              n   recall   wrong   abstain
  cpv_division      50    56.0%   44.0%      0.0%
  contract_nature   50    76.0%   17.4%      8.0%
  lot_count         50    92.0%    8.0%      0.0%
```

A model that cannot beat this on a field is not earning its cost on that
field, and `lot_count` at 92% is a fair warning that it may not be worth
spending a model call on.

## Case selection

`python -m procsignal.evaluate build` samples in strata, so the set is not 50
easy contract notices. Earlier strata claim notices first, so each notice
appears exactly once and the counts stay honest.

| Stratum | n | Why |
|---|---|---|
| `keyword_false_positive_candidate` | 7 | Tagged relevant on one weak keyword — the Palma case and its honest counterparts |
| `multi_lot` | 7 | Per-lot arrays invite summing the wrong things |
| `useless_title` | 7 | Forces work from the description |
| `no_published_value` | 7 | The abstention case |
| `long_description` | 4 | Token cost; losing the signal in noise |
| `award_or_modification` | 7 | Different text conventions, deadlines in the past |
| `open_contract_notice` | 7 | The main case |
| `topup` | 4 | Fills to the requested size |

Results are broken down by stratum as well as in aggregate, because one
average hides a systematic failure. The stub's breakdown already shows one:
`cpv_division` recall is 100% on `useless_title` but 14.3% on
`keyword_false_positive_candidate`.

## Versioning and reproducibility

`evals/cases.jsonl` is committed. The header line carries the set version, the
seed, and the ingest window it was drawn from; each case carries its own
`case_version`. Every result row in `evals/results.jsonl` records
`prompt_version`, `case_set_version`, model and effort, so a number is never
silently compared against a different question.

`build --seed` is fixed by default, so the same database produces the same
sample.

## What is reported

```
python -m procsignal.evaluate run --extractor stub
python -m procsignal.evaluate run --extractor claude --effort medium
```

Per field: `n`, `recall`, `wrong_value_rate`, `abstention_rate`, and the raw
correct/wrong/hallucinated/missed counts. Per run: total cost in USD, cost per
100 notices, p50 and p95 latency, token counts, cache hit rate, extractor,
model, effort, prompt version.

Cost and latency are first-class, not an afterthought. An extractor two points
more accurate for six times the cost is a different product decision, and the
number has to be on screen to make it.
