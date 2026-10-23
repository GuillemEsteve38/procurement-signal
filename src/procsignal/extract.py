"""Extract structured judgements from a notice's Spanish requirement text.

What this extracts, and what it deliberately does not, was decided by
measurement rather than by guessing — see docs/EVAL.md. The short version:

* ``description-proc`` is a ~190-character subject line. A budget figure
  appears in 2 of 50 sampled notices and a deadline in 0 of 50, so budget and
  deadline are **not extractable** from notice text. They are also already
  published as structured TED fields, which makes extracting them pointless
  twice over. The extractor does not attempt them.
* Contract duration is likewise structured (``duration-period-value-lot``,
  87% populated on open contract notices), so it is not extracted either.
* What *is* only available as prose: who is allowed to bid, what
  certifications they must hold, how the bid will be scored, and whether the
  contract is genuinely health-related. Those are what this extracts.

Two implementations:

* :class:`ClaudeExtractor` — the real one, via the Claude API with a
  JSON-schema-constrained response.
* :class:`StubExtractor` — deterministic regex/heuristic. Not a mock: it runs
  the whole pipeline with no API key and no cost, and it is a genuine accuracy
  baseline. A model that cannot beat regexes on a field is not earning its
  cost on that field.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

LOG = logging.getLogger(__name__)

#: Bump when the prompt or schema changes in a way that could move results.
#: Stored on every extraction and every eval run, so an old number is never
#: silently compared against a new question.
PROMPT_VERSION = "v2"

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "low"

CONTRACT_NATURES = ("services", "supplies", "works", "unknown")

#: CPV divisions this client's market actually spans, plus the adjacent ones
#: that show up in health procurement. "other" keeps the enum closed.
CPV_DIVISIONS = ("48", "72", "79", "85", "33", "32", "51", "50", "other")

#: What kind of work it is. Not published by TED in any form, and the single
#: most useful field for a vendor deciding whether to read the full dossier.
WORK_CATEGORIES = (
    "application_maintenance",
    "software_development",
    "software_licences",
    "infrastructure_hosting",
    "consulting_advisory",
    "clinical_information_system",
    "medical_imaging",
    "data_analytics",
    "support_helpdesk",
    "other",
)

#: Hand-written rather than generated from a dataclass, because the API's
#: JSON-schema support is a documented subset: `anyOf`, `enum` and
#: `additionalProperties: false` are supported; numeric and string constraints
#: such as `minimum` or `maxLength` are not.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cpv_division": {
            "type": "string",
            "enum": list(CPV_DIVISIONS),
            "description": (
                "Two-digit CPV division that best fits the subject matter. "
                "48 = software packages and information systems, "
                "72 = IT services (development, maintenance, consulting), "
                "79 = business and management services, "
                "85 = health and social work services, "
                "33 = medical equipment and pharmaceuticals, "
                "32 = radio/TV/telecom equipment, "
                "51 = installation services, 50 = repair and maintenance of "
                "equipment. 'other' if none fit."
            ),
        },
        "contract_nature": {
            "type": "string",
            "enum": list(CONTRACT_NATURES),
            "description": (
                "'services' for service provision, 'supplies' for goods or "
                "licences, 'works' for construction. 'unknown' if unclear."
            ),
        },
        "lot_count": {
            "type": "integer",
            "description": (
                "Number of lots the contract is divided into; 1 if undivided."
            ),
        },
        "is_health_related": {
            "type": "boolean",
            "description": (
                "True only if the SUBJECT MATTER of the contract is health or "
                "clinical: hospital or clinical systems, patient records, "
                "pharmacy, medical imaging or equipment, patient-facing "
                "services, public-health systems. A contract is NOT "
                "health-related merely because a health authority is the "
                "buyer, or because a word like 'salud' appears in a street "
                "name, a department name, or an unrelated list. Road "
                "maintenance bought by a city that happens to mention "
                "'Calle Salud' is false."
            ),
        },
        "work_category": {
            "type": "string",
            "enum": list(WORK_CATEGORIES),
            "description": (
                "The dominant kind of work. Pick the single best fit: "
                "application_maintenance for maintaining existing software; "
                "software_development for building new; software_licences for "
                "buying or reselling licences; infrastructure_hosting for "
                "cloud, datacentre or network; consulting_advisory for "
                "studies and advice; clinical_information_system for EHR, "
                "HIS or clinical records; medical_imaging for PACS/RIS or "
                "imaging equipment; data_analytics for BI, data platforms or "
                "process mining; support_helpdesk for user support."
            ),
        },
        "required_certifications": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Certifications or standards the text explicitly requires of "
                "the supplier, normalised to their common name, e.g. "
                "'ISO 27001', 'ISO 9001', 'ENS Medio', 'ENS Alto', 'HL7 "
                "FHIR', 'CMMI'. Only include a certification the text "
                "actually demands. Empty array if none. Do not infer one "
                "from the subject matter."
            ),
        },
        "renewable": {
            "type": "boolean",
            "description": (
                "True if the text says the contract may be extended, renewed "
                "or prorrogado beyond its initial term."
            ),
        },
        "price_weight_pct": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": (
                "Percentage weight given to price in the award criteria, if "
                "the text states it as a number. Null if not stated."
            ),
        },
        "summary_es": {
            "type": "string",
            "description": (
                "One sentence in Spanish, maximum 25 words, saying what is "
                "being bought. No preamble."
            ),
        },
    },
    "required": [
        "cpv_division",
        "contract_nature",
        "lot_count",
        "is_health_related",
        "work_category",
        "required_certifications",
        "renewable",
        "price_weight_pct",
        "summary_es",
    ],
}

SYSTEM_PROMPT = """\
You read Spanish public procurement notices from TED, the EU procurement \
journal, and extract structured judgements from their text.

