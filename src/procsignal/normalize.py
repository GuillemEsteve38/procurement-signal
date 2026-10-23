"""Turn a raw TED notice into a row you can actually query.

This module is the whole point of the project. The TED payload is shaped for
publication, not for analysis:

* every text field is a map of up to 24 language codes to either a string or a
  list of strings (one per lot);
* array fields repeat values meaninglessly — ``place-of-performance`` may be
  ``["ES300","ESP","ES300","ES300","ESP"]`` for a three-lot contract;
* monetary amounts and their currencies live in *different* fields, the amount
  a bare float and the currency a list;
* ``publication-date`` is a calendar date carrying a UTC offset
  (``"2026-10-23+02:00"``), which no date parser expects;
* the same buyer carries several identifiers under different national schemes.

Two rules govern everything here:

1. **Never drop a notice because a field surprised us.** Anything unexpected is
   recorded as an anomaly on the row, and the raw payload is kept verbatim, so
   a mis-mapping is re-derivable without re-fetching anything.
2. **Never guess silently.** Where a value is derived (a contract value summed
   across lots, a language chosen from several) the provenance is stored next
   to it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: Language preference for collapsing TED's per-language maps. Spanish first
#: because the client reads Spanish and TED's Spanish text is the original for
#: Spanish notices; the rest are translations. ``mul`` is TED's marker for a
#: value that is not language-specific (city names, mostly).
DEFAULT_LANGS: tuple[str, ...] = ("spa", "eng", "mul")

#: TED writes this in place-of-performance when a contract has no fixed
#: location. It is not a country code and must not be stored as one.
ANYWHERE = "anyw"

_COUNTRY_RE = re.compile(r"^[A-Z]{3}$")
_NUTS_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{0,3}$")
_DATE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[T ](?P<time>\d{2}:\d{2}(?::\d{2})?))?"
    r"(?P<offset>Z|[+-]\d{2}:?\d{2})?$"
)

# Anomaly codes. Stable strings — they end up in the database and in the UI.
A_NO_TITLE = "no_title"
A_NO_DESCRIPTION = "no_description"
A_NO_PUBLICATION_DATE = "no_publication_date"
A_BAD_DATE = "bad_date"
A_NO_VALUE = "no_value"
A_MULTI_CURRENCY = "multi_currency"
A_VALUE_FROM_LOT_SUM = "value_from_lot_sum"
A_DEADLINE_BEFORE_PUBLICATION = "deadline_before_publication"
A_NO_CPV = "no_cpv"
A_NO_BUYER_NAME = "no_buyer_name"
A_MULTI_BUYER_ID_SCHEME = "multi_buyer_id_scheme"
A_LOT_CARDINALITY_MISMATCH = "lot_cardinality_mismatch"
A_UNKNOWN_FIELDS = "unknown_fields"
A_NO_REQUIREMENT_TEXT = "no_requirement_text"
A_UNKNOWN_DURATION_UNIT = "unknown_duration_unit"

#: TED duration unit codes -> months. Converting to a single unit makes
#: durations comparable; the raw value+unit is kept in ``duration_raw`` so a
#: wrong conversion is visible rather than baked in.
DURATION_UNIT_MONTHS: dict[str, float] = {
    "MONTH": 1.0,
    "YEAR": 12.0,
    "WEEK": 7.0 / 30.0,
    "DAY": 1.0 / 30.0,
    "HOUR": 1.0 / (30.0 * 24.0),
}

#: The requirement-text fields, in the order they are most useful to a reader.
REQUIREMENT_FIELDS: tuple[str, ...] = (
    "selection-criterion-description-lot",
    "csecurity-clearance-description-lot",
    "award-criterion-description-lot",
    "award-criterion-name-lot",
    "exclusion-grounds-description",
    "guarantee-required-description-lot",
    "renewal-description-lot",
    "option-description-lot",
)


@dataclass
class NormalizedNotice:
    """One queryable row. Mirrors the ``notices`` table in store.py."""

    publication_number: str
    notice_identifier: str | None = None
    publication_date: str | None = None
    publication_offset: str | None = None
    notice_type: str | None = None
    notice_subtype: str | None = None
    form_type: str | None = None

    title: str | None = None
    title_lang: str | None = None
    description: str | None = None
    description_lang: str | None = None

    buyer_name: str | None = None
    buyer_city: str | None = None
    buyer_country: str | None = None
    buyer_identifiers: list[str] = field(default_factory=list)
    buyer_legal_type: str | None = None
    main_activity: str | None = None

    main_cpv: str | None = None
    cpv_codes: list[str] = field(default_factory=list)
    cpv_divisions: list[str] = field(default_factory=list)

    lot_count: int = 0
    lot_titles: list[str] = field(default_factory=list)
    lot_descriptions: list[str] = field(default_factory=list)

    value_amount: float | None = None
    value_currency: str | None = None
    value_source: str | None = None

    deadline: str | None = None
    nuts_codes: list[str] = field(default_factory=list)
    performance_countries: list[str] = field(default_factory=list)
    performance_anywhere: bool = False

    procedure_type: str | None = None
    contract_nature: str | None = None
    winner_names: list[str] = field(default_factory=list)

    duration_months: int | None = None
    duration_raw: str | None = None
    #: Free-text requirement blocks, keyed by their TED field. This is the
    #: extractor's actual input — ``description`` alone is a ~190-character
    #: subject line and cannot support requirement extraction.
    requirements: dict[str, list[str]] = field(default_factory=dict)

    notice_url: str | None = None
    contract_url: str | None = None

    is_relevant: bool = False
    matched_keywords: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    def flag(self, code: str) -> None:
        if code not in self.anomalies:
            self.anomalies.append(code)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def as_list(value: Any) -> list[Any]:
    """Coerce TED's scalar-or-list-or-missing to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def unique(values: Iterable[Any]) -> list[Any]:
    """Deduplicate while preserving first-seen order.

    TED repeats array values once per lot, so ``["ES300","ESP","ES300"]`` is
    two distinct places, not three. Order is preserved because the first value
    is usually the primary one.
    """
    seen: set[Any] = set()
    out: list[Any] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def pick_language(
    value: Any, langs: Sequence[str] = DEFAULT_LANGS
) -> tuple[str | None, str | None]:
    """Collapse a TED language map to a single string plus its language code.

    Accepts anything TED actually sends for a text field: a plain string, a
    ``{lang: str}`` map, or a ``{lang: [str, ...]}`` map (one entry per lot).
    Returns ``(text, lang)``; ``lang`` is ``None`` when the field was not
    language-tagged at all.
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        return value.strip() or None, None
    if isinstance(value, list):
        texts = [str(v).strip() for v in value if v]
        return (texts[0] if texts else None), None
    if not isinstance(value, dict):
        return str(value), None

    order = list(langs) + [k for k in value if k not in langs]
    for lang in order:
        if lang not in value:
            continue
        raw = value[lang]
        if isinstance(raw, list):
            texts = [str(v).strip() for v in raw if v]
            if texts:
                return texts[0], lang
        elif raw:
            text = str(raw).strip()
            if text:
                return text, lang
    return None, None


def language_items(
    value: Any, langs: Sequence[str] = DEFAULT_LANGS
) -> tuple[list[str], str | None]:
    """Like :func:`pick_language`, but keeps the whole per-lot list."""
    if value is None:
        return [], None
    if isinstance(value, str):
        return ([value.strip()] if value.strip() else []), None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()], None
    if not isinstance(value, dict):
        return [str(value)], None

    order = list(langs) + [k for k in value if k not in langs]
    for lang in order:
        if lang not in value:
            continue
        items = [str(v).strip() for v in as_list(value[lang]) if str(v).strip()]
        if items:
            return items, lang
    return [], None


def parse_ted_date(value: Any) -> tuple[str | None, str | None, bool]:
    """Parse a TED date into ``(iso_date, utc_offset, ok)``.

    TED emits several shapes from the same conceptual field:

    * ``"2026-10-23+02:00"`` — a calendar date with a UTC offset. Python's
      ``date.fromisoformat`` rejects this and ``datetime.fromisoformat``
      accepts it only by accident of version; both are a trap.
    * ``"2016-11-04T17:00:00+01:00"`` — a full timestamp (deadlines).
    * ``"2026-10-23"`` — a bare date.

    The offset is preserved separately rather than discarded, because a
    deadline is only meaningful with its timezone and we do not want to
    normalize to UTC and silently shift a submission deadline across midnight.
    """
    if value is None:
        return None, None, True
    if isinstance(value, (list, tuple)):
        values = [v for v in value if v]
        if not values:
            return None, None, True
        value = values[0]
    text = str(value).strip()
    if not text:
        return None, None, True

    match = _DATE_RE.match(text)
    if not match:
        return None, None, False

    date_part = match.group("date")
    offset = match.group("offset")
    try:
        dt.date.fromisoformat(date_part)
    except ValueError:
        return None, None, False

    if offset == "Z":
        offset = "+00:00"
    elif offset and ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"

    time_part = match.group("time")
    iso = f"{date_part}T{time_part}" if time_part else date_part
    return iso, offset, True


def parse_money(
    amount: Any, currency: Any
) -> tuple[float | None, str | None, bool]:
    """Pair a TED amount with its separately-stored currency.

    Returns ``(amount, currency, single_currency)``. ``single_currency`` is
    False when the currency field held more than one distinct code, which means
    the amount cannot be trusted as a single figure.
    """
    amounts = [a for a in as_list(amount) if isinstance(a, (int, float))]
    currencies = unique(str(c).upper() for c in as_list(currency) if c)

    total = float(sum(amounts)) if amounts else None
    single = len(currencies) <= 1
    return total, (currencies[0] if currencies else None), single


# ---------------------------------------------------------------------------
# the normalizer
# ---------------------------------------------------------------------------


def normalize(
    raw: dict[str, Any],
    *,
    known_fields: Sequence[str] = (),
    relevance_keywords: Sequence[str] = (),
    langs: Sequence[str] = DEFAULT_LANGS,
) -> NormalizedNotice:
    """Normalize one raw notice. Never raises on bad data — it flags it."""
    pub_no = raw.get("publication-number")
    if not pub_no:
        # The only unrecoverable case: without the primary key there is nothing
        # to store the anomaly against.
        raise ValueError("notice has no publication-number")

    n = NormalizedNotice(publication_number=str(pub_no))

    # -- schema drift ------------------------------------------------------
    if known_fields:
        extra = sorted(set(raw) - set(known_fields))
        if extra:
            n.unknown_fields = extra
            n.flag(A_UNKNOWN_FIELDS)

    # -- identity ----------------------------------------------------------
    n.notice_identifier = _scalar(raw.get("notice-identifier"))
    n.notice_type = _scalar(raw.get("notice-type"))
    n.notice_subtype = _scalar(raw.get("notice-subtype"))
    n.form_type = _scalar(raw.get("form-type"))

    pub_date, pub_offset, ok = parse_ted_date(raw.get("publication-date"))
    n.publication_date, n.publication_offset = pub_date, pub_offset
    if not ok:
        n.flag(A_BAD_DATE)
    if pub_date is None:
        n.flag(A_NO_PUBLICATION_DATE)

    # -- text --------------------------------------------------------------
    # title-proc is the procurement's own title; notice-title is the
    # TED-generated headline ("Spain – IT services: ... – <title>"), which is
    # noisier. Prefer the former and fall back.
    n.title, n.title_lang = pick_language(raw.get("title-proc"), langs)
    if not n.title:
        n.title, n.title_lang = pick_language(raw.get("notice-title"), langs)
    if not n.title:
        n.flag(A_NO_TITLE)

    n.description, n.description_lang = pick_language(
        raw.get("description-proc"), langs
    )
    n.lot_descriptions, lot_desc_lang = language_items(
        raw.get("description-lot"), langs
    )
    n.lot_titles, _ = language_items(raw.get("title-lot"), langs)
    if not n.description:
        # Some notices carry no procedure-level description, only per-lot ones.
        if n.lot_descriptions:
            n.description = "\n\n".join(n.lot_descriptions)
            n.description_lang = lot_desc_lang
        else:
            n.flag(A_NO_DESCRIPTION)

    # -- buyer -------------------------------------------------------------
    n.buyer_name, _ = pick_language(raw.get("buyer-name"), langs)
    if not n.buyer_name:
        n.flag(A_NO_BUYER_NAME)
    n.buyer_city, _ = pick_language(raw.get("buyer-city"), langs)
    countries = unique(str(c).upper() for c in as_list(raw.get("buyer-country")))
    n.buyer_country = countries[0] if countries else None
    n.buyer_identifiers = unique(str(i) for i in as_list(raw.get("buyer-identifier")))
    if len(n.buyer_identifiers) > 1:
        # e.g. ["50494010087781", "Q2822001J"] — a DIR3 code and a Spanish NIF.
        # Both identify the same buyer under different national schemes, so
        # entity resolution cannot key on a single identifier.
        n.flag(A_MULTI_BUYER_ID_SCHEME)
    n.buyer_legal_type = _scalar(raw.get("buyer-legal-type"))
    n.main_activity = _scalar(raw.get("main-activity"))

    # -- classification ----------------------------------------------------
    n.cpv_codes = unique(str(c) for c in as_list(raw.get("classification-cpv")))
    main = unique(str(c) for c in as_list(raw.get("main-classification-proc")))
    n.main_cpv = main[0] if main else (n.cpv_codes[0] if n.cpv_codes else None)
    n.cpv_divisions = unique(c[:2] for c in n.cpv_codes if len(c) >= 2)
    if not n.cpv_codes:
        n.flag(A_NO_CPV)

    # -- lots --------------------------------------------------------------
    lot_values = as_list(raw.get("estimated-value-lot"))
    counts = {
        "title-lot": len(n.lot_titles),
        "description-lot": len(n.lot_descriptions),
        "estimated-value-lot": len(lot_values),
    }
    n.lot_count = max(counts.values())
    non_zero = {v for v in counts.values() if v}
    if len(non_zero) > 1:
        # Per-lot arrays disagree on how many lots there are. Worth knowing
        # before anyone builds per-lot logic on top of this.
        n.flag(A_LOT_CARDINALITY_MISMATCH)

    # -- money -------------------------------------------------------------
    _resolve_value(n, raw, lot_values)

    # -- dates -------------------------------------------------------------
    deadline, _, ok = parse_ted_date(raw.get("deadline-receipt-request"))
    if deadline is None:
        deadline, _, ok2 = parse_ted_date(
            raw.get("deadline-receipt-tender-date-lot")
        )
        ok = ok and ok2
    n.deadline = deadline
    if not ok:
        n.flag(A_BAD_DATE)
    if deadline and pub_date and deadline[:10] < pub_date[:10]:
        # Common on award and modification notices, which quote the original
        # tender deadline. Flagged rather than corrected, because for a
        # contract notice it would mean the data is wrong.
        n.flag(A_DEADLINE_BEFORE_PUBLICATION)

    # -- place -------------------------------------------------------------
    _resolve_places(n, raw)

    # -- duration and requirement text ------------------------------------
    _resolve_duration(n, raw)
    _resolve_requirements(n, raw, langs)

    # -- procedure and outcome --------------------------------------------
    n.procedure_type = _scalar(raw.get("procedure-type"))
    natures = unique(str(x) for x in as_list(raw.get("contract-nature")))
    n.contract_nature = natures[0] if natures else None
    winners, _ = language_items(raw.get("winner-name"), langs)
    n.winner_names = unique(winners)
    n.contract_url = _first_url(raw.get("contract-url"))
    n.notice_url = _notice_url(raw.get("links"), n.publication_number)

    # -- relevance ---------------------------------------------------------
    if relevance_keywords:
        haystack = " ".join(
            part
            for part in (
                n.title,
                n.description,
                n.buyer_name,
                " ".join(n.lot_titles),
            )
            if part
        ).lower()
        n.matched_keywords = [k for k in relevance_keywords if k.lower() in haystack]
        n.is_relevant = bool(n.matched_keywords)

    return n


def _resolve_value(
    n: NormalizedNotice, raw: dict[str, Any], lot_values: list[Any]
) -> None:
    """Pick a single contract value, recording where it came from.

    Preference order: the awarded total, then the procedure-level estimate,
    then the sum of per-lot estimates. The last is a derivation, not a
    reported figure, so it is flagged.
    """
    for source, amount_field, currency_field in (
        ("total", "total-value", "total-value-cur"),
        ("estimated_proc", "estimated-value-proc", "estimated-value-cur-proc"),
    ):
        amount, currency, single = parse_money(
            raw.get(amount_field), raw.get(currency_field)
        )
        if amount is not None:
            n.value_amount, n.value_currency, n.value_source = amount, currency, source
            if not single:
                n.flag(A_MULTI_CURRENCY)
            return

    if lot_values:
        amount, currency, single = parse_money(
            lot_values, raw.get("estimated-value-cur-lot")
        )
        if amount is not None:
            n.value_amount = amount
            n.value_currency = currency
            n.value_source = "estimated_lot_sum"
            n.flag(A_VALUE_FROM_LOT_SUM)
            if not single:
                # Summing across currencies produces a meaningless number;
                # refuse to store it rather than store a plausible-looking lie.
                n.value_amount = None
                n.value_source = None
                n.flag(A_MULTI_CURRENCY)
                n.flag(A_NO_VALUE)
            return

    n.flag(A_NO_VALUE)


def _resolve_places(n: NormalizedNotice, raw: dict[str, Any]) -> None:
    """Split place-of-performance into NUTS regions and ISO-3 countries.

    TED mixes both into one array, repeats them per lot, and uses the literal
    ``"anyw"`` for location-independent contracts.
    """
    mixed = unique(
        str(p).strip().upper()
        for p in as_list(raw.get("place-of-performance"))
        if str(p).strip()
    )

    nuts: list[str] = []
    countries: list[str] = []
    for code in mixed:
        if code.lower() == ANYWHERE:
            n.performance_anywhere = True
        elif _COUNTRY_RE.match(code):
            countries.append(code)
        elif _NUTS_RE.match(code):
            nuts.append(code)

    nuts += [
        str(c).strip().upper()
        for c in as_list(raw.get("place-of-performance-subdiv-lot"))
        if str(c).strip()
    ]
    countries += [
        str(c).strip().upper()
        for c in as_list(raw.get("place-of-performance-country-lot"))
        if str(c).strip()
    ]

    n.nuts_codes = unique(nuts)
    n.performance_countries = unique(countries)


def _resolve_duration(n: NormalizedNotice, raw: dict[str, Any]) -> None:
    """Convert TED's value+unit duration pair to months.

    Per-lot durations can differ; the longest is kept, because for a vendor
    deciding whether a contract is worth pursuing the overall span matters
    more than the shortest lot.
    """
    values = [v for v in as_list(raw.get("duration-period-value-lot"))]
    units = [str(u).strip().upper() for u in as_list(raw.get("duration-period-unit-lot"))]
    if not values:
        return

    n.duration_raw = ", ".join(
        f"{v} {units[i] if i < len(units) else '?'}" for i, v in enumerate(values)
    )

    best: float | None = None
    for index, value in enumerate(values):
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        unit = units[index] if index < len(units) else (units[0] if units else "")
        factor = DURATION_UNIT_MONTHS.get(unit)
        if factor is None:
            n.flag(A_UNKNOWN_DURATION_UNIT)
            continue
        months = amount * factor
        if best is None or months > best:
            best = months

    if best is not None:
        n.duration_months = int(round(best))


def _resolve_requirements(
    n: NormalizedNotice, raw: dict[str, Any], langs: Sequence[str]
) -> None:
    """Collect the requirement-text blocks the extractor will actually read."""
    for name in REQUIREMENT_FIELDS:
        items, _ = language_items(raw.get(name), langs)
        # Per-lot arrays repeat the same boilerplate across lots; dedupe so a
        # seven-lot notice does not pay seven times the tokens for one clause.
        items = [t for t in unique(items) if t]
        if items:
            n.requirements[name] = items

    if not n.requirements:
        n.flag(A_NO_REQUIREMENT_TEXT)


def requirement_text(requirements: dict[str, list[str]], *, max_chars: int = 8000) -> str:
    """Render the requirement blocks as labelled prose for a prompt."""
    labels = {
        "selection-criterion-description-lot": "CRITERIOS DE SELECCIÓN",
        "csecurity-clearance-description-lot": "SEGURIDAD / ACREDITACIONES",
        "award-criterion-description-lot": "CRITERIOS DE ADJUDICACIÓN",
        "award-criterion-name-lot": "CRITERIOS DE ADJUDICACIÓN (NOMBRES)",
        "exclusion-grounds-description": "MOTIVOS DE EXCLUSIÓN",
        "guarantee-required-description-lot": "GARANTÍAS EXIGIDAS",
        "renewal-description-lot": "RENOVACIÓN",
        "option-description-lot": "PRÓRROGAS / OPCIONES",
    }
    parts: list[str] = []
    budget = max_chars
    for name in REQUIREMENT_FIELDS:
        items = requirements.get(name)
        if not items:
            continue
        body = "\n".join(f"  - {t}" for t in items)
        block = f"{labels.get(name, name)}:\n{body}"
        if len(block) > budget:
            block = block[: max(0, budget)] + "\n  [...truncado...]"
        parts.append(block)
        budget -= len(block)
        if budget <= 0:
            break
    return "\n\n".join(parts)


def _scalar(value: Any) -> str | None:
    items = as_list(value)
    if not items:
        return None
    first = items[0]
    if isinstance(first, dict):
        text, _ = pick_language(first)
        return text
    text = str(first).strip()
    return text or None


def _first_url(value: Any) -> str | None:
    for item in as_list(value):
        text = str(item).strip()
        if text.startswith("http"):
            return text
    return None


def _notice_url(links: Any, publication_number: str) -> str:
    """Prefer the Spanish human-readable page, else build the canonical one."""
    if isinstance(links, dict):
        html = links.get("html") or links.get("htmlDirect") or {}
        if isinstance(html, dict):
            for key in ("SPA", "ENG"):
                url = html.get(key)
                if url:
                    return str(url)
            for url in html.values():
                if url:
                    return str(url)
    return f"https://ted.europa.eu/en/notice/-/detail/{publication_number}"
