"""Ingest CLI.

    python -m procsignal.ingest --profile spanish-health-it --days 60

The run is windowed by publication date and chunked, so a large backfill is a
sequence of small resumable runs rather than one long scroll that loses
everything if it dies on page 40.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import traceback

from .normalize import normalize
from .profile import PROFILES
from .store import Store, UpsertCounts
from .ted import Page, TedClient, TedError, TedQueryError

LOG = logging.getLogger("procsignal.ingest")

DEFAULT_DB = "data/procurement.sqlite3"


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        profile = PROFILES[args.profile]
    except KeyError:
        LOG.error(
            "unknown profile %r; available: %s",
            args.profile,
            ", ".join(sorted(PROFILES)),
        )
        return 2

    date_to = args.until or dt.date.today()
    date_from = args.since or (date_to - dt.timedelta(days=args.days))
    if date_from > date_to:
        LOG.error("--since (%s) is after --until (%s)", date_from, date_to)
        return 2

    windows = list(_windows(date_from, date_to, args.window_days))
    LOG.info(
        "profile=%s window=%s..%s (%d chunk(s) of <=%d days) db=%s",
        profile.name,
        date_from,
        date_to,
        len(windows),
        args.window_days,
        args.db,
    )

    totals = UpsertCounts()
    seen_total = 0
    failures = 0

    with Store(args.db) as store, TedClient() as client:
        for lo, hi in windows:
            try:
                counts, seen = _ingest_window(
                    client,
                    store,
                    profile,
                    lo,
                    hi,
                    limit=args.limit,
                    max_pages=args.max_pages,
                    dry_run=args.dry_run,
                )
            except TedQueryError as exc:
                # Our request is wrong. No amount of retrying or moving to the
                # next window fixes that, so stop and show the structured error.
                LOG.error("query rejected by TED: %s", exc)
                return 1
            except TedError as exc:
                LOG.error("window %s..%s failed: %s", lo, hi, exc)
                failures += 1
                continue

            totals += counts
            seen_total += seen

        _report(store, totals, seen_total, failures)

    return 1 if failures else 0


def _ingest_window(
    client: TedClient,
    store: Store,
    profile,
    date_from: dt.date,
    date_to: dt.date,
    *,
    limit: int,
    max_pages: int | None,
    dry_run: bool,
) -> tuple[UpsertCounts, int]:
    query = profile.query(date_from, date_to)
    LOG.info("window %s..%s | query: %s", date_from, date_to, query)

    run_id = store.start_run(
        profile=profile.name,
        query=query,
        window_from=date_from.isoformat(),
        window_to=date_to.isoformat(),
    )

    counts = UpsertCounts()
    seen = 0
    skipped = 0
    pages = 0
    reported_total: int | None = None

    def on_page(page_no: int, page: Page) -> None:
        nonlocal pages, reported_total
        pages = page_no
        reported_total = page.total
        LOG.info(
            "  page %d: %d notices (query matches %d)",
            page_no,
            len(page.notices),
            page.total,
        )

    try:
        for raw in client.iter_notices(
            query, profile.fields, limit=limit, max_pages=max_pages, on_page=on_page
        ):
            seen += 1
            store.observe_fields(raw, expected=profile.fields)
            try:
                notice = normalize(
                    raw,
                    known_fields=profile.fields,
                    relevance_keywords=profile.relevance_keywords,
                )
            except ValueError as exc:
                # Only raised when there is no publication-number, i.e. no key
                # to attach the problem to. Count it; do not abort the run.
                LOG.warning("skipping unidentifiable notice: %s", exc)
                skipped += 1
                continue

            if dry_run:
                continue

            outcome = store.upsert(raw=raw, notice=notice, profile=profile.name)
            setattr(counts, outcome, getattr(counts, outcome) + 1)

            if seen % 200 == 0:
                store.commit()

        if not dry_run:
            store.commit()
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        store.commit()
        store.finish_run(
            run_id,
            status="failed",
            reported_total=reported_total,
            pages=pages,
            seen=seen,
            counts=counts,
            skipped=skipped,
            error=f"{type(exc).__name__}: {exc}",
        )
        LOG.debug("traceback:\n%s", traceback.format_exc())
        raise

    store.finish_run(
        run_id,
        status="ok",
        reported_total=reported_total,
        pages=pages,
        seen=seen,
        counts=counts,
        skipped=skipped,
    )

    if reported_total is not None and seen < reported_total and max_pages is None:
        # The clearest signal that an ingest silently lost data.
        LOG.warning(
            "  window reported %d matches but only %d were fetched",
            reported_total,
            seen,
        )

    LOG.info(
        "  window done: seen=%d inserted=%d updated=%d unchanged=%d skipped=%d",
        seen,
        counts.inserted,
        counts.updated,
        counts.unchanged,
        skipped,
    )
    return counts, seen


def _report(
    store: Store, totals: UpsertCounts, seen: int, failures: int
) -> None:
    print()
    print("=" * 68)
    print("INGEST SUMMARY")
    print("=" * 68)
    print(
        f"  fetched {seen}  ->  inserted {totals.inserted}, "
        f"updated {totals.updated}, unchanged {totals.unchanged}"
    )
    if failures:
        print(f"  !! {failures} window(s) failed; see ingest_runs table")

    s = store.summary()
    print()
    print(f"  notices in db     : {s['notices']}")
    print(f"  keyword-relevant  : {s['relevant']}")
    print(f"  publication range : {s['earliest']} .. {s['latest']}")
    total_eur = s["eur_value_sum"] or 0.0
    print(
        f"  with EUR value    : {s['with_eur_value']}"
        f"  (sum EUR {total_eur:,.0f})"
    )

    types = store.notice_type_counts()
    if types:
        print()
        print("  notice types:")
        for name, n in list(types.items())[:10]:
            print(f"    {name:<24} {n}")

    anomalies = store.anomaly_counts()
    if anomalies:
        print()
        print("  anomalies (notices affected):")
        for code, n in anomalies.items():
            print(f"    {code:<32} {n}")

    unexpected = store.unexpected_fields()
    if unexpected:
        print()
        print("  !! fields TED returned that the profile did not ask for:")
        for row in unexpected:
            print(
                f"    {row['field_name']:<40} "
                f"x{row['seen_count']} since {row['first_seen']}"
            )
    print("=" * 68)


def _windows(
    date_from: dt.date, date_to: dt.date, window_days: int
) -> list[tuple[dt.date, dt.date]]:
    """Split a date range into inclusive chunks."""
    out: list[tuple[dt.date, dt.date]] = []
    cursor = date_from
    step = dt.timedelta(days=max(1, window_days) - 1)
    while cursor <= date_to:
        end = min(cursor + step, date_to)
        out.append((cursor, end))
        cursor = end + dt.timedelta(days=1)
    return out


def _force_utf8_stdio() -> None:
    """Make Spanish text survive a Windows console.

    Python on Windows defaults stdout to the ANSI code page (cp1252 here),
    which raises UnicodeEncodeError the moment a notice title contains a
    character outside it — and that includes ordinary accented Spanish. This
    is a real bug that cost an afternoon, not defensive boilerplate.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="procsignal.ingest",
        description="Ingest TED procurement notices into SQLite.",
    )
    p.add_argument(
        "--profile",
        default="spanish-health-it",
        help=f"search profile ({', '.join(sorted(PROFILES))})",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="how many days back from --until (ignored if --since is given)",
    )
    p.add_argument("--since", type=_date, help="first publication date (YYYY-MM-DD)")
    p.add_argument("--until", type=_date, help="last publication date (YYYY-MM-DD)")
    p.add_argument(
        "--window-days",
        type=int,
        default=14,
        help="chunk size; smaller chunks resume better and avoid server timeouts",
    )
    p.add_argument("--limit", type=int, default=250, help="page size")
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="stop after N pages per window (for smoke tests)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and normalize but write no notice rows (the run log and "
        "field observations are still recorded)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


if __name__ == "__main__":
    raise SystemExit(main())
