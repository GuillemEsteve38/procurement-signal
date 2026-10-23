"""Search profiles — the client's definition of "a tender worth knowing about".

A profile is the only thing that should need changing to point this at a
different market. It compiles to a TED expert-search query string.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# Fields we ask TED for. Every name here was validated against the live API;
# see scripts/dump_fields.py for how to enumerate the full 1833-name vocabulary.
NOTICE_FIELDS: tuple[str, ...] = (
    # identity and provenance
    "publication-number",
    "notice-identifier",
    "publication-date",
    "notice-type",
    "notice-subtype",
    "form-type",
    # what it is
    "notice-title",
    "title-proc",
    "description-proc",
    "title-lot",
    "description-lot",
    # who is buying
    "buyer-name",
    "buyer-city",
    "buyer-country",
    "buyer-identifier",
    "buyer-legal-type",
    "main-activity",
    # classification
    "classification-cpv",
    "main-classification-proc",
    # money (amount and currency are separate fields, with different shapes)
    "total-value",
    "total-value-cur",
    "estimated-value-proc",
    "estimated-value-cur-proc",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    # when
    "deadline-receipt-request",
    "deadline-receipt-tender-date-lot",
    # Contract duration is published structured, as a value plus a unit code.
    "duration-period-value-lot",
    "duration-period-unit-lot",
    # Requirement text. Added after measuring population on open contract
    # notices: award criteria 81%, selection criteria 70%, exclusion grounds
    # 64%, renewal/option ~20%, security clearance ~1%. description-proc alone
    # is a 188-character subject line and cannot support extraction; see
    # docs/EVAL.md.
    "award-criterion-description-lot",
    "award-criterion-name-lot",
    "selection-criterion-description-lot",
    "exclusion-grounds-description",
    "renewal-description-lot",
    "option-description-lot",
    "guarantee-required-description-lot",
    "csecurity-clearance-description-lot",
    # where
    "place-of-performance",
    "place-of-performance-country-lot",
    "place-of-performance-subdiv-lot",
    # how
    "procedure-type",
    "contract-nature",
    # outcome (award notices only)
    "winner-name",
    "contract-url",
    # links to the human-readable notice
    "links",
)


@dataclass(frozen=True)
class Profile:
    """A named market definition."""

    name: str
    cpv_codes: tuple[str, ...]
    buyer_countries: tuple[str, ...] = ()
    #: Lower-cased substrings; a notice is *tagged* relevant if any appears in
    #: its title or description. Tagging never excludes a notice from ingestion —
    #: filtering at read time is reversible, filtering at ingest time is not.
    relevance_keywords: tuple[str, ...] = ()
    sort: str = "publication-number DESC"
    fields: tuple[str, ...] = field(default=NOTICE_FIELDS)

    def query(self, date_from: dt.date, date_to: dt.date | None = None) -> str:
        """Compile to a TED expert-search query.

        CPV codes must be given in full 8-digit form — the API rejects the
        2-digit family prefix with QUERY_UNSUPPORTED_FIELD_VALUE. Hierarchical
        matching is implicit: ``classification-cpv=72000000`` also matches
        ``72212900``.
        """
        clauses: list[str] = []

        if not self.cpv_codes:
            raise ValueError(f"profile {self.name!r} has no CPV codes")
        cpv = " OR ".join(f"classification-cpv={c}" for c in self.cpv_codes)
        clauses.append(f"({cpv})")

        if self.buyer_countries:
            countries = " OR ".join(
                f"buyer-country={c}" for c in self.buyer_countries
            )
            clauses.append(f"({countries})")

        clauses.append(f"publication-date>={_ted_date(date_from)}")
        if date_to is not None:
            clauses.append(f"publication-date<={_ted_date(date_to)}")

        return " AND ".join(clauses) + f" SORT BY {self.sort}"


def _ted_date(d: dt.date) -> str:
    """TED expert search wants bare ``YYYYMMDD``, not ISO-8601."""
    return d.strftime("%Y%m%d")


#: CPV 72xxxxxx = IT services, 48xxxxxx = software packages.
#: Health relevance is approximated by CPV + keyword rather than by buyer type;
#: see SCOPE.md, Assumption 1.
SPANISH_HEALTH_IT = Profile(
    name="spanish-health-it",
    cpv_codes=("72000000", "48000000"),
    buyer_countries=("ESP",),
    relevance_keywords=(
        "salud",
        "sanitari",
        "sanitat",
        "hospital",
        "clínic",
        "clinic",
        "historia clínica",
        "farmaci",
        "asistencial",
        "médic",
        "medic",
        "paciente",
        "health",
        "osasun",  # Basque
        "saúde",
    ),
)

EU_WIDE_IT = Profile(
    name="eu-wide-it",
    cpv_codes=("72000000",),
)

PROFILES: dict[str, Profile] = {
    p.name: p for p in (SPANISH_HEALTH_IT, EU_WIDE_IT)
}
