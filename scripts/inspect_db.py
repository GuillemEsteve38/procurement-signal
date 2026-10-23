"""Browse the ingest database without needing the sqlite3 CLI.

Python's sqlite3 module is part of the standard library, so this works out of
the box on any machine that can run the ingest.

    python scripts/inspect_db.py                      # overview
    python scripts/inspect_db.py schema               # all table definitions
    python scripts/inspect_db.py schema notices       # one table's columns
    python scripts/inspect_db.py runs                 # ingest history
    python scripts/inspect_db.py anomalies            # anomaly counts
    python scripts/inspect_db.py top --limit 20       # biggest relevant tenders
    python scripts/inspect_db.py notice 737081-2026    # one notice, normalized
    python scripts/inspect_db.py raw 737081-2026       # ...and its raw payload
    python scripts/inspect_db.py sql "SELECT buyer_name, COUNT(*) n FROM notices GROUP BY 1 ORDER BY n DESC LIMIT 10"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "data/procurement.sqlite3"


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def table(rows: list[sqlite3.Row], *, max_width: int = 58) -> None:
    """Print rows as an aligned text table, truncating wide cells."""
    if not rows:
        print("  (no rows)")
        return

    headers = list(rows[0].keys())
    cells = [[_fmt(r[h], max_width) for h in headers] for r in rows]
    widths = [
        min(max_width, max(len(h), *(len(row[i]) for row in cells)))
        for i, h in enumerate(headers)
    ]

    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in cells:
        print("  " + "  ".join(c.ljust(w)[:w] for c, w in zip(row, widths)))
    print(f"\n  {len(rows)} row(s)")


def _fmt(value: object, max_width: int) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= max_width else text[: max_width - 1] + "…"


def heading(text: str) -> None:
    print()
    print(text)
    print("=" * min(len(text), 72))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_overview(conn: sqlite3.Connection, args) -> None:
    heading("TABLES")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in rows:
        n = conn.execute(f"SELECT COUNT(*) FROM {row['name']}").fetchone()[0]
        print(f"  {row['name']:<22} {n:>8} rows")

    heading("NOTICES")
    s = conn.execute(
        "SELECT COUNT(*) total,"
        " SUM(is_relevant) relevant,"
        " MIN(publication_date) earliest,"
        " MAX(publication_date) latest,"
        " SUM(value_amount IS NOT NULL) with_value"
        " FROM notices"
    ).fetchone()
    for key in s.keys():
        print(f"  {key:<22} {_fmt(s[key], 40)}")

    heading("NOTICE TYPES")
    table(
        conn.execute(
            "SELECT notice_type, COUNT(*) n FROM notices "
            "GROUP BY 1 ORDER BY n DESC LIMIT 12"
        ).fetchall()
    )

    cmd_anomalies(conn, args)

    heading("LAST 5 INGEST RUNS")
    table(
        conn.execute(
            "SELECT id, window_from, window_to, status, reported_total, seen,"
            " inserted, updated, unchanged FROM ingest_runs"
            " ORDER BY id DESC LIMIT 5"
        ).fetchall()
    )

    print()
    print("  More: scripts/inspect_db.py --help")


def cmd_schema(conn: sqlite3.Connection, args) -> None:
    if args.table:
        heading(f"COLUMNS OF {args.table}")
        rows = conn.execute(f"PRAGMA table_info({args.table})").fetchall()
        if not rows:
            print(f"  no such table: {args.table}")
            return
        table(rows)
        return

    heading("SCHEMA")
    for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
    ):
        print(row["sql"].strip() + ";\n")


def cmd_runs(conn: sqlite3.Connection, args) -> None:
    heading("INGEST RUNS")
    table(
        conn.execute(
            "SELECT id, profile, window_from, window_to, status, reported_total,"
            " pages, seen, inserted, updated, unchanged, skipped, error"
            " FROM ingest_runs ORDER BY id DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    )
    gaps = conn.execute(
        "SELECT id, window_from, window_to, reported_total, seen"
        " FROM ingest_runs"
        " WHERE status='ok' AND reported_total IS NOT NULL AND seen < reported_total"
    ).fetchall()
    if gaps:
        print()
        print("  !! runs that fetched fewer notices than TED reported:")
        table(gaps)


def cmd_anomalies(conn: sqlite3.Connection, args) -> None:
    heading("ANOMALIES (notices affected)")
    counts: dict[str, int] = {}
    for (blob,) in conn.execute(
        "SELECT anomalies FROM notices WHERE anomalies NOT IN ('[]','')"
    ):
        for code in json.loads(blob):
            counts[code] = counts.get(code, 0) + 1
    if not counts:
        print("  (none)")
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {code:<32} {n}")

    unexpected = conn.execute(
        "SELECT field_name, seen_count, first_seen FROM field_observations"
        " WHERE is_expected=0 ORDER BY seen_count DESC"
    ).fetchall()
    if unexpected:
        heading("UNEXPECTED API FIELDS (schema drift)")
        table(unexpected)


def cmd_top(conn: sqlite3.Connection, args) -> None:
    heading(f"TOP {args.limit} BY VALUE" + (" (relevant only)" if args.relevant else ""))
    where = ["value_amount IS NOT NULL"]
    if args.relevant:
        where.append("is_relevant=1")
    if args.open_only:
        where.append("notice_type LIKE 'cn-%'")
    table(
        conn.execute(
            "SELECT publication_number, publication_date, value_amount,"
            " value_currency, lot_count, deadline, buyer_name, title"
            f" FROM notices WHERE {' AND '.join(where)}"
            " ORDER BY value_amount DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    )


def cmd_notice(conn: sqlite3.Connection, args) -> None:
    row = conn.execute(
        "SELECT * FROM notices WHERE publication_number=?", (args.publication_number,)
    ).fetchone()
    if row is None:
        print(f"  not found: {args.publication_number}")
        return
    heading(f"NOTICE {args.publication_number} (normalized)")
    for key in row.keys():
        value = row[key]
        if key in ("description", "lot_descriptions") and value:
            value = str(value)[:400]
        print(f"  {key:<22} {_fmt(value, 90)}")


def cmd_raw(conn: sqlite3.Connection, args) -> None:
    row = conn.execute(
        "SELECT payload, payload_sha256, first_fetched_at, last_fetched_at, fetch_count"
        " FROM raw_notices WHERE publication_number=?",
        (args.publication_number,),
    ).fetchone()
    if row is None:
        print(f"  not found: {args.publication_number}")
        return
    heading(f"RAW PAYLOAD {args.publication_number}")
    print(
        f"  sha256={row['payload_sha256'][:16]}…  fetched {row['fetch_count']}x"
        f"  first={row['first_fetched_at']}  last={row['last_fetched_at']}"
    )
    print()
    print(json.dumps(json.loads(row["payload"]), indent=2, ensure_ascii=False))


def cmd_sql(conn: sqlite3.Connection, args) -> None:
    statement = args.statement.strip()
    if not statement.lower().startswith(("select", "with", "pragma", "explain")):
        # Read-only on purpose: this is a browser, not a migration tool. Use
        # the ingest CLI to change data.
        print("  refusing to run anything but SELECT / WITH / PRAGMA / EXPLAIN")
        return
    heading("QUERY")
    print(f"  {statement}")
    print()
    try:
        table(conn.execute(statement).fetchall(), max_width=args.width)
    except sqlite3.Error as exc:
        print(f"  sqlite error: {exc}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="inspect_db.py",
        description="Browse the procurement database (read-only).",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"default: {DEFAULT_DB}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("overview", help="counts, date range, notice types, anomalies")

    p_schema = sub.add_parser("schema", help="table definitions")
    p_schema.add_argument("table", nargs="?", help="limit to one table")

    p_runs = sub.add_parser("runs", help="ingest history and coverage gaps")
    p_runs.add_argument("--limit", type=int, default=20)

    sub.add_parser("anomalies", help="anomaly counts and schema drift")

    p_top = sub.add_parser("top", help="largest notices by value")
    p_top.add_argument("--limit", type=int, default=10)
    p_top.add_argument(
        "--all", dest="relevant", action="store_false", help="include non-relevant"
    )
    p_top.add_argument(
        "--open-only",
        action="store_true",
        help="contract notices only (exclude awards)",
    )

    p_notice = sub.add_parser("notice", help="one normalized notice")
    p_notice.add_argument("publication_number")

    p_raw = sub.add_parser("raw", help="one raw API payload")
    p_raw.add_argument("publication_number")

    p_sql = sub.add_parser("sql", help="run a read-only query")
    p_sql.add_argument("statement")
    p_sql.add_argument("--width", type=int, default=58, help="max column width")

    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}")
        print("run an ingest first:  python -m procsignal.ingest --days 30")
        return 1

    # Read-only URI so a stray query can never damage the ingest database.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        handler = {
            None: cmd_overview,
            "overview": cmd_overview,
            "schema": cmd_schema,
            "runs": cmd_runs,
            "anomalies": cmd_anomalies,
            "top": cmd_top,
            "notice": cmd_notice,
            "raw": cmd_raw,
            "sql": cmd_sql,
        }[args.command]
        handler(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
