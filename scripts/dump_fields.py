"""Enumerate every selectable TED search field.

TED publishes no field list and has no metadata endpoint (``/v3/notices/fields``
returns 400). But the API validates ``fields`` against an allow-list and puts
*the entire allow-list* in the 400 body, so asking for a field that cannot
exist is a documentation endpoint in disguise.

    python scripts/dump_fields.py        # writes docs/ted_fields.txt
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
SENTINEL = "__no_such_field__"
OUT = Path(__file__).resolve().parents[1] / "docs" / "ted_fields.txt"


def fetch_field_names() -> list[str]:
    body = json.dumps(
        {"query": "classification-cpv=72000000", "limit": 1, "fields": [SENTINEL]}
    ).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_URL, data=body, headers={"Content-Type": "application/json"}
    )

    try:
        urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
    else:
        raise SystemExit("expected a 400; the sentinel field name may now be valid")

    try:
        message = json.loads(raw)["error"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        message = raw

    marker = "supported values are:"
    index = message.find(marker)
    if index < 0:
        raise SystemExit(f"could not find the field list in: {message[:300]}")

    # The list is a comma-separated tail wrapped in a trailing paren. Several
    # field names contain no commas, so a naive split is safe here.
    tail = message[index + len(marker) :].rstrip().rstrip(")")
    return sorted({name.strip() for name in tail.split(",") if name.strip()})


def main() -> int:
    names = fetch_field_names()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"{len(names)} field names -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
