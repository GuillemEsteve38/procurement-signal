"""Extractor, eval-scoring and new-normalizer tests.

The Spanish text fixtures are taken from real notices in the ingested data,
including the €58.7m Palma road-pavement contract that the keyword filter
wrongly tags health-related.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from procsignal.evaluate import Case, score
from procsignal.extract import (
    CPV_DIVISIONS,
    EXTRACTION_SCHEMA,
    WORK_CATEGORIES,
    Extraction,
    NoticeText,
    StubExtractor,
    _from_payload,
    _normalise_cert,
    build_extractor,
    estimate_cost_usd,
)
from procsignal.normalize import (
    A_NO_REQUIREMENT_TEXT,
    A_UNKNOWN_DURATION_UNIT,
    normalize,
    requirement_text,
)
from procsignal.profile import NOTICE_FIELDS
from procsignal.store import Store
from procsignal.ted import MAX_FIELDS_PER_PAGE, clamp_limit


class TestFieldsPerPageCap(unittest.TestCase):
    """TED rejects fields x limit > 10000, undocumented and easy to trip."""

    def test_limit_is_clamped_so_the_product_stays_under_the_cap(self):
        limit = clamp_limit(250, 45)
        self.assertLessEqual(limit * 45, MAX_FIELDS_PER_PAGE)
        self.assertEqual(limit, 222)

    def test_a_small_field_set_is_left_alone(self):
        self.assertEqual(clamp_limit(250, 10), 250)

    def test_the_real_profile_field_set_produces_a_usable_page_size(self):
        limit = clamp_limit(250, len(NOTICE_FIELDS))
        self.assertGreaterEqual(limit, 50)
        self.assertLessEqual(limit * len(NOTICE_FIELDS), MAX_FIELDS_PER_PAGE)

    def test_a_degenerate_field_count_cannot_produce_a_zero_page_size(self):
        self.assertEqual(clamp_limit(250, 0), 250)
        self.assertGreaterEqual(clamp_limit(250, 100_000), 1)


class TestDurationNormalisation(unittest.TestCase):
    def test_years_are_converted_to_months(self):
        n = normalize(
            {
                "publication-number": "1-2026",
                "duration-period-value-lot": [2],
                "duration-period-unit-lot": ["YEAR"],
            },
            known_fields=NOTICE_FIELDS,
        )
        self.assertEqual(n.duration_months, 24)
        self.assertEqual(n.duration_raw, "2 YEAR")

    def test_the_longest_lot_duration_wins(self):
        n = normalize(
            {
                "publication-number": "2-2026",
                "duration-period-value-lot": [6, 36, 12],
                "duration-period-unit-lot": ["MONTH", "MONTH", "MONTH"],
            },
            known_fields=NOTICE_FIELDS,
        )
        self.assertEqual(n.duration_months, 36)

    def test_an_unknown_unit_is_flagged_not_guessed(self):
        n = normalize(
            {
                "publication-number": "3-2026",
                "duration-period-value-lot": [5],
                "duration-period-unit-lot": ["FORTNIGHT"],
            },
            known_fields=NOTICE_FIELDS,
        )
        self.assertIsNone(n.duration_months)
        self.assertIn(A_UNKNOWN_DURATION_UNIT, n.anomalies)
        # The raw pair survives, so the conversion can be fixed later.
        self.assertEqual(n.duration_raw, "5 FORTNIGHT")


class TestRequirementText(unittest.TestCase):
    RAW = {
        "publication-number": "733957-2026",
        "selection-criterion-description-lot": {
            "spa": ["Cifra anual de negocio", "Trabajos realizados"]
        },
        "csecurity-clearance-description-lot": {
            "spa": [
                "Acreditación de cumplir con el “Esquema Nacional de Seguridad "
                "Nivel MEDIO o superior”"
            ]
        },
        "award-criterion-description-lot": {
            "spa": ["AO.1. Oferta económica (precio)"]
        },
    }

    def test_requirement_blocks_are_collected_per_field(self):
        n = normalize(self.RAW, known_fields=NOTICE_FIELDS)
        self.assertIn("selection-criterion-description-lot", n.requirements)
        self.assertIn("csecurity-clearance-description-lot", n.requirements)
        self.assertNotIn(A_NO_REQUIREMENT_TEXT, n.anomalies)

    def test_per_lot_boilerplate_is_deduplicated(self):
        # A seven-lot notice repeats the same clause seven times; paying seven
        # times the tokens for it is pure waste.
        raw = dict(
            self.RAW,
            **{
                "exclusion-grounds-description": {
                    "spa": ["Motivos de exclusión previstos en la LCSP"] * 7
                }
            },
        )
        n = normalize(raw, known_fields=NOTICE_FIELDS)
        self.assertEqual(len(n.requirements["exclusion-grounds-description"]), 1)

    def test_a_notice_with_no_requirement_text_is_flagged(self):
        n = normalize({"publication-number": "4-2026"}, known_fields=NOTICE_FIELDS)
        self.assertIn(A_NO_REQUIREMENT_TEXT, n.anomalies)
        self.assertEqual(n.requirements, {})

    def test_rendered_text_is_labelled_and_budget_bounded(self):
        n = normalize(self.RAW, known_fields=NOTICE_FIELDS)
        rendered = requirement_text(n.requirements)
        self.assertIn("CRITERIOS DE SELECCIÓN", rendered)
        self.assertIn("Esquema Nacional de Seguridad", rendered)
        self.assertLessEqual(len(requirement_text(n.requirements, max_chars=80)), 200)


class TestStubExtractor(unittest.TestCase):
    def setUp(self):
        self.stub = StubExtractor()

    def test_the_palma_pavement_contract_is_not_health_related(self):
        # The notice the ingest-time keyword filter gets wrong: "salud"
        # appears, the subject matter is roads.
        notice = NoticeText(
            publication_number="531238-2026",
            title="Contrato mixto de obras y servicios de reforma de pavimentos",
            description=(
                "La conservación, reparación, reforma y mejora de los pavimentos "
                "e infraestructuras municipales en viales, caminos y espacios "
                "libres públicos, incluida la Calle Salud."
            ),
        )
        result = self.stub.extract(notice)
        self.assertFalse(result.is_health_related)
        self.assertEqual(result.contract_nature, "works")

    def test_a_clinical_records_contract_is_health_related(self):
        notice = NoticeText(
            publication_number="737081-2026",
            title="Servicios de mantenimiento de aplicaciones de salud digital",
            description=(
                "Mantenimiento de las aplicaciones de historia clínica "
                "electrónica del Servicio Madrileño de Salud (3 LOTES)."
            ),
        )
        result = self.stub.extract(notice)
        self.assertTrue(result.is_health_related)
        self.assertEqual(result.work_category, "clinical_information_system")
        self.assertEqual(result.lot_count, 3)

    def test_certifications_are_normalised_from_spanish_prose(self):
        notice = NoticeText(
            publication_number="x",
            title="t",
            description="d",
            requirement_text=(
                "Acreditación de cumplir con el Esquema Nacional de Seguridad "
                "Nivel MEDIO o superior, así como certificado ISO 27001 vigente."
            ),
        )
        certs = self.stub.extract(notice).required_certifications
        self.assertIn("ENS Medio", certs)
        self.assertIn("ISO 27001", certs)

    def test_renewal_language_is_detected(self):
        notice = NoticeText(
            publication_number="x",
            title="t",
            description="d",
            requirement_text=(
                "Este contrato es susceptible de ser prorrogado por dos años."
            ),
        )
        self.assertTrue(self.stub.extract(notice).renewable)

    def test_price_weight_is_read_only_when_numeric(self):
        notice = NoticeText(
            publication_number="x",
            title="t",
            description="Oferta económica (precio): 60 %. Calidad técnica: 40 %.",
        )
        self.assertEqual(self.stub.extract(notice).price_weight_pct, 60.0)

        wordy = NoticeText(
            publication_number="y",
            title="t",
            description="La oferta económica tendrá un peso mayoritario.",
        )
        self.assertIsNone(self.stub.extract(wordy).price_weight_pct)

    def test_the_stub_never_returns_a_value_outside_its_enums(self):
        notice = NoticeText(publication_number="z", title="", description="")
        result = self.stub.extract(notice)
        self.assertIn(result.cpv_division, CPV_DIVISIONS)
        self.assertIn(result.work_category, WORK_CATEGORIES)


class TestNormaliseCert(unittest.TestCase):
    def test_the_many_spellings_of_ens_collapse(self):
        for raw in (
            "Esquema Nacional de Seguridad Nivel MEDIO",
            "ENS MEDIO",
            "ENS-Medio",
        ):
            self.assertEqual(_normalise_cert(raw), "ENS Medio")

    def test_iso_numbers_are_kept_and_spacing_normalised(self):
        self.assertEqual(_normalise_cert("ISO27001"), "ISO 27001")
        self.assertEqual(_normalise_cert("iso 9001"), "ISO 9001")


class TestPayloadCoercion(unittest.TestCase):
    def test_a_value_outside_the_enum_falls_back_rather_than_crashing(self):
        result = _from_payload(
            {"cpv_division": "99", "work_category": "nonsense", "lot_count": "3"}
        )
        self.assertEqual(result.cpv_division, "other")
        self.assertEqual(result.work_category, "other")
        self.assertEqual(result.lot_count, 3)

    def test_a_missing_payload_yields_safe_defaults(self):
        result = _from_payload({})
        self.assertEqual(result.lot_count, 1)
        self.assertEqual(result.required_certifications, [])
        self.assertIsNone(result.price_weight_pct)

    def test_a_scalar_certification_is_accepted_as_a_one_item_list(self):
        result = _from_payload({"required_certifications": "ISO 27001"})
        self.assertEqual(result.required_certifications, ["ISO 27001"])

    def test_schema_requires_every_property_and_forbids_extras(self):
        # Both are preconditions for the API's strict JSON-schema mode.
        self.assertFalse(EXTRACTION_SCHEMA["additionalProperties"])
        self.assertEqual(
            set(EXTRACTION_SCHEMA["required"]),
            set(EXTRACTION_SCHEMA["properties"]),
        )


class TestCost(unittest.TestCase):
    def test_cache_reads_are_charged_at_a_tenth_of_input(self):
        full = estimate_cost_usd(
            "claude-opus-5", input_tokens=1_000_000, output_tokens=0
        )
        cached = estimate_cost_usd(
            "claude-opus-5",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
        )
        self.assertAlmostEqual(full, 5.00, places=4)
        self.assertAlmostEqual(cached, 0.50, places=4)

    def test_an_unknown_model_reports_zero_rather_than_a_wrong_number(self):
        self.assertEqual(
            estimate_cost_usd("stub", input_tokens=10_000, output_tokens=10_000), 0.0
        )


class TestScoring(unittest.TestCase):
    """The scorer must distinguish abstention from being wrong."""

    def _case(self, truth: dict, labels: dict | None = None) -> Case:
        return Case(
            case_id="c",
            case_version=1,
            publication_number="1-2026",
            stratum="t",
            input={"title": "t", "description": "d"},
            truth=truth,
            labels=labels or {},
        )

    def test_a_correct_value_counts_as_recall_not_as_wrong(self):
        cases = [self._case({"cpv_division": "72"})]
        got = score(cases, [Extraction(cpv_division="72")])["cpv_division"]
        self.assertEqual(got.correct, 1)
        self.assertEqual(got.recall, 1.0)
        self.assertEqual(got.wrong_value_rate, 0.0)

    def test_a_wrong_value_is_counted_separately_from_an_abstention(self):
        cases = [self._case({"cpv_division": "72"}), self._case({"cpv_division": "48"})]
        got = score(
            cases,
            [Extraction(cpv_division="33"), Extraction(cpv_division="other")],
        )["cpv_division"]
        self.assertEqual(got.wrong_value, 1)
        self.assertEqual(got.missed, 1)
        # One prediction was made and it was wrong.
        self.assertEqual(got.wrong_value_rate, 1.0)
        self.assertEqual(got.abstention_rate, 0.5)

    def test_abstaining_everywhere_scores_no_wrong_values_and_zero_recall(self):
        # The metric that makes abstention-gaming visible: a model that says
        # nothing has a perfect wrong_value_rate and is useless.
        cases = [self._case({"cpv_division": "72"}) for _ in range(5)]
        got = score(cases, [Extraction(cpv_division="other") for _ in range(5)])[
            "cpv_division"
        ]
        self.assertIsNone(got.wrong_value_rate)
        self.assertEqual(got.recall, 0.0)
        self.assertEqual(got.abstention_rate, 1.0)

    def test_an_unlabelled_field_is_excluded_rather_than_scored_as_wrong(self):
        cases = [self._case({}, labels={"is_health_related": None})]
        got = score(cases, [Extraction(is_health_related=True)])["is_health_related"]
        self.assertEqual(got.unlabelled, 1)
        self.assertEqual(got.correct, 0)
        self.assertIsNone(got.recall)

    def test_a_labelled_field_is_scored(self):
        cases = [self._case({}, labels={"is_health_related": False})]
        got = score(cases, [Extraction(is_health_related=False)])["is_health_related"]
        self.assertEqual(got.unlabelled, 0)
        self.assertEqual(got.correct, 1)

    def test_certifications_compare_as_sets_not_as_ordered_lists(self):
        cases = [
            self._case(
                {}, labels={"required_certifications": ["ISO 27001", "ENS Medio"]}
            )
        ]
        got = score(
            cases,
            [Extraction(required_certifications=["ens  medio", "ISO-27001"])],
        )["required_certifications"]
        self.assertEqual(got.correct, 1)

    def test_a_failed_extraction_is_a_miss_not_a_wrong_value(self):
        cases = [self._case({"cpv_division": "72"})]
        got = score(cases, [Extraction(ok=False, error="boom")])["cpv_division"]
        self.assertEqual(got.missed, 1)
        self.assertEqual(got.wrong_value, 0)


class TestExtractionPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_configurations_coexist_for_the_same_notice(self):
        a = Extraction(
            extractor="claude", model="claude-opus-5", effort="low", cpv_division="72"
        )
        b = Extraction(
            extractor="claude", model="claude-opus-5", effort="high", cpv_division="48"
        )
        with Store(self.db) as store:
            store.save_extraction("1-2026", a)
            store.save_extraction("1-2026", b)
            store.commit()
            rows = list(
                store.conn.execute(
                    "SELECT effort, cpv_division FROM extractions ORDER BY effort"
                )
            )
        self.assertEqual(
            [(r["effort"], r["cpv_division"]) for r in rows],
            [("high", "48"), ("low", "72")],
        )

    def test_re_extracting_the_same_configuration_overwrites(self):
        with Store(self.db) as store:
            store.save_extraction(
                "1-2026", Extraction(extractor="stub", model="stub", effort="n/a")
            )
            store.save_extraction(
                "1-2026",
                Extraction(
                    extractor="stub", model="stub", effort="n/a", cpv_division="48"
                ),
            )
            store.commit()
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0], 1
            )

    def test_pending_work_excludes_notices_already_extracted_ok(self):
        from procsignal.extract import PROMPT_VERSION

        raw = {
            "publication-number": "1-2026",
            "title-proc": {"spa": "Mantenimiento de aplicaciones de salud"},
            "description-proc": {"spa": "Historia clínica electrónica"},
            "classification-cpv": ["72000000"],
        }
        with Store(self.db) as store:
            notice = normalize(
                raw,
                known_fields=NOTICE_FIELDS,
                relevance_keywords=("salud",),
            )
            store.upsert(notice, raw, profile="t")
            store.commit()

            kwargs = dict(
                extractor="stub",
                model="stub",
                effort="n/a",
                prompt_version=PROMPT_VERSION,
            )
            self.assertEqual(len(store.notices_needing_extraction(**kwargs)), 1)

            store.save_extraction(
                "1-2026",
                Extraction(extractor="stub", model="stub", effort="n/a", ok=True),
            )
            store.commit()
            self.assertEqual(len(store.notices_needing_extraction(**kwargs)), 0)

    def test_a_failed_extraction_stays_pending_for_retry(self):
        from procsignal.extract import PROMPT_VERSION

        raw = {
            "publication-number": "2-2026",
            "title-proc": {"spa": "Hospital"},
            "description-proc": {"spa": "Sistema hospitalario"},
        }
        with Store(self.db) as store:
            store.upsert(
                normalize(raw, known_fields=NOTICE_FIELDS, relevance_keywords=("hospital",)),
                raw,
                profile="t",
            )
            store.save_extraction(
                "2-2026",
                Extraction(
                    extractor="stub", model="stub", effort="n/a", ok=False, error="x"
                ),
            )
            store.commit()
            pending = store.notices_needing_extraction(
                extractor="stub",
                model="stub",
                effort="n/a",
                prompt_version=PROMPT_VERSION,
            )
        self.assertEqual(len(pending), 1)


class TestBuildExtractor(unittest.TestCase):
    def test_stub_is_buildable_without_any_credential(self):
        self.assertEqual(build_extractor("stub").name, "stub")

    def test_an_unknown_name_fails_loudly(self):
        with self.assertRaises(ValueError):
            build_extractor("gpt")


if __name__ == "__main__":
    unittest.main()
