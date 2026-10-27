"""Run the extractor across ingested notices and persist the results.

    python -m procsignal.enrich --limit 20                  # relevant notices
    python -m procsignal.enrich --extractor stub --all      # everything, free
    python -m procsignal.enrich --dry-run --limit 5         # no writes

Only notices with no successful extraction for the exact extractor / model /
effort / prompt-version combination are processed, so a run that dies partway
resumes without re-paying for work already done. Changing the prompt version
makes every notice eligible again, which is the intended behaviour: a new
prompt is a new question.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .extract import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    NoticeText,
    build_extractor,
    default_extractor_name,
)
from .normalize import requirement_text
from .store import Store

LOG = logging.getLogger("procsignal.enrich")

DEFAULT_DB = os.environ.get("PROCSIGNAL_DB", "data/procurement.sqlite3")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    name = args.extractor or default_extractor_name()
    if name == "stub" and not args.extractor:
        LOG.warning(
            "no Anthropic credential found — using the deterministic stub. "
            "Set ANTHROPIC_API_KEY and pass --extractor claude for the real thing."
        )

    try:
        extractor = build_extractor(name, model=args.model, effort=args.effort)
    except (ValueError, RuntimeError) as exc:
        LOG.error("could not build extractor: %s", exc)
        return 2

    model = getattr(extractor, "model", args.model)
    effort = getattr(extractor, "effort", args.effort)

    with Store(args.db) as store:
        from .extract import PROMPT_VERSION

        pending = store.notices_needing_extraction(
            extractor=name,
            model=model,
            effort=effort,
            prompt_version=PROMPT_VERSION,
            relevant_only=not args.all,
            limit=args.limit,
        )
        if not pending:
            LOG.info("nothing to do — every eligible notice already extracted")
            return 0

        LOG.info(
            "extracting %d notice(s) with %s (model=%s effort=%s prompt=%s)",
            len(pending),
            name,
            model,
            effort,
            PROMPT_VERSION,
        )

        cost = 0.0
        failures = 0
        for index, row in enumerate(pending, start=1):
            notice = NoticeText(
                publication_number=row["publication_number"],
                title=row["title"] or "",
                description=row["description"] or "",
                lot_titles=json.loads(row["lot_titles"] or "[]"),
                requirement_text=requirement_text(
                    json.loads(row["requirements"] or "{}")
                ),
            )
            extraction = extractor.extract(notice)
            cost += extraction.cost_usd
            if not extraction.ok:
                failures += 1
                LOG.warning(
                    "  [%d/%d] %s FAILED: %s",
                    index,
                    len(pending),
                    notice.publication_number,
                    extraction.error,
                )
            else:
                LOG.info(
                    "  [%d/%d] %s  %s / %s  health=%s  %4dms  $%.5f",
                    index,
                    len(pending),
                    notice.publication_number,
                    extraction.cpv_division,
                    extraction.work_category,
                    extraction.is_health_related,
                    extraction.latency_ms,
                    extraction.cost_usd,
                )

            if not args.dry_run:
                store.save_extraction(notice.publication_number, extraction)
                if index % 25 == 0:
                    store.commit()

        if not args.dry_run:
            store.commit()

        summary = store.extraction_summary()
        print()
        print(f"  processed        {len(pending)}  ({failures} failed)")
        print(f"  run cost         ${cost:.4f}")
        if pending:
            print(f"  per 100 notices  ${cost / len(pending) * 100:.2f}")
        print(f"  in db            {summary['extractions']} extraction(s),"
              f" ${summary['cost_usd']:.4f} cumulative")
        print(f"  health-related   {summary['health_related']}")

    return 1 if failures else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="procsignal.enrich")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--extractor", default=None, help="claude | stub")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--effort", default=DEFAULT_EFFORT)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--all",
        action="store_true",
        help="include notices the keyword filter did not tag relevant",
    )
    p.add_argument("--dry-run", action="store_true", help="extract but do not write")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
