"""Web layer tests.

Filter construction is tested directly, because that is where a bug silently
returns the wrong notices. The HTTP layer is tested through FastAPI's
TestClient against a temporary database, so no server and no network.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from procsignal.extract import Extraction
from procsignal.normalize import normalize
from procsignal.profile import NOTICE_FIELDS
from procsignal.store import Store
from procsignal.web.app import _deadline_class, _fmt_eur, create_app
from procsignal.web.queries import SORT_SQL, SearchFilters, SearchRepo, build_where

TODAY = dt.date.today()
FUTURE = (TODAY + dt.timedelta(days=20)).isoformat()
SOON = (TODAY + dt.timedelta(days=3)).isoformat()
PAST = (TODAY - dt.timedelta(days=30)).isoformat()


def _notice(
    number: str,
    *,
    notice_type: str = "cn-standard",
    title: str = "Servicio de mantenimiento",
    description: str = "Mantenimiento de aplicaciones",
    buyer: str = "Servicio Gallego de Salud",
    cpv: str = "72000000",
    value: float | None = 1_000_000.0,
    deadline: str | None = None,
    keywords: tuple[str, ...] = ("salud",),
    requirements: bool = True,
) -> dict:
    raw: dict = {
        "publication-number": number,
        "publication-date": "2026-10-01+02:00",
        "notice-type": notice_type,
        "title-proc": {"spa": title},
        "description-proc": {"spa": description},
        "buyer-name": {"spa": [buyer]},
        "buyer-country": ["ESP"],
        "classification-cpv": [cpv],
        "main-classification-proc": [cpv],
        "contract-nature": ["services"],
    }
    if value is not None:
        raw["total-value"] = value
        raw["total-value-cur"] = ["EUR"]
    if deadline is not None:
        raw["deadline-receipt-request"] = deadline
    if requirements:
        raw["award-criterion-description-lot"] = {"spa": ["Oferta económica"]}
    return raw


def _seed(db: Path) -> None:
    rows = [
        _notice("100-2026", deadline=FUTURE, value=5_000_000.0),
        _notice("101-2026", deadline=SOON, value=250_000.0),
        _notice("102-2026", deadline=PAST, value=12_000_000.0),
        _notice("103-2026", notice_type="can-standard", deadline=None, value=900_000.0),
        _notice(
            "104-2026",
            title="Reforma de pavimentos en viales municipales",
            description="Conservación de pavimentos. Incluye la Calle Salud.",
            buyer="Ayuntamiento de Palma",
            cpv="45000000",
            value=58_700_000.0,
            requirements=False,
        ),
        _notice(
            "105-2026",
            title="Suministro de licencias",
            description="Licencias de software ofimático",
            buyer="Diputacion de Teruel",
            cpv="48000000",
            value=None,
            keywords=(),
            requirements=False,
        ),
    ]
    with Store(db) as store:
        for raw in rows:
            notice = normalize(
                raw,
                known_fields=NOTICE_FIELDS,
                relevance_keywords=("salud", "hospital"),
            )
            store.upsert(notice, raw, profile="test")
        store.save_extraction(
            "100-2026",
            Extraction(
                extractor="stub",
                model="stub",
                effort="n/a",
                cpv_division="72",
                work_category="application_maintenance",
                is_health_related=True,
                required_certifications=["ENS Medio"],
                summary_es="Mantenimiento de aplicaciones sanitarias.",
            ),
        )
        store.commit()


class WebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls._tmp.name) / "web.sqlite3"
        _seed(cls.db)
        cls.repo = SearchRepo(cls.db)
        cls.client = TestClient(create_app(cls.db))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class TestFilterParsing(WebTestCase):
    def test_defaults_are_the_clients_working_view(self):
        f = SearchFilters()
        self.assertEqual(f.status, "open")
        self.assertTrue(f.relevant_only)
        self.assertEqual(f.sort, "date")

    def test_relevant_defaults_on_for_a_bare_url_and_off_when_unticked(self):
        # A checkbox sends nothing when unticked, so the form posts a hidden
        # "submitted" marker to tell "first load" from "deliberately off".
        self.assertTrue(SearchFilters.from_query_params({}).relevant_only)
        self.assertFalse(
            SearchFilters.from_query_params({"submitted": "1"}).relevant_only
        )
        self.assertTrue(
            SearchFilters.from_query_params(
                {"submitted": "1", "relevant": "1"}
            ).relevant_only
        )

    def test_a_malformed_url_falls_back_instead_of_erroring(self):
        f = SearchFilters.from_query_params(
            {"status": "nonsense", "sort": "'; DROP TABLE notices--",
             "page": "abc", "per_page": "99999", "min_value": "not a number"}
        )
        self.assertEqual(f.status, "open")
        self.assertIn(f.sort, SORT_SQL)
        self.assertEqual(f.page, 1)
        self.assertLessEqual(f.per_page, 100)
        self.assertIsNone(f.min_value)

    def test_query_string_round_trips_so_pagination_keeps_state(self):
        f = SearchFilters.from_query_params(
            {"submitted": "1", "q": "salud", "cpv": "72", "relevant": "1"}
        )
        qs = f.query_string(page=3)
        self.assertIn("q=salud", qs)
        self.assertIn("cpv=72", qs)
        self.assertIn("relevant=1", qs)
        self.assertIn("page=3", qs)

    def test_a_thousands_separator_in_min_value_is_accepted(self):
        self.assertEqual(
            SearchFilters.from_query_params({"min_value": "1,000,000"}).min_value,
            1_000_000.0,
        )


class TestWhereClause(WebTestCase):
    def test_no_user_value_is_ever_interpolated_into_sql(self):
        f = SearchFilters.from_query_params(
            {"submitted": "1", "q": "'; DROP TABLE notices;--", "country": "ESP"}
        )
        where, params = build_where(f)
        self.assertNotIn("DROP", where)
        self.assertIn("%'; DROP TABLE notices;--%", params)

    def test_the_injection_attempt_returns_rows_rather_than_breaking(self):
        f = SearchFilters.from_query_params(
            {"submitted": "1", "q": "'; DROP TABLE notices;--"}
        )
        rows, total = self.repo.search(f)
        self.assertEqual(total, 0)
        # And the table is still there.
        self.assertGreater(self.repo.health()["notices"], 0)

    def test_hiding_expired_keeps_notices_with_no_deadline(self):
        # Absence of a deadline is not evidence of expiry; dropping those rows
        # would silently hide live tenders that did not publish one.
        f = SearchFilters(status="all", relevant_only=False, hide_expired=True)
        rows, _ = self.repo.search(f)
        numbers = {r["publication_number"] for r in rows}
        self.assertIn("103-2026", numbers)  # no deadline
        self.assertIn("100-2026", numbers)  # future deadline
        self.assertNotIn("102-2026", numbers)  # past deadline

    def test_stage_filter_separates_open_tenders_from_awards(self):
        open_rows, _ = self.repo.search(
            SearchFilters(status="open", relevant_only=False)
        )
        awarded_rows, _ = self.repo.search(
            SearchFilters(status="awarded", relevant_only=False)
        )
        self.assertNotIn(
            "103-2026", {r["publication_number"] for r in open_rows}
        )
        self.assertEqual(
            {r["publication_number"] for r in awarded_rows}, {"103-2026"}
        )

    def test_cpv_division_filters_on_the_first_two_digits(self):
        rows, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, cpv_division="45")
        )
        self.assertEqual({r["publication_number"] for r in rows}, {"104-2026"})

    def test_min_value_excludes_notices_with_no_published_value(self):
        rows, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, min_value=1_000_000)
        )
        numbers = {r["publication_number"] for r in rows}
        self.assertNotIn("105-2026", numbers)  # value is NULL
        self.assertNotIn("101-2026", numbers)  # 250k
        self.assertIn("102-2026", numbers)  # 12m

    def test_requirement_filter_excludes_notices_with_an_empty_object(self):
        rows, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, has_requirements=True)
        )
        numbers = {r["publication_number"] for r in rows}
        self.assertNotIn("104-2026", numbers)
        self.assertIn("100-2026", numbers)

    def test_search_matches_buyer_and_publication_number_too(self):
        by_buyer, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, q="Palma")
        )
        self.assertEqual({r["publication_number"] for r in by_buyer}, {"104-2026"})

        by_number, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, q="105-2026")
        )
        self.assertEqual({r["publication_number"] for r in by_number}, {"105-2026"})

    def test_sorting_by_value_puts_nulls_last_not_first(self):
        rows, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, sort="value")
        )
        self.assertEqual(rows[0]["publication_number"], "104-2026")  # 58.7m
        self.assertIsNone(rows[-1]["value_amount"])

    def test_sorting_by_deadline_puts_nulls_last_not_first(self):
        rows, _ = self.repo.search(
            SearchFilters(status="all", relevant_only=False, sort="deadline")
        )
        self.assertIsNotNone(rows[0]["deadline"])
        self.assertIsNone(rows[-1]["deadline"])

    def test_pagination_does_not_overlap_or_skip(self):
        base = dict(status="all", relevant_only=False, per_page=2, sort="date")
        page1, total = self.repo.search(SearchFilters(**base, page=1))
        page2, _ = self.repo.search(SearchFilters(**base, page=2))
        self.assertEqual(total, 6)
        self.assertEqual(len(page1), 2)
        self.assertFalse(
            {r["publication_number"] for r in page1}
            & {r["publication_number"] for r in page2}
        )


class TestHttp(WebTestCase):
    def test_the_index_renders_and_defaults_to_open_relevant_notices(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Procurement Signal", response.text)
        # 100/101/102 are open + relevant; 103 is an award, 105 is not relevant.
        self.assertIn("100-2026", response.text)
        self.assertNotIn("103-2026", response.text)

    def test_an_htmx_request_returns_only_the_results_fragment(self):
        full = self.client.get("/")
        fragment = self.client.get("/", headers={"hx-request": "true"})
        self.assertEqual(fragment.status_code, 200)
        self.assertIn('id="results"', fragment.text)
        # The shell is absent from the fragment but present in the full page.
        self.assertNotIn("<!doctype html>", fragment.text.lower())
        self.assertIn("<!doctype html>", full.text.lower())

    def test_matched_keywords_are_rendered_so_a_false_positive_is_visible(self):
        response = self.client.get("/?submitted=1&status=all&relevant=1&q=Palma")
        self.assertIn("104-2026", response.text)
        self.assertIn("salud", response.text)
        # A single-keyword match is marked as weak rather than presented as a hit.
        self.assertIn("weak", response.text)

    def test_the_notice_page_shows_requirements_and_extractions(self):
        response = self.client.get("/notice/100-2026")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Oferta económica", response.text)
        self.assertIn("Award criteria", response.text)
        self.assertIn("application_maintenance", response.text)
        self.assertIn("ENS Medio", response.text)

    def test_an_unknown_notice_returns_404_with_a_usable_page(self):
        response = self.client.get("/notice/999-1999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("999-1999", response.text)

    def test_healthz_reports_row_count_and_freshness(self):
        payload = self.client.get("/healthz").json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["notices"], 6)
        self.assertIsNotNone(payload["last_ingest_at"])
        self.assertEqual(payload["schema_version"], "2")

    def test_healthz_reports_503_when_the_database_is_missing(self):
        client = TestClient(
            create_app(Path("/nonexistent/nope.sqlite3")), raise_server_exceptions=False
        )
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")

    def test_the_json_api_mirrors_the_ui_filters(self):
        payload = self.client.get(
            "/api/search?submitted=1&status=all&cpv=45"
        ).json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["publication_number"], "104-2026")

    def test_an_empty_result_set_explains_the_likely_cause(self):
        response = self.client.get("/?submitted=1&q=zzzznothing&relevant=1")
        self.assertIn("Nothing matches", response.text)
        self.assertIn("Keyword-relevant only", response.text)

    def test_static_assets_are_served_locally_not_from_a_cdn(self):
        index = self.client.get("/").text
        self.assertIn("/static/htmx.min.js", index)
        self.assertNotIn("unpkg.com", index)
        self.assertNotIn("cdn.jsdelivr", index)
        self.assertEqual(self.client.get("/static/htmx.min.js").status_code, 200)

    def test_the_web_process_cannot_write_to_the_database(self):
        import sqlite3

        conn = self.repo._connect()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM notices")
        conn.close()


class TestFormatters(unittest.TestCase):
    def test_values_are_abbreviated_for_a_dense_table(self):
        self.assertEqual(_fmt_eur(58_700_000), "58.7M")
        self.assertEqual(_fmt_eur(250_000), "250k")
        self.assertEqual(_fmt_eur(42), "42")
        self.assertEqual(_fmt_eur(None), "—")
        self.assertEqual(_fmt_eur("not a number"), "—")

    def test_deadlines_are_classified_for_colouring(self):
        self.assertEqual(_deadline_class(PAST), "expired")
        self.assertEqual(_deadline_class(SOON), "soon")
        self.assertEqual(_deadline_class(FUTURE), "open")
        self.assertEqual(_deadline_class(None), "none")
        self.assertEqual(_deadline_class("garbage"), "none")


if __name__ == "__main__":
    unittest.main()
