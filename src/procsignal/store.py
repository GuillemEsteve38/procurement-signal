"""SQLite persistence.

Three design decisions worth stating, because they are what make an ingest
restartable rather than merely re-runnable:

1. **The raw payload is stored verbatim, forever.** Normalization is a guess
   about what TED's undocumented fields mean. Keeping the raw JSON means a
   wrong guess costs a re-parse, not a re-crawl.
2. **Writes are idempotent and keyed on ``publication-number``.** A run killed
   halfway re-runs with no duplicates. Re-ingesting unchanged data is detected
   by content hash and recorded as "unchanged" rather than written again.
3. **Every run is logged, including failures.** A silent short run is the
   failure mode that matters most in ingestion, so row counts per run are
   first-class data, not log output.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .normalize import NormalizedNotice

SCHEMA_VERSION = 2

#: Columns added after v1 shipped. ``CREATE TABLE IF NOT EXISTS`` does not add
#: columns to a table that already exists, so an existing database needs these
#: applied explicitly or every insert fails on an unknown column.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("notices", "duration_months", "INTEGER"),
    ("notices", "duration_raw", "TEXT"),
    ("notices", "requirements", "TEXT"),
)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Verbatim API responses. Never rewritten once the hash matches.
CREATE TABLE IF NOT EXISTS raw_notices (
    publication_number TEXT PRIMARY KEY,
    payload            TEXT NOT NULL,
    payload_sha256     TEXT NOT NULL,
    first_fetched_at   TEXT NOT NULL,
    last_fetched_at    TEXT NOT NULL,
    fetch_count        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notices (
    publication_number     TEXT PRIMARY KEY,
    notice_identifier      TEXT,
    publication_date       TEXT,
    publication_offset     TEXT,
    notice_type            TEXT,
    notice_subtype         TEXT,
    form_type              TEXT,

    title                  TEXT,
    title_lang             TEXT,
    description            TEXT,
    description_lang       TEXT,

    buyer_name             TEXT,
    buyer_city             TEXT,
    buyer_country          TEXT,
    buyer_identifiers      TEXT,  -- json array
    buyer_legal_type       TEXT,
    main_activity          TEXT,

    main_cpv               TEXT,
    cpv_codes              TEXT,  -- json array
    cpv_divisions          TEXT,  -- json array

    lot_count              INTEGER NOT NULL DEFAULT 0,
    lot_titles             TEXT,  -- json array
    lot_descriptions       TEXT,  -- json array

    value_amount           REAL,
    value_currency         TEXT,
    value_source           TEXT,  -- total | estimated_proc | estimated_lot_sum

    deadline               TEXT,
    nuts_codes             TEXT,  -- json array
    performance_countries  TEXT,  -- json array
    performance_anywhere   INTEGER NOT NULL DEFAULT 0,

    procedure_type         TEXT,
    contract_nature        TEXT,
    winner_names           TEXT,  -- json array

    duration_months        INTEGER,
    duration_raw           TEXT,
    requirements           TEXT,  -- json object: TED field -> list of text blocks

    notice_url             TEXT,
    contract_url           TEXT,

    is_relevant            INTEGER NOT NULL DEFAULT 0,
    matched_keywords       TEXT,  -- json array
    anomalies              TEXT,  -- json array
    unknown_fields         TEXT,  -- json array

    profile                TEXT,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    payload_sha256         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_notices_pubdate   ON notices(publication_date DESC);
CREATE INDEX IF NOT EXISTS ix_notices_buyer     ON notices(buyer_name);
CREATE INDEX IF NOT EXISTS ix_notices_country   ON notices(buyer_country);
CREATE INDEX IF NOT EXISTS ix_notices_maincpv   ON notices(main_cpv);
CREATE INDEX IF NOT EXISTS ix_notices_relevant  ON notices(is_relevant, publication_date DESC);
CREATE INDEX IF NOT EXISTS ix_notices_deadline  ON notices(deadline);

-- One row per ingest attempt, written at start and updated at finish, so a
-- crashed run leaves a 'running' row behind instead of leaving no trace.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile        TEXT NOT NULL,
    query          TEXT NOT NULL,
    window_from    TEXT,
    window_to      TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,  -- running | ok | failed
    reported_total INTEGER,
    pages          INTEGER NOT NULL DEFAULT 0,
    seen           INTEGER NOT NULL DEFAULT 0,
    inserted       INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    unchanged      INTEGER NOT NULL DEFAULT 0,
    skipped        INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

-- One row per notice per extractor configuration, so two prompt versions or
-- two models can be compared on the same notice without overwriting.
CREATE TABLE IF NOT EXISTS extractions (
    publication_number  TEXT NOT NULL,
    extractor           TEXT NOT NULL,
    model               TEXT NOT NULL,
    effort              TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,

    cpv_division        TEXT,
    contract_nature     TEXT,
    lot_count           INTEGER,
    is_health_related   INTEGER,
    work_category       TEXT,
    required_certifications TEXT,  -- json array
    renewable           INTEGER,
    summary_es          TEXT,

    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    ok                  INTEGER NOT NULL DEFAULT 1,
    error               TEXT,
    extracted_at        TEXT NOT NULL,

    PRIMARY KEY (publication_number, extractor, model, effort, prompt_version)
);

CREATE INDEX IF NOT EXISTS ix_extractions_health
    ON extractions(is_health_related);

-- Field-level drift detector: how often each API key has been seen, and when.
-- A key that appears for the first time months into the project is how a
-- schema change announces itself.
CREATE TABLE IF NOT EXISTS field_observations (
    field_name  TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    seen_count  INTEGER NOT NULL DEFAULT 0,
    is_expected INTEGER NOT NULL DEFAULT 1
);
"""

