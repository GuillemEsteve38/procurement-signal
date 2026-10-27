"""Eval harness for the extractor.

    python -m procsignal.evaluate build --limit 50      # build the case set
    python -m procsignal.evaluate run --extractor stub  # score a run
    python -m procsignal.evaluate run --extractor claude --effort medium

Methodology, including the design error this harness caught, is in
docs/EVAL.md. The short version: three fields are scored against TED
structured fields the extractor never sees, so there is real ground truth at
zero labelling cost; three more have no structured counterpart anywhere and
need human labels, and are excluded from scoring until labelled.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import sqlite3
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .normalize import requirement_text
from .extract import (
    PROMPT_VERSION,
    Extraction,
    Extractor,
    NoticeText,
    build_extractor,
    default_extractor_name,
)

LOG = logging.getLogger("procsignal.evaluate")

DEFAULT_DB = os.environ.get("PROCSIGNAL_DB", "data/procurement.sqlite3")
DEFAULT_CASES = "evals/cases.jsonl"
DEFAULT_RESULTS = "evals/results.jsonl"

CASE_SET_VERSION = 1

#: Fields scored against TED structured fields the extractor never sees.
#: budget and deadline are absent: they are not present in notice text (a
#: budget cue appears in 2/50 sampled notices, a deadline in 0/50) and TED
#: already publishes both structured. See docs/EVAL.md.
DERIVED_FIELDS = ("cpv_division", "contract_nature", "lot_count")
#: Fields with no structured counterpart anywhere in TED. These are the ones
#: worth extracting, and the ones that need human labels to score.
LABELLED_FIELDS = ("is_health_related", "work_category", "required_certifications")


# ---------------------------------------------------------------------------
# case set
# ---------------------------------------------------------------------------

#: (stratum, extra WHERE). Order matters: earlier strata claim notices first,
#: so a notice appears in exactly one stratum and the counts stay honest.
STRATA: tuple[tuple[str, str], ...] = (
    (
        "keyword_false_positive_candidate",
        # Tagged relevant on exactly one weak keyword. The Palma road-pavement
        # contract lives here; so does the real health work that only says
        # "hospital" once. Distinguishing them is the point of the field.
        "is_relevant = 1 AND json_array_length(matched_keywords) = 1",
    ),
    (
        "multi_lot",
        "lot_count >= 3 AND value_amount IS NOT NULL",
    ),
    (
        "useless_title",
        # Titles that are a file reference rather than a description.
        "(title LIKE 'Contrato de %(CONTR%' OR length(title) < 45) "
        "AND value_amount IS NOT NULL",
    ),
    (
        "no_published_value",
        # The abstention case: the model should return null, not a guess.
        "value_amount IS NULL",
    ),
    (
        "long_description",
        "length(description) > 3000 AND value_amount IS NOT NULL",
    ),
    (
        "award_or_modification",
        "notice_type LIKE 'can-%' AND value_amount IS NOT NULL",
    ),
    (
        "open_contract_notice",
        "notice_type LIKE 'cn-%' AND value_amount IS NOT NULL "
        "AND deadline IS NOT NULL",
    ),
)


@dataclass
class Case:
    case_id: str
    case_version: int
    publication_number: str
    stratum: str
    input: dict[str, Any]
    truth: dict[str, Any]
    labels: dict[str, Any] = field(default_factory=dict)

    def notice(self) -> NoticeText:
        return NoticeText(
            publication_number=self.publication_number,
            title=self.input.get("title") or "",
            description=self.input.get("description") or "",
            lot_titles=list(self.input.get("lot_titles") or []),
            requirement_text=self.input.get("requirement_text") or "",
        )


def build_cases(
    db_path: str | Path,
    *,
    limit: int,
    seed: int,
    per_stratum: int | None = None,
) -> list[Case]:
    """Sample a stratified case set from the ingested database.

    Sampling is seeded, so the same database and seed give the same set and a
    result stays reproducible.
    """
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)

    quota = per_stratum or max(1, limit // len(STRATA))
    claimed: set[str] = set()
    cases: list[Case] = []

    try:
        for stratum, predicate in STRATA:
            rows = conn.execute(
                "SELECT publication_number, title, description, lot_titles,"
                " main_cpv, lot_count, contract_nature, requirements,"
                " value_amount, value_currency, deadline"
                f" FROM notices WHERE {predicate}"
                " AND title IS NOT NULL AND description IS NOT NULL"
                " ORDER BY publication_number"
            ).fetchall()
            pool = [r for r in rows if r["publication_number"] not in claimed]
            rng.shuffle(pool)

            for row in pool[:quota]:
                claimed.add(row["publication_number"])
                cases.append(_case_from_row(row, stratum))

        # Top up from the largest remaining pool if strata under-filled.
        if len(cases) < limit:
            rows = conn.execute(
                "SELECT publication_number, title, description, lot_titles,"
                " main_cpv, lot_count, contract_nature, requirements,"
                " value_amount, value_currency, deadline"
                " FROM notices WHERE title IS NOT NULL AND description IS NOT NULL"
                " ORDER BY publication_number"
            ).fetchall()
            pool = [r for r in rows if r["publication_number"] not in claimed]
            rng.shuffle(pool)
            for row in pool[: limit - len(cases)]:
                claimed.add(row["publication_number"])
                cases.append(_case_from_row(row, "topup"))
    finally:
        conn.close()

    cases.sort(key=lambda c: c.publication_number)
    return cases[:limit]


def _case_from_row(row: sqlite3.Row, stratum: str) -> Case:
    # CPV division is the first two digits of TED's own main CPV code — a
    # well-populated, independent label the extractor never sees.
    main_cpv = row["main_cpv"] or ""
    nature = row["contract_nature"] or None

    return Case(
        case_id=f"{row['publication_number']}",
        case_version=1,
        publication_number=row["publication_number"],
        stratum=stratum,
        input={
            "title": row["title"],
            "description": row["description"],
            "lot_titles": json.loads(row["lot_titles"] or "[]"),
            "requirement_text": requirement_text(
                json.loads(row["requirements"] or "{}")
            ),
        },
        truth={
            "cpv_division": main_cpv[:2] or None,
            "contract_nature": nature,
            "lot_count": row["lot_count"],
        },
        labels={name: None for name in LABELLED_FIELDS},
    )


def _write_cases(cases: Sequence[Case], path: str | Path, meta: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for case in cases:
            handle.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")


def load_cases(path: str | Path) -> tuple[list[Case], dict[str, Any]]:
    cases: list[Case] = []
    meta: dict[str, Any] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if "_meta" in payload:
                meta = payload["_meta"]
                continue
            cases.append(Case(**payload))
    return cases, meta


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _eq_exact(pred: Any, truth: Any) -> bool:
    return pred == truth


def _eq_str(pred: Any, truth: Any) -> bool:
    return str(pred).strip().lower() == str(truth).strip().lower()


def _eq_cert_set(pred: Any, truth: Any) -> bool:
    return _normalise_certs(pred) == _normalise_certs(truth)


def _normalise_certs(value: Any) -> set[str]:
    items = value if isinstance(value, list) else [value]
    return {
        " ".join(str(v).split()).upper().replace("-", " ")
        for v in items
        if v is not None and str(v).strip()
    }


#: field -> (comparator, treat-as-absent values)
COMPARATORS: dict[str, Any] = {
    # "other" is the model declining to classify, so it counts as abstention
    # rather than as a wrong answer.
    "cpv_division": (_eq_str, (None, "", "other")),
    "contract_nature": (_eq_str, (None, "", "unknown")),
    "lot_count": (_eq_exact, (None,)),
    "is_health_related": (_eq_exact, (None,)),
    "work_category": (_eq_str, (None, "", "other")),
    "required_certifications": (_eq_cert_set, (None,)),
}


@dataclass
class FieldScore:
    field: str
    n: int = 0
    truth_present: int = 0
    pred_present: int = 0
    correct: int = 0
    wrong_value: int = 0
    hallucinated: int = 0
    missed: int = 0
    correct_abstention: int = 0
    unlabelled: int = 0

    @property
    def recall(self) -> float | None:
        if not self.truth_present:
            return None
        return self.correct / self.truth_present

    @property
    def wrong_value_rate(self) -> float | None:
        if not self.pred_present:
            return None
        return (self.wrong_value + self.hallucinated) / self.pred_present

    @property
    def abstention_rate(self) -> float | None:
        scored = self.n - self.unlabelled
        if not scored:
            return None
        return (scored - self.pred_present) / scored


def score(
    cases: Sequence[Case], extractions: Sequence[Extraction]
) -> dict[str, FieldScore]:
    scores = {name: FieldScore(field=name) for name in COMPARATORS}

    for case, extraction in zip(cases, extractions):
        predicted = extraction.scored_fields()
        for name, (compare, absent) in COMPARATORS.items():
            s = scores[name]
            s.n += 1

            if name in LABELLED_FIELDS:
                truth = case.labels.get(name)
                if truth is None:
                    s.unlabelled += 1
                    continue
            else:
                truth = case.truth.get(name)

            pred = predicted.get(name)
            # An empty list is a real answer for a set-valued field ("none
            # required"), not an abstention.
            pred_absent = pred in absent if not isinstance(pred, list) else False
            truth_absent = truth in absent if not isinstance(truth, list) else False

            if not extraction.ok:
                s.missed += 1
                continue
            if truth_absent:
                s.truth_present += 0
            else:
                s.truth_present += 1

            if pred_absent:
                if truth_absent:
                    s.correct_abstention += 1
                else:
                    s.missed += 1
                continue

            s.pred_present += 1
            if truth_absent:
                s.hallucinated += 1
            elif compare(pred, truth):
                s.correct += 1
            else:
                s.wrong_value += 1

    return scores


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run_id: str
    started_at: str
    extractor: str
    model: str
    effort: str
    prompt_version: str
    case_set_version: int
    cases: int
    failures: int
    cost_usd: float
    latency_p50_ms: float
    latency_p95_ms: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_hit_rate: float | None
    scores: dict[str, dict[str, Any]]
    by_stratum: dict[str, dict[str, Any]]


def run_eval(
    cases: Sequence[Case],
    extractor: Extractor,
    *,
    case_set_version: int = CASE_SET_VERSION,
    progress: bool = True,
) -> tuple[RunResult, list[Extraction]]:
    extractions: list[Extraction] = []
    started = dt.datetime.now(dt.timezone.utc)

    for index, case in enumerate(cases, start=1):
        extraction = extractor.extract(case.notice())
        extractions.append(extraction)
        if progress:
            status = "ok " if extraction.ok else "ERR"
            LOG.info(
                "  [%3d/%d] %s %s  %4dms  $%.5f  %s",
                index,
                len(cases),
                status,
                case.publication_number,
                extraction.latency_ms,
                extraction.cost_usd,
                case.stratum,
            )
            if not extraction.ok:
                LOG.warning("        %s", extraction.error)

    scores = score(cases, extractions)
    latencies = [e.latency_ms for e in extractions] or [0]
    cache_read = sum(e.cache_read_tokens for e in extractions)
    cache_write = sum(e.cache_write_tokens for e in extractions)
    cacheable = cache_read + cache_write

    by_stratum: dict[str, dict[str, Any]] = {}
    for stratum in sorted({c.stratum for c in cases}):
        subset = [(c, e) for c, e in zip(cases, extractions) if c.stratum == stratum]
        sub_scores = score([c for c, _ in subset], [e for _, e in subset])
        by_stratum[stratum] = {
            "n": len(subset),
            **{
                name: {
                    "recall": s.recall,
                    "wrong_value_rate": s.wrong_value_rate,
                }
                for name, s in sub_scores.items()
                if name in DERIVED_FIELDS
            },
        }

    first = extractions[0] if extractions else Extraction()
    result = RunResult(
        run_id=started.strftime("%Y%m%dT%H%M%SZ"),
        started_at=started.isoformat(timespec="seconds"),
        extractor=getattr(extractor, "name", "?"),
        model=first.model or getattr(extractor, "model", "?"),
        effort=first.effort or getattr(extractor, "effort", "?"),
        prompt_version=PROMPT_VERSION,
        case_set_version=case_set_version,
        cases=len(cases),
        failures=sum(1 for e in extractions if not e.ok),
        cost_usd=sum(e.cost_usd for e in extractions),
        latency_p50_ms=statistics.median(latencies),
        latency_p95_ms=_percentile(latencies, 95),
        input_tokens=sum(e.input_tokens for e in extractions),
        output_tokens=sum(e.output_tokens for e in extractions),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_hit_rate=(cache_read / cacheable) if cacheable else None,
        scores={
            name: {
                "n": s.n,
                "truth_present": s.truth_present,
                "pred_present": s.pred_present,
                "correct": s.correct,
                "wrong_value": s.wrong_value,
                "hallucinated": s.hallucinated,
                "missed": s.missed,
                "correct_abstention": s.correct_abstention,
                "unlabelled": s.unlabelled,
                "recall": s.recall,
                "wrong_value_rate": s.wrong_value_rate,
                "abstention_rate": s.abstention_rate,
            }
            for name, s in scores.items()
        },
        by_stratum=by_stratum,
    )
    return result, extractions


def _percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return float(ordered[index])


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def print_report(result: RunResult) -> None:
    def pct(value: float | None) -> str:
        return "   n/a" if value is None else f"{value * 100:5.1f}%"

    print()
    print("=" * 78)
    print(
        f"EVAL  extractor={result.extractor}  model={result.model}"
        f"  effort={result.effort}  prompt={result.prompt_version}"
    )
    print(f"      cases={result.cases}  case_set=v{result.case_set_version}", end="")
    print(f"  failures={result.failures}")
    print("=" * 78)

    print()
    print("  DERIVED FIELDS — ground truth withheld from the extractor")
    print(
        f"    {'field':<24} {'n':>4} {'recall':>7} {'wrong':>7} {'abstain':>8}"
        f"  {'correct/wrong/halluc/missed':>28}"
    )
    for name in DERIVED_FIELDS:
        s = result.scores[name]
        detail = (
            f"{s['correct']}/{s['wrong_value']}/{s['hallucinated']}/{s['missed']}"
        )
        print(
            f"    {name:<24} {s['truth_present']:>4} {pct(s['recall']):>7}"
            f" {pct(s['wrong_value_rate']):>7} {pct(s['abstention_rate']):>8}"
            f"  {detail:>28}"
        )

    print()
    print("  LABELLED FIELDS — require human labels")
    for name in LABELLED_FIELDS:
        s = result.scores[name]
        if s["unlabelled"] == s["n"]:
            print(
                f"    {name:<24} unscored — {s['n']} case(s) unlabelled "
                f"(run scripts/label_cases.py)"
            )
        else:
            print(
                f"    {name:<24} {s['n'] - s['unlabelled']:>4} {pct(s['recall']):>7}"
                f" {pct(s['wrong_value_rate']):>7}"
                f"   ({s['unlabelled']} still unlabelled)"
            )

    print()
    print("  COST AND LATENCY")
    print(f"    total cost           ${result.cost_usd:.4f}")
    if result.cases:
        print(
            f"    per 100 notices      ${result.cost_usd / result.cases * 100:.2f}"
        )
    print(f"    latency p50 / p95    {result.latency_p50_ms:.0f}ms"
          f" / {result.latency_p95_ms:.0f}ms")
    print(
        f"    tokens in/out        {result.input_tokens:,} / {result.output_tokens:,}"
    )
    if result.cache_hit_rate is not None:
        print(
            f"    cache read/write     {result.cache_read_tokens:,}"
            f" / {result.cache_write_tokens:,}"
            f"  (hit rate {result.cache_hit_rate * 100:.1f}%)"
        )

    print()
    print("  BY STRATUM — recall / wrong-value rate")
    header = "  ".join(f"{f.split('_')[0][:8]:>15}" for f in DERIVED_FIELDS)
    print(f"    {'stratum':<34} {'n':>3}  {header}")
    for stratum, data in result.by_stratum.items():
        cells = []
        for name in DERIVED_FIELDS:
            entry = data.get(name, {})
            cells.append(
                f"{pct(entry.get('recall'))}/{pct(entry.get('wrong_value_rate'))}"
            )
        print(f"    {stratum:<34} {data['n']:>3}  " + "  ".join(
            f"{c:>15}" for c in cells
        ))
    print("=" * 78)


def append_result(result: RunResult, path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="procsignal.evaluate")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="sample a stratified case set")
    p_build.add_argument("--db", default=DEFAULT_DB)
    p_build.add_argument("--out", default=DEFAULT_CASES)
    p_build.add_argument("--limit", type=int, default=50)
    p_build.add_argument("--per-stratum", type=int, default=None)
    p_build.add_argument("--seed", type=int, default=20261023)

    p_run = sub.add_parser("run", help="score an extractor against the case set")
    p_run.add_argument("--cases", default=DEFAULT_CASES)
    p_run.add_argument("--results", default=DEFAULT_RESULTS)
    p_run.add_argument(
        "--extractor",
        default=None,
        help="claude | stub (default: claude if a credential is reachable)",
    )
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--effort", default=None, help="low | medium | high | xhigh | max")
    p_run.add_argument("--limit", type=int, default=None, help="first N cases only")
    p_run.add_argument("--stratum", default=None, help="restrict to one stratum")
    p_run.add_argument("--no-save", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stdout
    )

    if args.command == "build":
        return _cmd_build(args)
    return _cmd_run(args)


def _cmd_build(args) -> int:
    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}; run an ingest first")
        return 1

    cases = build_cases(
        db, limit=args.limit, seed=args.seed, per_stratum=args.per_stratum
    )
    if not cases:
        print("no cases built — is the database empty?")
        return 1

    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        window = conn.execute(
            "SELECT MIN(publication_date), MAX(publication_date), COUNT(*)"
            " FROM notices"
        ).fetchone()
    finally:
        conn.close()

    meta = {
        "case_set_version": CASE_SET_VERSION,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "seed": args.seed,
        "source_db_notices": window[2],
        "source_window": [window[0], window[1]],
        "derived_fields": list(DERIVED_FIELDS),
        "labelled_fields": list(LABELLED_FIELDS),
    }
    _write_cases(cases, args.out, meta)

    counts: dict[str, int] = {}
    for case in cases:
        counts[case.stratum] = counts.get(case.stratum, 0) + 1

    print(f"wrote {len(cases)} cases -> {args.out}")
    print(f"  source: {window[2]} notices, {window[0]} .. {window[1]}")
    print("  strata:")
    for stratum, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {stratum:<36} {n}")

    truth_coverage = {
        name: sum(1 for c in cases if c.truth.get(name) is not None)
        for name in DERIVED_FIELDS
    }
    print("  ground truth available:")
    for name, n in truth_coverage.items():
        print(f"    {name:<36} {n}/{len(cases)}")
    print(f"  labels needed for: {', '.join(LABELLED_FIELDS)}")
    return 0


def _cmd_run(args) -> int:
    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"no case set at {cases_path}; run: evaluate build")
        return 1

    cases, meta = load_cases(cases_path)
    if args.stratum:
        cases = [c for c in cases if c.stratum == args.stratum]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("no cases selected")
        return 1

    name = args.extractor or default_extractor_name()
    if name == "stub" and not args.extractor:
        print(
            "no Anthropic credential found — falling back to the deterministic\n"
            "stub extractor. Set ANTHROPIC_API_KEY (or run `ant auth login`)\n"
            "and re-run with --extractor claude for the real numbers.\n"
        )

    kwargs: dict[str, Any] = {}
    if args.model:
        kwargs["model"] = args.model
    if args.effort:
        kwargs["effort"] = args.effort

    try:
        extractor = build_extractor(name, **kwargs)
    except (ValueError, RuntimeError) as exc:
        print(f"could not build extractor: {exc}")
        return 2

    LOG.info(
        "running %d case(s) through %s (case set v%s)",
        len(cases),
        name,
        meta.get("case_set_version", "?"),
    )
    start = time.perf_counter()
    result, _ = run_eval(
        cases, extractor, case_set_version=meta.get("case_set_version", CASE_SET_VERSION)
    )
    LOG.info("wall clock %.1fs", time.perf_counter() - start)

    print_report(result)

    if not args.no_save:
        append_result(result, args.results)
        print(f"\nappended to {args.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
