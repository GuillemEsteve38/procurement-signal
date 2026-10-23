"""Search query construction, kept out of the HTTP layer so it is testable.

Every user-supplied value reaches SQLite as a bound parameter. Nothing is
interpolated into SQL, including the sort key, which is mapped through an
allow-list rather than passed through.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Status = Literal["open", "awarded", "planned", "all"]

#: Notice-type prefixes. TED uses `cn-` for contract notices (a live tender),
#: `can-` for contract award notices, `pin-` for prior information.
STATUS_PREFIX: dict[str, str] = {
    "open": "cn-",
    "awarded": "can-",
    "planned": "pin-",
}

#: Sort keys are mapped, never interpolated from user input. NULLs last in
#: every case: a notice with no deadline should not head a deadline sort.
SORT_SQL: dict[str, str] = {
    "date": "publication_date DESC",
    "value": "value_amount IS NULL, value_amount DESC",
    "deadline": "deadline IS NULL, deadline ASC",
    "buyer": "buyer_name ASC",
}

#: Human labels for the CPV divisions that actually occur in this market.
CPV_DIVISION_LABELS: dict[str, str] = {
    "48": "48 — Software packages",
    "72": "72 — IT services",
    "79": "79 — Business services",
    "85": "85 — Health & social work",
    "33": "33 — Medical equipment",
    "32": "32 — Telecom equipment",
    "51": "51 — Installation",
    "50": "50 — Repair & maintenance",
    "45": "45 — Construction",
}

MAX_PER_PAGE = 100


@dataclass
class SearchFilters:
    """The query the user has asked for.

    Defaults are the client's actual working view: live tenders, tagged
    relevant, newest first. Everything else is one control away.
    """

    q: str = ""
    status: str = "open"
    relevant_only: bool = True
    hide_expired: bool = False
    has_requirements: bool = False
    cpv_division: str = ""
    buyer_country: str = ""
    min_value: float | None = None
    sort: str = "date"
    page: int = 1
    per_page: int = 25

    @classmethod
    def from_query_params(cls, params: Any) -> "SearchFilters":
        """Build from a Starlette QueryParams-like mapping.

        Anything unparseable falls back to the default rather than erroring: a
        malformed URL should show results, not a stack trace.
        """

        def flag(name: str, default: bool = False) -> bool:
            raw = params.get(name)
            if raw is None:
                return default
            return str(raw).lower() in ("1", "true", "on", "yes")

        def number(name: str) -> float | None:
            raw = (params.get(name) or "").strip().replace(",", "")
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        def integer(name: str, default: int, lo: int, hi: int) -> int:
            try:
                value = int(params.get(name) or default)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, value))

        status = (params.get("status") or "open").lower()
        if status not in ("open", "awarded", "planned", "all"):
            status = "open"

        sort = (params.get("sort") or "date").lower()
        if sort not in SORT_SQL:
            sort = "date"

        # "relevant" is absent from the query string on first load, where the
        # default applies, but present-and-empty when the user unticks it.
        has_submitted = "submitted" in params

        return cls(
            q=(params.get("q") or "").strip()[:200],
            status=status,
            relevant_only=flag("relevant", default=not has_submitted),
            hide_expired=flag("hide_expired"),
            has_requirements=flag("has_requirements"),
            cpv_division=(params.get("cpv") or "").strip()[:2],
            buyer_country=(params.get("country") or "").strip().upper()[:3],
            min_value=number("min_value"),
            sort=sort,
            page=integer("page", 1, 1, 10_000),
            per_page=integer("per_page", 25, 5, MAX_PER_PAGE),
        )

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def is_default(self) -> bool:
        default = SearchFilters()
        return all(
            getattr(self, name) == getattr(default, name)
            for name in (
                "q",
                "status",
                "relevant_only",
                "hide_expired",
                "has_requirements",
                "cpv_division",
                "buyer_country",
                "min_value",
            )
        )

    def query_string(self, **overrides: Any) -> str:
        """Rebuild the query string, so pagination and sort links keep state."""
        from urllib.parse import urlencode

        values: dict[str, Any] = {
            "submitted": "1",
            "q": self.q,
            "status": self.status,
            "sort": self.sort,
            "per_page": self.per_page,
            "page": self.page,
        }
        if self.relevant_only:
            values["relevant"] = "1"
        if self.hide_expired:
            values["hide_expired"] = "1"
        if self.has_requirements:
            values["has_requirements"] = "1"
        if self.cpv_division:
            values["cpv"] = self.cpv_division
        if self.buyer_country:
            values["country"] = self.buyer_country
        if self.min_value is not None:
            values["min_value"] = self.min_value
        values.update(overrides)
        return urlencode({k: v for k, v in values.items() if v not in ("", None)})


def build_where(filters: SearchFilters) -> tuple[str, list[Any]]:
    """Return a WHERE fragment and its bound parameters."""
    clauses: list[str] = []
    params: list[Any] = []

    if filters.q:
        # LIKE is enough at this data volume. It does not fold Spanish
        # accents, so "clinica" will not match "clínica" — see the note in
        # the web README; FTS5 with remove_diacritics is the upgrade path.
        needle = f"%{filters.q}%"
        clauses.append(
            "(title LIKE ? COLLATE NOCASE"
            " OR description LIKE ? COLLATE NOCASE"
            " OR buyer_name LIKE ? COLLATE NOCASE"
            " OR publication_number LIKE ?)"
        )
        params += [needle, needle, needle, needle]

    prefix = STATUS_PREFIX.get(filters.status)
    if prefix:
        clauses.append("notice_type LIKE ?")
        params.append(f"{prefix}%")

    if filters.relevant_only:
        clauses.append("is_relevant = 1")

    if filters.hide_expired:
        # Notices with no deadline are kept: absence of a deadline is not
        # evidence of expiry, and dropping them would silently hide live
        # tenders that simply did not publish one.
        clauses.append("(deadline IS NULL OR substr(deadline, 1, 10) >= ?)")
        params.append(dt.date.today().isoformat())

    if filters.has_requirements:
        clauses.append("requirements IS NOT NULL AND requirements != '{}'")

    if filters.cpv_division:
        clauses.append("substr(main_cpv, 1, 2) = ?")
        params.append(filters.cpv_division)

    if filters.buyer_country:
        clauses.append("buyer_country = ?")
        params.append(filters.buyer_country)

    if filters.min_value is not None:
        clauses.append("value_amount >= ?")
        params.append(filters.min_value)

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


ROW_COLUMNS = (
    "publication_number, publication_date, notice_type, title, buyer_name,"
    " buyer_country, value_amount, value_currency, value_source, deadline,"
    " lot_count, main_cpv, duration_months, is_relevant, matched_keywords,"
    " anomalies, notice_url,"
    " (requirements IS NOT NULL AND requirements != '{}') AS has_requirements"
)


class SearchRepo:
    """Read-only data access for the web app."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        # Read-only URI: the web process must never be able to write.
        conn = sqlite3.connect(
            f"file:{self.db_path.as_posix()}?mode=ro", uri=True, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def _cursor(self):
        """Open a connection and actually close it.

        ``with sqlite3.connect(...)`` is a *transaction* context manager, not a
        closing one — it commits or rolls back and leaves the handle open. Using
        it here leaked one connection per HTTP request.
        """
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def search(self, filters: SearchFilters) -> tuple[list[dict[str, Any]], int]:
        where, params = build_where(filters)
        order = SORT_SQL[filters.sort]

        with self._cursor() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM notices WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT {ROW_COLUMNS} FROM notices WHERE {where}"
                f" ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, filters.per_page, filters.offset],
            ).fetchall()
        return [_decode(dict(row)) for row in rows], total

    def facets(self, filters: SearchFilters) -> dict[str, Any]:
        """Counts that help the user understand what they are looking at."""
        where, params = build_where(filters)
        with self._cursor() as conn:
            agg = conn.execute(
                "SELECT COUNT(*) n,"
                " SUM(CASE WHEN value_currency='EUR' THEN value_amount END) eur,"
                " COUNT(deadline) with_deadline"
                f" FROM notices WHERE {where}",
                params,
            ).fetchone()
            divisions = conn.execute(
                "SELECT substr(main_cpv,1,2) d, COUNT(*) n FROM notices"
                " WHERE main_cpv IS NOT NULL GROUP BY d ORDER BY n DESC"
            ).fetchall()
            countries = conn.execute(
                "SELECT buyer_country c, COUNT(*) n FROM notices"
                " WHERE buyer_country IS NOT NULL GROUP BY c ORDER BY n DESC"
                " LIMIT 25"
            ).fetchall()
        return {
            "count": agg["n"] or 0,
            "eur_total": agg["eur"] or 0.0,
            "with_deadline": agg["with_deadline"] or 0,
            "divisions": [
                {
                    "value": r["d"],
                    "label": CPV_DIVISION_LABELS.get(r["d"], f"{r['d']} — other"),
                    "count": r["n"],
                }
                for r in divisions
                if r["d"]
            ],
            "countries": [{"value": r["c"], "count": r["n"]} for r in countries],
        }

    def notice(self, publication_number: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT * FROM notices WHERE publication_number = ?",
                (publication_number,),
            ).fetchone()
            if row is None:
                return None
            notice = _decode(dict(row))
            notice["extractions"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT extractor, model, effort, prompt_version,"
                    " cpv_division, work_category, is_health_related,"
                    " required_certifications, renewable, summary_es,"
                    " cost_usd, latency_ms, ok, error, extracted_at"
                    " FROM extractions WHERE publication_number = ?"
                    " ORDER BY extracted_at DESC",
                    (publication_number,),
                )
            ]
        for extraction in notice["extractions"]:
            extraction["required_certifications"] = _loads(
                extraction.get("required_certifications"), []
            )
        return notice

    def health(self) -> dict[str, Any]:
        """Cheap liveness probe: can we read, and how fresh is the data."""
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, MAX(publication_date) latest,"
                " MAX(last_seen_at) last_ingest FROM notices"
            ).fetchone()
            schema = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        return {
            "status": "ok",
            "notices": row["n"] or 0,
            "latest_publication": row["latest"],
            "last_ingest_at": row["last_ingest"],
            "schema_version": schema["value"] if schema else None,
        }


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "matched_keywords",
        "anomalies",
        "cpv_codes",
        "cpv_divisions",
        "lot_titles",
        "lot_descriptions",
        "nuts_codes",
        "performance_countries",
        "winner_names",
        "buyer_identifiers",
        "unknown_fields",
    ):
        if key in row:
            row[key] = _loads(row[key], [])
    if "requirements" in row:
        row["requirements"] = _loads(row["requirements"], {})
    return row


def _loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