JSON_COLUMNS = frozenset(
    {
        "buyer_identifiers",
        "cpv_codes",
        "cpv_divisions",
        "lot_titles",
        "lot_descriptions",
        "nuts_codes",
        "performance_countries",
        "winner_names",
        "matched_keywords",
        "anomalies",
        "unknown_fields",
    }
)

#: JSON columns holding an object rather than an array. Kept separate because
#: `dict or []` would silently turn an empty mapping into an empty list.
JSON_OBJECT_COLUMNS = frozenset({"requirements"})


@dataclasses.dataclass
class UpsertCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def __iadd__(self, other: "UpsertCounts") -> "UpsertCounts":
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        return self


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def payload_hash(raw: dict[str, Any]) -> str:
    """Content hash of a raw notice.

    ``sort_keys`` matters: TED does not guarantee key order, and an unsorted
    dump would make every re-fetch look like a change.
    """
    blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Store:
    """Connection wrapper. Not thread-safe; one per process is the intent."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Add post-v1 columns to a database created by an earlier version."""
        for table, column, decl in MIGRATIONS:
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # table does not exist yet; SCHEMA just created it
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # -- runs --------------------------------------------------------------

    def start_run(
        self,
        *,
        profile: str,
        query: str,
        window_from: str | None,
        window_to: str | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_runs"
            "(profile, query, window_from, window_to, started_at, status) "
            "VALUES(?,?,?,?,?,'running')",
            (profile, query, window_from, window_to, now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        reported_total: int | None = None,
        pages: int = 0,
        seen: int = 0,
        counts: UpsertCounts | None = None,
        skipped: int = 0,
        error: str | None = None,
    ) -> None:
        c = counts or UpsertCounts()
        self.conn.execute(
            "UPDATE ingest_runs SET finished_at=?, status=?, reported_total=?, "
            "pages=?, seen=?, inserted=?, updated=?, unchanged=?, skipped=?, error=? "
            "WHERE id=?",
            (
                now(),
                status,
                reported_total,
                pages,
                seen,
                c.inserted,
                c.updated,
                c.unchanged,
                skipped,
                error,
                run_id,
            ),
        )
        self.conn.commit()

    # -- notices -----------------------------------------------------------

    def upsert(
        self,
        notice: NormalizedNotice,
        raw: dict[str, Any],
        *,
        profile: str,
    ) -> str:
        """Insert or refresh one notice. Returns 'inserted'/'updated'/'unchanged'.

        ``last_seen_at`` is bumped even when nothing changed, which is how we
        can tell "TED still lists this notice" apart from "we have not looked
        since March".
        """
        sha = payload_hash(raw)
        ts = now()
        blob = json.dumps(raw, sort_keys=True, ensure_ascii=False)

        prior = self.conn.execute(
            "SELECT payload_sha256 FROM notices WHERE publication_number=?",
            (notice.publication_number,),
        ).fetchone()

        self.conn.execute(
            "INSERT INTO raw_notices"
            "(publication_number, payload, payload_sha256, first_fetched_at, last_fetched_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(publication_number) DO UPDATE SET "
            "  payload = excluded.payload,"
            "  payload_sha256 = excluded.payload_sha256,"
            "  last_fetched_at = excluded.last_fetched_at,"
            "  fetch_count = raw_notices.fetch_count + 1",
            (notice.publication_number, blob, sha, ts, ts),
        )

        if prior is not None and prior["payload_sha256"] == sha:
            self.conn.execute(
                "UPDATE notices SET last_seen_at=? WHERE publication_number=?",
                (ts, notice.publication_number),
            )
            return "unchanged"

        row = _row_from_notice(notice, profile=profile, sha=sha, ts=ts)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)
        # first_seen_at is deliberately excluded from the update set — the
        # first time we saw a notice should not drift on re-ingest.
        assignments = ", ".join(
            f"{k} = excluded.{k}" for k in row if k not in ("publication_number", "first_seen_at")
        )
        self.conn.execute(
            f"INSERT INTO notices ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(publication_number) DO UPDATE SET {assignments}",
            row,
        )
        return "updated" if prior is not None else "inserted"

    def save_extraction(
        self, publication_number: str, extraction: Any
    ) -> None:
        """Persist one extraction, keyed by its full configuration.

        The key includes extractor, model, effort and prompt version, so two
        configurations can be compared on the same notice instead of one
        silently overwriting the other.
        """
        self.conn.execute(
            "INSERT INTO extractions ("
            " publication_number, extractor, model, effort, prompt_version,"
            " cpv_division, contract_nature, lot_count, is_health_related,"
            " work_category, required_certifications, renewable, summary_es,"
            " input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, cost_usd, latency_ms, ok, error, extracted_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(publication_number, extractor, model, effort,"
            " prompt_version) DO UPDATE SET"
            "  cpv_division = excluded.cpv_division,"
            "  contract_nature = excluded.contract_nature,"
            "  lot_count = excluded.lot_count,"
            "  is_health_related = excluded.is_health_related,"
            "  work_category = excluded.work_category,"
            "  required_certifications = excluded.required_certifications,"
            "  renewable = excluded.renewable,"
            "  summary_es = excluded.summary_es,"
            "  input_tokens = excluded.input_tokens,"
            "  output_tokens = excluded.output_tokens,"
            "  cache_read_tokens = excluded.cache_read_tokens,"
            "  cache_write_tokens = excluded.cache_write_tokens,"
            "  cost_usd = excluded.cost_usd,"
            "  latency_ms = excluded.latency_ms,"
            "  ok = excluded.ok,"
            "  error = excluded.error,"
            "  extracted_at = excluded.extracted_at",
            (
                publication_number,
                extraction.extractor,
                extraction.model,
                extraction.effort,
                extraction.prompt_version,
                extraction.cpv_division,
                extraction.contract_nature,
                extraction.lot_count,
                int(bool(extraction.is_health_related)),
                extraction.work_category,
                json.dumps(extraction.required_certifications, ensure_ascii=False),
                int(bool(extraction.renewable)),
                extraction.summary_es,
                extraction.input_tokens,
                extraction.output_tokens,
                extraction.cache_read_tokens,
                extraction.cache_write_tokens,
                extraction.cost_usd,
                extraction.latency_ms,
                int(bool(extraction.ok)),
                extraction.error,
                now(),
            ),
        )

    def notices_needing_extraction(
        self,
        *,
        extractor: str,
        model: str,
        effort: str,
        prompt_version: str,
        relevant_only: bool = True,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Notices with no successful extraction for this exact configuration."""
        clauses = ["n.title IS NOT NULL"]
        if relevant_only:
            clauses.append("n.is_relevant = 1")
        where = " AND ".join(clauses)
        sql = (
            "SELECT n.publication_number, n.title, n.description, n.lot_titles,"
            " n.requirements"
            " FROM notices n"
            " LEFT JOIN extractions e"
            "   ON e.publication_number = n.publication_number"
            "  AND e.extractor = ? AND e.model = ? AND e.effort = ?"
            "  AND e.prompt_version = ? AND e.ok = 1"
            f" WHERE {where} AND e.publication_number IS NULL"
            " ORDER BY n.publication_date DESC"
        )
        params: list[Any] = [extractor, model, effort, prompt_version]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.conn.execute(sql, params))

    def extraction_summary(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) n, SUM(ok) ok, SUM(cost_usd) cost,"
            " SUM(is_health_related) health FROM extractions"
        ).fetchone()
        return {
            "extractions": row["n"] or 0,
            "ok": row["ok"] or 0,
            "cost_usd": row["cost"] or 0.0,
            "health_related": row["health"] or 0,
        }

    def observe_fields(
        self, raw: dict[str, Any], *, expected: Iterable[str] = ()
    ) -> None:
        expected_set = set(expected)
        ts = now()
        for key in raw:
            self.conn.execute(
                "INSERT INTO field_observations"
                "(field_name, first_seen, last_seen, seen_count, is_expected) "
                "VALUES(?,?,?,1,?) "
                "ON CONFLICT(field_name) DO UPDATE SET "
                "  last_seen = excluded.last_seen,"
                "  seen_count = field_observations.seen_count + 1",
                (key, ts, ts, 1 if (not expected_set or key in expected_set) else 0),
            )

    def commit(self) -> None:
        self.conn.commit()

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        q = self.conn.execute
        total = q("SELECT COUNT(*) AS n FROM notices").fetchone()["n"]
        relevant = q(
            "SELECT COUNT(*) AS n FROM notices WHERE is_relevant=1"
        ).fetchone()["n"]
        dates = q(
            "SELECT MIN(publication_date) AS lo, MAX(publication_date) AS hi "
            "FROM notices WHERE publication_date IS NOT NULL"
        ).fetchone()
        valued = q(
            "SELECT COUNT(*) AS n, SUM(value_amount) AS total FROM notices "
            "WHERE value_amount IS NOT NULL AND value_currency='EUR'"
        ).fetchone()
        return {
            "notices": total,
            "relevant": relevant,
            "earliest": dates["lo"],
            "latest": dates["hi"],
            "with_eur_value": valued["n"],
            "eur_value_sum": valued["total"],
        }

    def anomaly_counts(self) -> dict[str, int]:
        """Count notices per anomaly code.

        Done in Python rather than SQL because the codes live in a JSON array;
        at this data volume (tens of thousands of rows) it is not worth a
        normalized side table yet.
        """
        counts: dict[str, int] = {}
        for (blob,) in self.conn.execute(
            "SELECT anomalies FROM notices WHERE anomalies IS NOT NULL "
            "AND anomalies != '[]'"
        ):
            for code in json.loads(blob):
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def unexpected_fields(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT field_name, first_seen, seen_count FROM field_observations "
                "WHERE is_expected=0 ORDER BY seen_count DESC"
            )
        )

    def notice_type_counts(self) -> dict[str, int]:
        return {
            (r["notice_type"] or "(none)"): r["n"]
            for r in self.conn.execute(
                "SELECT notice_type, COUNT(*) AS n FROM notices "
                "GROUP BY notice_type ORDER BY n DESC"
            )
        }


def _row_from_notice(
    notice: NormalizedNotice, *, profile: str, sha: str, ts: str
) -> dict[str, Any]:
    row = dataclasses.asdict(notice)
    for key in JSON_COLUMNS:
        row[key] = json.dumps(row.get(key) or [], ensure_ascii=False)
    for key in JSON_OBJECT_COLUMNS:
        row[key] = json.dumps(row.get(key) or {}, ensure_ascii=False)
    row["performance_anywhere"] = int(bool(row["performance_anywhere"]))
    row["is_relevant"] = int(bool(row["is_relevant"]))
    row["profile"] = profile
    row["payload_sha256"] = sha
    row["first_seen_at"] = ts
    row["last_seen_at"] = ts
    return row
