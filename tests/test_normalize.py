"""Normalizer tests.

The fixtures are abridged copies of real TED responses, not invented shapes.
NOTICE_737081 is notice 737081-2026 (Servicio Madrileño de Salud, three lots,
€7,053,801.73) — it is the notice that motivated most of the normalizer.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from procsignal.normalize import (
    A_BAD_DATE,
    A_LOT_CARDINALITY_MISMATCH,
    A_MULTI_BUYER_ID_SCHEME,
    A_MULTI_CURRENCY,
    A_NO_VALUE,
    A_UNKNOWN_FIELDS,
    A_VALUE_FROM_LOT_SUM,
    normalize,
    parse_money,
    parse_ted_date,
    pick_language,
    unique,
)
from procsignal.profile import NOTICE_FIELDS, SPANISH_HEALTH_IT
from procsignal.store import Store, payload_hash

NOTICE_737081 = {
    "publication-number": "737081-2026",
    "notice-identifier": "8301d058-3755-4f17-b847-831cd9669003",
    "publication-date": "2026-10-23+02:00",
    "notice-type": "can-modif",
    "notice-subtype": "38",
    "form-type": "cont-modif",
    "title-proc": {
        "spa": "«SERVICIOS DE MANTENIMIENTO DE APLICACIONES DE LA FACTORÍA DE "
        "SOFTWARE DE LA DIRECCIÓN GENERAL DE SALUD DIGITAL (3 LOTES)»"
    },
    "notice-title": {
        "eng": "Spain – IT services – SERVICIOS DE MANTENIMIENTO...",
        "hun": "Spanyolország – IT-szolgáltatások – ...",
    },
    "description-proc": {
        "spa": "Servicio destinado al mantenimiento de las aplicaciones "
        "pertenecientes al Servicio Madrileño de Salud."
    },
    "title-lot": {
        "spa": [
            "Asistencial y Corporativo. (73 aplicaciones)",
            "Ciudadano e Historia Clínica. (73 aplicaciones)",
            "Farmacia y Salud Pública. (40 aplicaciones)",
        ]
    },
    "description-lot": {
        "spa": [
            "Asistencial y Corporativo. (73 aplicaciones)",
            "Ciudadano e Historia Clínica. (73 aplicaciones)",
            "Farmacia y Salud Pública. (40 aplicaciones)",
        ]
    },
    "buyer-name": {"spa": ["Consejería de Digitalización"]},
    "buyer-city": {"mul": ["MADRID"]},
    "buyer-country": ["ESP"],
    "buyer-identifier": ["S-7800001E"],
    "classification-cpv": [
        "72000000",
        "72212900",
        "72000000",
        "72000000",
        "72000000",
        "72212900",
        "72212900",
        "72212900",
    ],
    "main-classification-proc": ["72000000"],
    "contract-nature": ["services", "services", "services", "services"],
    "total-value": 7053801.73,
    "total-value-cur": ["EUR"],
    "place-of-performance": [
        "ES300",
        "ESP",
        "ES300",
        "ES300",
        "ES300",
        "ESP",
        "ESP",
        "ESP",
    ],
    "place-of-performance-country-lot": ["ESP", "ESP", "ESP"],
    "place-of-performance-subdiv-lot": ["ES300", "ES300", "ES300"],
    "links": {
        "html": {
            "SPA": "https://ted.europa.eu/es/notice/-/detail/737081-2026",
            "ENG": "https://ted.europa.eu/en/notice/-/detail/737081-2026",
        }
    },
}

#: Real notice with two buyer identifiers under different national schemes.
NOTICE_ENAIRE = {
    "publication-number": "737080-2026",
    "publication-date": "2026-10-23+02:00",
    "notice-type": "can-modif",
    "buyer-name": {"spa": ["Dirección Económico-Financiera de ENAIRE"]},
    "buyer-identifier": ["50494010087781", "Q2822001J"],
    "buyer-country": ["ESP"],
    "classification-cpv": ["72212218", "72212218"],
    "description-proc": {"spa": "Continuidad de nuevos desarrollos y consultoría"},
    "total-value": 1_000_000.0,
    "total-value-cur": ["EUR"],
}

#: Location-independent EU-agency notice; 'anyw' is not a country code.
NOTICE_ANYWHERE = {
    "publication-number": "737135-2026",
    "publication-date": "2026-10-23+02:00",
    "notice-type": "pin-only",
    "place-of-performance": ["anyw", "anyw"],
    "total-value": 200_000_000,
    "total-value-cur": ["EUR"],
    "classification-cpv": ["72000000"],
}


class TestPrimitives(unittest.TestCase):
    def test_unique_preserves_first_seen_order(self):
        self.assertEqual(unique(["ES300", "ESP", "ES300", "ESP"]), ["ES300", "ESP"])

    def test_pick_language_prefers_spanish_then_english(self):
        value = {"eng": "English title", "spa": "Título español", "hun": "..."}
        self.assertEqual(pick_language(value), ("Título español", "spa"))
        self.assertEqual(pick_language({"eng": "Only English"}), ("Only English", "eng"))

    def test_pick_language_falls_back_to_an_untagged_language(self):
        # A language we have no preference for must still beat returning None.
        text, lang = pick_language({"hun": "Magyar"})
        self.assertEqual(text, "Magyar")
        self.assertEqual(lang, "hun")

    def test_pick_language_takes_first_entry_of_a_per_lot_list(self):
        self.assertEqual(
            pick_language({"spa": ["Lote 1", "Lote 2"]}), ("Lote 1", "spa")
        )

    def test_pick_language_handles_plain_strings_and_missing_values(self):
        self.assertEqual(pick_language("plain"), ("plain", None))
        self.assertEqual(pick_language(None), (None, None))
        self.assertEqual(pick_language({}), (None, None))
        self.assertEqual(pick_language({"spa": "   "}), (None, None))

    def test_parse_date_handles_a_date_carrying_a_utc_offset(self):
        # This is the shape that breaks date.fromisoformat.
        self.assertEqual(
            parse_ted_date("2026-10-23+02:00"), ("2026-10-23", "+02:00", True)
        )

    def test_parse_date_handles_full_timestamps_and_bare_dates(self):
        self.assertEqual(
            parse_ted_date("2016-11-04T17:00:00+01:00"),
            ("2016-11-04T17:00:00", "+01:00", True),
        )
        self.assertEqual(parse_ted_date("2026-01-05"), ("2026-01-05", None, True))
        self.assertEqual(parse_ted_date("2026-01-05Z"), ("2026-01-05", "+00:00", True))

    def test_parse_date_reports_failure_rather_than_raising(self):
        self.assertEqual(parse_ted_date("not a date"), (None, None, False))
        self.assertEqual(parse_ted_date("2026-13-45"), (None, None, False))
        self.assertEqual(parse_ted_date(None), (None, None, True))

    def test_parse_date_unwraps_single_element_lists(self):
        self.assertEqual(
            parse_ted_date(["2026-10-23+02:00"]), ("2026-10-23", "+02:00", True)
        )

    def test_parse_money_pairs_amount_with_separately_stored_currency(self):
        self.assertEqual(parse_money(7053801.73, ["EUR"]), (7053801.73, "EUR", True))

    def test_parse_money_sums_lot_amounts_and_detects_mixed_currencies(self):
        self.assertEqual(parse_money([100.0, 250.0], ["EUR", "EUR"]), (350.0, "EUR", True))
        amount, currency, single = parse_money([100.0, 250.0], ["EUR", "SEK"])
        self.assertEqual(amount, 350.0)
        self.assertFalse(single)
        self.assertEqual(currency, "EUR")

    def test_parse_money_with_nothing_to_parse(self):
        self.assertEqual(parse_money(None, None), (None, None, True))


class TestNormalizeRealNotice(unittest.TestCase):
    def setUp(self):
        self.n = normalize(
            NOTICE_737081,
            known_fields=NOTICE_FIELDS,
            relevance_keywords=SPANISH_HEALTH_IT.relevance_keywords,
        )

    def test_prefers_the_procurement_title_over_teds_headline(self):
        self.assertTrue(self.n.title.startswith("«SERVICIOS DE MANTENIMIENTO"))
        self.assertEqual(self.n.title_lang, "spa")

    def test_collapses_repeated_cpv_codes_and_keeps_the_main_one(self):
        self.assertEqual(self.n.cpv_codes, ["72000000", "72212900"])
        self.assertEqual(self.n.main_cpv, "72000000")
        self.assertEqual(self.n.cpv_divisions, ["72"])

    def test_splits_place_of_performance_into_nuts_and_countries(self):
        self.assertEqual(self.n.nuts_codes, ["ES300"])
        self.assertEqual(self.n.performance_countries, ["ESP"])
        self.assertFalse(self.n.performance_anywhere)

    def test_date_offset_is_kept_rather_than_normalized_away(self):
        self.assertEqual(self.n.publication_date, "2026-10-23")
        self.assertEqual(self.n.publication_offset, "+02:00")

    def test_value_records_its_provenance(self):
        self.assertEqual(self.n.value_amount, 7053801.73)
        self.assertEqual(self.n.value_currency, "EUR")
        self.assertEqual(self.n.value_source, "total")

    def test_lot_count_comes_from_the_widest_per_lot_array(self):
        self.assertEqual(self.n.lot_count, 3)
        self.assertEqual(len(self.n.lot_titles), 3)

    def test_health_relevance_is_detected_from_spanish_text(self):
        self.assertTrue(self.n.is_relevant)
        self.assertIn("salud", self.n.matched_keywords)

    def test_notice_url_prefers_the_spanish_page(self):
        self.assertEqual(
            self.n.notice_url, "https://ted.europa.eu/es/notice/-/detail/737081-2026"
        )

    def test_a_clean_notice_raises_only_the_expected_anomaly(self):
        # This fixture was captured before the requirement-text fields were
        # added to the profile, so it legitimately has none. Everything else
        # about it is clean.
        self.assertEqual(self.n.anomalies, ["no_requirement_text"])


class TestNormalizeEdgeCases(unittest.TestCase):
    def test_two_buyer_id_schemes_are_flagged_not_merged(self):
        n = normalize(NOTICE_ENAIRE, known_fields=NOTICE_FIELDS)
        self.assertEqual(n.buyer_identifiers, ["50494010087781", "Q2822001J"])
        self.assertIn(A_MULTI_BUYER_ID_SCHEME, n.anomalies)

    def test_anywhere_is_not_stored_as_a_country(self):
        n = normalize(NOTICE_ANYWHERE, known_fields=NOTICE_FIELDS)
        self.assertTrue(n.performance_anywhere)
        self.assertEqual(n.performance_countries, [])
        self.assertEqual(n.nuts_codes, [])

    def test_missing_value_is_flagged(self):
        n = normalize(
            {"publication-number": "1-2026", "publication-date": "2026-01-01"},
            known_fields=NOTICE_FIELDS,
        )
        self.assertIn(A_NO_VALUE, n.anomalies)
        self.assertIsNone(n.value_amount)

    def test_lot_sum_is_flagged_as_a_derivation(self):
        n = normalize(
            {
                "publication-number": "2-2026",
                "estimated-value-lot": [100.0, 200.0],
                "estimated-value-cur-lot": ["EUR", "EUR"],
            },
            known_fields=NOTICE_FIELDS,
        )
        self.assertEqual(n.value_amount, 300.0)
        self.assertEqual(n.value_source, "estimated_lot_sum")
        self.assertIn(A_VALUE_FROM_LOT_SUM, n.anomalies)

    def test_a_sum_across_currencies_is_refused_not_rounded(self):
        n = normalize(
            {
                "publication-number": "3-2026",
                "estimated-value-lot": [100.0, 200.0],
                "estimated-value-cur-lot": ["EUR", "SEK"],
            },
            known_fields=NOTICE_FIELDS,
        )
        self.assertIsNone(n.value_amount)
        self.assertIn(A_MULTI_CURRENCY, n.anomalies)
        self.assertIn(A_NO_VALUE, n.anomalies)

    def test_disagreeing_lot_arrays_are_flagged(self):
        n = normalize(
            {
                "publication-number": "4-2026",
                "title-lot": {"spa": ["A", "B", "C"]},
                "estimated-value-lot": [1.0, 2.0],
                "estimated-value-cur-lot": ["EUR"],
            },
            known_fields=NOTICE_FIELDS,
        )
        self.assertEqual(n.lot_count, 3)
        self.assertIn(A_LOT_CARDINALITY_MISMATCH, n.anomalies)

    def test_an_unparseable_date_does_not_lose_the_notice(self):
        n = normalize(
            {"publication-number": "5-2026", "publication-date": "soon"},
            known_fields=NOTICE_FIELDS,
        )
        self.assertEqual(n.publication_number, "5-2026")
        self.assertIn(A_BAD_DATE, n.anomalies)

    def test_a_new_api_field_is_reported_as_schema_drift(self):
        payload = dict(NOTICE_737081)
        payload["some-brand-new-field"] = ["surprise"]
        n = normalize(payload, known_fields=NOTICE_FIELDS)
        self.assertIn(A_UNKNOWN_FIELDS, n.anomalies)
        self.assertEqual(n.unknown_fields, ["some-brand-new-field"])

    def test_only_a_missing_primary_key_is_fatal(self):
        with self.assertRaises(ValueError):
            normalize({"publication-date": "2026-01-01"})

    def test_description_falls_back_to_concatenated_lot_descriptions(self):
        payload = {
            "publication-number": "6-2026",
            "description-lot": {"spa": ["Lote uno", "Lote dos"]},
        }
        n = normalize(payload, known_fields=NOTICE_FIELDS)
        self.assertEqual(n.description, "Lote uno\n\nLote dos")
        self.assertEqual(n.description_lang, "spa")


class TestProfileQuery(unittest.TestCase):
    def test_query_uses_teds_bare_yyyymmdd_date_format(self):
        q = SPANISH_HEALTH_IT.query(dt.date(2026, 9, 1), dt.date(2026, 10, 23))
        self.assertIn("publication-date>=20260901", q)
        self.assertIn("publication-date<=20261023", q)

    def test_query_ors_cpv_codes_and_ands_the_rest(self):
        q = SPANISH_HEALTH_IT.query(dt.date(2026, 9, 1))
        self.assertIn("(classification-cpv=72000000 OR classification-cpv=48000000)", q)
        self.assertIn("(buyer-country=ESP)", q)
        self.assertTrue(q.endswith("SORT BY publication-number DESC"))


class TestStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reingesting_the_same_notice_is_idempotent(self):
        notice = normalize(NOTICE_737081, known_fields=NOTICE_FIELDS)
        with Store(self.db) as store:
            self.assertEqual(
                store.upsert(notice, NOTICE_737081, profile="t"), "inserted"
            )
            self.assertEqual(
                store.upsert(notice, NOTICE_737081, profile="t"), "unchanged"
            )
            store.commit()
            self.assertEqual(store.summary()["notices"], 1)

    def test_a_changed_payload_is_detected_as_an_update(self):
        with Store(self.db) as store:
            store.upsert(
                normalize(NOTICE_737081, known_fields=NOTICE_FIELDS),
                NOTICE_737081,
                profile="t",
            )
            revised = dict(NOTICE_737081, **{"total-value": 8_000_000.0})
            self.assertEqual(
                store.upsert(
                    normalize(revised, known_fields=NOTICE_FIELDS),
                    revised,
                    profile="t",
                ),
                "updated",
            )
            store.commit()
            row = store.conn.execute(
                "SELECT value_amount, first_seen_at, last_seen_at FROM notices"
            ).fetchone()
            self.assertEqual(row["value_amount"], 8_000_000.0)

    def test_an_unchanged_payload_still_picks_up_new_relevance_tags(self):
        with Store(self.db) as store:
            store.upsert(normalize(NOTICE_737081), NOTICE_737081, profile="a")
            retagged = normalize(NOTICE_737081, relevance_keywords=("salud",))
            self.assertTrue(retagged.is_relevant)
            self.assertEqual(
                store.upsert(retagged, NOTICE_737081, profile="b"), "unchanged"
            )
            store.commit()
            row = store.conn.execute(
                "SELECT is_relevant, profile FROM notices"
            ).fetchone()
            self.assertEqual((row["is_relevant"], row["profile"]), (1, "b"))

    def test_payload_hash_is_order_independent(self):
        a = {"b": 1, "a": [1, 2]}
        b = {"a": [1, 2], "b": 1}
        self.assertEqual(payload_hash(a), payload_hash(b))

    def test_raw_payload_is_retained_verbatim(self):
        with Store(self.db) as store:
            store.upsert(
                normalize(NOTICE_737081, known_fields=NOTICE_FIELDS),
                NOTICE_737081,
                profile="t",
            )
            store.commit()
            blob = store.conn.execute(
                "SELECT payload FROM raw_notices WHERE publication_number=?",
                ("737081-2026",),
            ).fetchone()["payload"]
            self.assertEqual(json.loads(blob), NOTICE_737081)

    def test_unexpected_api_fields_are_recorded_for_drift_detection(self):
        payload = dict(NOTICE_737081)
        payload["brand-new-field"] = 1
        with Store(self.db) as store:
            store.observe_fields(payload, expected=NOTICE_FIELDS)
            store.commit()
            names = [r["field_name"] for r in store.unexpected_fields()]
            self.assertEqual(names, ["brand-new-field"])

    def test_accented_text_survives_a_round_trip(self):
        # Guards the Windows cp1252 trap: if any file or DB handle defaults to
        # the ANSI code page, this is where it shows up.
        with Store(self.db) as store:
            store.upsert(
                normalize(NOTICE_737081, known_fields=NOTICE_FIELDS),
                NOTICE_737081,
                profile="t",
            )
            store.commit()
            title = store.conn.execute("SELECT title FROM notices").fetchone()["title"]
            self.assertIn("FACTORÍA", title)
            self.assertIn("DIRECCIÓN", title)


if __name__ == "__main__":
    unittest.main()