What you are given is notice metadata, not the full tender dossier. The \
subject description is often a single short line, and the detailed \
requirements live in a separate document ("pliego de prescripciones técnicas \
particulares", PPTP) that you cannot see. Several notices will say so \
explicitly. Work with what is present and do not infer the contents of \
documents you were not given.

Rules:

- Extract only what the text supports. For every nullable field, "not stated" \
is a correct and useful answer; an invented value is actively harmful, \
because it sends a bidder to the wrong contract.
- A contract is health-related only if its subject matter is health or \
clinical. Spanish notices mention health bodies, street names and department \
names incidentally. The buyer being a health authority is not sufficient on \
its own, and neither is the word "salud" appearing somewhere in a list.
- For required certifications, report only what the supplier is required to \
hold. "Esquema Nacional de Seguridad Nivel MEDIO" normalises to "ENS Medio". \
Do not list a standard just because the work relates to it.
- Percentages written as "60 %" or "60 puntos sobre 100" are 60. If price \
weight is expressed only in words, return null.
- Do not translate. Return the summary in Spanish.
"""


@dataclass
class Extraction:
    """What the extractor produces, plus how much it cost to produce."""

    cpv_division: str = "other"
    contract_nature: str = "unknown"
    lot_count: int = 1
    is_health_related: bool = False
    work_category: str = "other"
    required_certifications: list[str] = field(default_factory=list)
    renewable: bool = False
    price_weight_pct: float | None = None
    summary_es: str = ""

    # instrumentation — never scored, always reported
    extractor: str = ""
    model: str = ""
    effort: str = ""
    prompt_version: str = PROMPT_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    ok: bool = True
    error: str | None = None

    def scored_fields(self) -> dict[str, Any]:
        return {
            "cpv_division": self.cpv_division,
            "contract_nature": self.contract_nature,
            "lot_count": self.lot_count,
            "is_health_related": self.is_health_related,
            "work_category": self.work_category,
            "required_certifications": self.required_certifications,
            "renewable": self.renewable,
        }


@dataclass(frozen=True)
class NoticeText:
    """The only thing an extractor is allowed to see.

    Everything TED publishes as a structured field is withheld, so the eval
    can score the model's reading against independent ground truth rather than
    against labels written by whoever wrote the prompt.
    """

    publication_number: str
    title: str
    description: str
    lot_titles: list[str] = field(default_factory=list)
    requirement_text: str = ""

    def as_prompt(self, *, max_chars: int = 14_000) -> str:
        parts = [f"TÍTULO: {self.title or '(ninguno)'}"]
        if self.lot_titles:
            lots = "\n".join(f"  - {t}" for t in self.lot_titles)
            parts.append(f"LOTES:\n{lots}")
        body = self.description or "(ninguna)"
        parts.append(f"DESCRIPCIÓN:\n{body}")
        if self.requirement_text:
            parts.append(self.requirement_text)
        prompt = "\n\n".join(parts)
        if len(prompt) > max_chars:
            prompt = prompt[:max_chars] + "\n[...texto truncado...]"
        return prompt


class Extractor(Protocol):
    name: str

    def extract(self, notice: NoticeText) -> Extraction: ...


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

#: USD per million tokens. Cache reads are ~0.1x input, writes ~1.25x (5m TTL).
#: Kept local and explicit so the eval can report a cost without a network
#: call, and so a stale price shows up in a diff rather than hiding.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    rates = PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        return 0.0
    rate_in, rate_out = rates
    return (
        input_tokens * rate_in
        + cache_write_tokens * rate_in * 1.25
        + cache_read_tokens * rate_in * 0.10
        + output_tokens * rate_out
    ) / 1_000_000


# ---------------------------------------------------------------------------
# the real extractor
# ---------------------------------------------------------------------------


class ClaudeExtractor:
    """Extract via the Claude API with a JSON-schema-constrained response."""

    name = "claude"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = 4096,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "the claude extractor needs the anthropic SDK: "
                    "pip install -e '.[extract]'"
                ) from exc
            # A bare constructor also resolves an `ant auth login` profile, so
            # do not demand ANTHROPIC_API_KEY when none was passed.
            client = (
                anthropic.Anthropic(api_key=api_key)
                if api_key
                else anthropic.Anthropic()
            )
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def extract(self, notice: NoticeText) -> Extraction:
        started = time.perf_counter()
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # The system prompt and schema are byte-identical for every
                # notice, so they are the cacheable prefix; the notice text
                # varies and goes after it.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
                    "effort": self.effort,
                },
                messages=[{"role": "user", "content": notice.as_prompt()}],
            )
        except Exception as exc:  # noqa: BLE001 - recorded per case, never fatal
            # One bad notice must not kill a 50-case eval run.
            return Extraction(
                extractor=self.name,
                model=self.model,
                effort=self.effort,
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        extraction = self._parse(response)
        extraction.extractor = self.name
        extraction.model = getattr(response, "model", self.model) or self.model
        extraction.effort = self.effort
        extraction.latency_ms = latency_ms

        usage = getattr(response, "usage", None)
        if usage is not None:
            extraction.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            extraction.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            extraction.cache_read_tokens = int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            )
            extraction.cache_write_tokens = int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
            extraction.cost_usd = estimate_cost_usd(
                extraction.model,
                input_tokens=extraction.input_tokens,
                output_tokens=extraction.output_tokens,
                cache_read_tokens=extraction.cache_read_tokens,
                cache_write_tokens=extraction.cache_write_tokens,
            )
        return extraction

    def _parse(self, response: Any) -> Extraction:
        # A refusal is a successful HTTP response with no usable content, so it
        # must be checked before reading content blocks.
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return Extraction(ok=False, error=f"refusal (category={category})")
        if stop == "max_tokens":
            return Extraction(
                ok=False, error="max_tokens reached before the JSON object closed"
            )

        text = next(
            (
                block.text
                for block in getattr(response, "content", [])
                if getattr(block, "type", None) == "text"
            ),
            None,
        )
        if not text:
            return Extraction(ok=False, error="no text block in response")

        try:
            data = json.loads(text)
        except ValueError as exc:
            return Extraction(ok=False, error=f"invalid JSON: {exc}")

        return _from_payload(data)


def _from_payload(data: dict[str, Any]) -> Extraction:
    """Build an Extraction from parsed JSON, coercing defensively.

    The schema constrains the response, but parsing must not crash on a
    surprise: a crash loses the whole run, a coercion loses one field.
    """
    return Extraction(
        cpv_division=_enum(data.get("cpv_division"), CPV_DIVISIONS, "other"),
        contract_nature=_enum(
            data.get("contract_nature"), CONTRACT_NATURES, "unknown"
        ),
        lot_count=_as_int(data.get("lot_count")) or 1,
        is_health_related=bool(data.get("is_health_related")),
        work_category=_enum(data.get("work_category"), WORK_CATEGORIES, "other"),
        required_certifications=_as_str_list(data.get("required_certifications")),
        renewable=bool(data.get("renewable")),
        price_weight_pct=_as_float(data.get("price_weight_pct")),
        summary_es=str(data.get("summary_es") or "").strip(),
    )


def _enum(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def _as_str_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else ([value] if value else [])
    return [str(v).strip() for v in items if str(v).strip()]


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# the deterministic baseline
# ---------------------------------------------------------------------------

_LOTS_RE = re.compile(r"\b(?P<n>\d{1,2})\s+lotes?\b", re.IGNORECASE)
_PCT_RE = re.compile(
    r"(?:precio|econ[oó]mic\w*)[^.%]{0,60}?(?P<n>\d{1,3})\s*(?:%|puntos)",
    re.IGNORECASE,
)
_CERT_RE = re.compile(
    r"\b(ISO[\s/-]?\d{4,5}(?:[-:]\d+)?|"
    r"(?:Esquema Nacional de Seguridad|ENS)[^.,;]{0,30}?(?:Alto|Medio|B[aá]sico)|"
    r"HL7(?:\s+FHIR)?|CMMI|ITIL|SOC\s?2|UNE[\s-]?EN[\s-]?\d+)\b",
    re.IGNORECASE,
)
_RENEW_RE = re.compile(
    r"\b(prorrogab\w+|prorrogad\w+|pr[oó]rroga|renovaci[oó]n|renovab\w+|"
    r"susceptible de ser prorrogado)\b",
    re.IGNORECASE,
)

#: Subject-matter terms, deliberately narrower than the ingest-time keyword
#: list — that list is what produces the false positives this field measures.
_HEALTH_SUBJECT_RE = re.compile(
    r"\b(historia(?:s)? cl[ií]nica|hospital(?:ario|aria)?|asistencia sanitaria|"
    r"servicio(?:s)? sanitario|atenci[oó]n primaria|atenci[oó]n especializada|"
    r"equipamiento m[eé]dico|farmac[eé]utic|receta electr[oó]nica|"
    r"paciente|quir[uú]rgic|radiolog|laboratorio cl[ií]nico|"
    r"salud p[uú]blica|salud digital|sistema(?:s)? de informaci[oó]n sanitari)\b",
    re.IGNORECASE,
)
_WORKS_RE = re.compile(
    r"\b(obras?|construcci[oó]n|pavimento|urbanizaci[oó]n|edificaci[oó]n)\b",
    re.IGNORECASE,
)
_SUPPLIES_RE = re.compile(
    r"\b(suministro|adquisici[oó]n|licencias?|equipamiento|compra)\b", re.IGNORECASE
)
_SERVICES_RE = re.compile(
    r"\b(servicios?|mantenimiento|soporte|consultor[ií]a|desarrollo)\b", re.IGNORECASE
)

#: (category, pattern) in priority order — first match wins.
_CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("medical_imaging", re.compile(r"\b(pacs|ris|radiolog|imagen m[eé]dica|imaxe)\b", re.I)),
    (
        "clinical_information_system",
        re.compile(r"\b(historia cl[ií]nica|hce|his\b|receta electr[oó]nica)\b", re.I),
    ),
    ("support_helpdesk", re.compile(r"\b(help[\s-]?desk|cau\b|soporte a usuarios|atenci[oó]n a usuarios)\b", re.I)),
    ("software_licences", re.compile(r"\b(licencias?|suscripci[oó]n|renovaci[oó]n de licencias)\b", re.I)),
    ("infrastructure_hosting", re.compile(r"\b(hosting|alojamiento|cloud|nube|datacenter|centro de datos|red(?:es)? corporativ)\b", re.I)),
    ("data_analytics", re.compile(r"\b(business intelligence|anal[ií]tica|process mining|big data|cuadro de mando)\b", re.I)),
    ("application_maintenance", re.compile(r"\b(mantenimiento (?:de )?(?:las )?aplicaciones?|evolutivo|soporte y mantenimiento)\b", re.I)),
    ("software_development", re.compile(r"\b(desarrollo (?:de )?software|implantaci[oó]n|nuevos desarrollos)\b", re.I)),
    ("consulting_advisory", re.compile(r"\b(consultor[ií]a|asistencia t[eé]cnica|estudio)\b", re.I)),
)

#: Weak keyword -> CPV division. Crude on purpose; it is the floor the model
#: has to clear.
_CPV_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("33", re.compile(r"\b(equipamiento m[eé]dico|sanitario fungible|f[aá]rmac|pr[oó]tesis|imaxe de alta tecnolog)\b", re.I)),
    ("32", re.compile(r"\b(telecomunicaci|radiofrecuencia|antena|telefon[ií]a)\b", re.I)),
    ("48", re.compile(r"\b(licencias?|software|soluci[oó]n inform[aá]tica|aplicaci[oó]n inform)\b", re.I)),
    ("85", re.compile(r"\b(servicios sanitarios|asistencia sanitaria|atenci[oó]n primaria|servicios sociales)\b", re.I)),
    ("79", re.compile(r"\b(consultor[ií]a de gesti[oó]n|auditor[ií]a|asesoramiento jur)\b", re.I)),
    ("50", re.compile(r"\b(reparaci[oó]n|mantenimiento de equipos)\b", re.I)),
    ("51", re.compile(r"\b(instalaci[oó]n)\b", re.I)),
)


class StubExtractor:
    """Regex and heuristic baseline. No API key, no cost, no network.

    Its purpose is to make the model's numbers mean something: "82% on CPV
    division" only reads as good next to what a weekend of regexes achieves.
    """

    name = "stub"

    def __init__(self, *, model: str = "stub", effort: str = "n/a") -> None:
        self.model = model
        self.effort = effort

    def extract(self, notice: NoticeText) -> Extraction:
        started = time.perf_counter()
        text = "\n".join(
            [notice.title, *notice.lot_titles, notice.description, notice.requirement_text]
        )

        return Extraction(
            cpv_division=self._cpv(text),
            contract_nature=self._nature(text),
            lot_count=self._lots(text, notice),
            is_health_related=bool(_HEALTH_SUBJECT_RE.search(text)),
            work_category=self._category(text),
            required_certifications=self._certs(text),
            renewable=bool(_RENEW_RE.search(text)),
            price_weight_pct=self._price_weight(text),
            summary_es=(notice.description or notice.title or "")[:160].strip(),
            extractor=self.name,
            model=self.model,
            effort=self.effort,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _cpv(text: str) -> str:
        for division, pattern in _CPV_RULES:
            if pattern.search(text):
                return division
        return "72"  # the market's modal division; a baseline needs a default

    @staticmethod
    def _category(text: str) -> str:
        for category, pattern in _CATEGORY_RULES:
            if pattern.search(text):
                return category
        return "other"

    @staticmethod
    def _lots(text: str, notice: NoticeText) -> int:
        match = _LOTS_RE.search(text)
        if match:
            return max(1, int(match.group("n")))
        return max(1, len(notice.lot_titles))

    @staticmethod
    def _nature(text: str) -> str:
        # Order matters: a works contract usually also says "servicios".
        if _WORKS_RE.search(text):
            return "works"
        if _SUPPLIES_RE.search(text):
            return "supplies"
        if _SERVICES_RE.search(text):
            return "services"
        return "unknown"

    @staticmethod
    def _certs(text: str) -> list[str]:
        seen: dict[str, None] = {}
        for match in _CERT_RE.finditer(text):
            raw = " ".join(match.group(0).split())
            seen.setdefault(_normalise_cert(raw), None)
        return list(seen)

    @staticmethod
    def _price_weight(text: str) -> float | None:
        match = _PCT_RE.search(text)
        if not match:
            return None
        value = float(match.group("n"))
        return value if 0 <= value <= 100 else None


def _normalise_cert(raw: str) -> str:
    """Collapse the many ways a Spanish notice names the same certification."""
    text = raw.upper().replace("-", " ")
    if "ESQUEMA NACIONAL DE SEGURIDAD" in text or text.startswith("ENS"):
        for level, label in (("ALTO", "ENS Alto"), ("MEDIO", "ENS Medio")):
            if level in text:
                return label
        if "BAS" in text:
            return "ENS Basico"
        return "ENS"
    match = re.search(r"ISO\s*(\d{4,5})", text)
    if match:
        return f"ISO {match.group(1)}"
    return " ".join(text.split())


def build_extractor(
    name: str,
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> Extractor:
    if name == "stub":
        return StubExtractor()
    if name == "claude":
        return ClaudeExtractor(model=model, effort=effort)
    raise ValueError(f"unknown extractor {name!r} (expected 'claude' or 'stub')")


def default_extractor_name() -> str:
    """Use Claude when a credential is reachable, otherwise the stub.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials — the
    SDK also resolves an `ant auth login` profile — so check for that too.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "claude"
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR")
    candidates = [config_dir] if config_dir else []
    candidates.append(os.path.expanduser("~/.config/anthropic"))
    if any(c and os.path.isdir(c) for c in candidates):
        return "claude"
    return "stub"
