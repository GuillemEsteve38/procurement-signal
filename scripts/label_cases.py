"""Label the eval cases that have no structured ground truth.

Three extraction fields have no counterpart anywhere in TED, which is exactly
why they are worth extracting — and why they can only be scored against human
judgement:

* ``is_health_related``       — the keyword filter's false-positive problem
* ``work_category``           — what kind of IT work it actually is
* ``required_certifications`` — what the supplier must hold

    python scripts/label_cases.py                  # label everything unlabelled
    python scripts/label_cases.py --field is_health_related
    python scripts/label_cases.py --stratum keyword_false_positive_candidate
    python scripts/label_cases.py --show 737081-2026

Labels are written back into evals/cases.jsonl in place. Answering is
deliberately quick — one keypress for the boolean, a number for the category —
because an eval set only gets labelled if labelling is cheap.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from procsignal.extract import WORK_CATEGORIES  # noqa: E402

DEFAULT_CASES = "evals/cases.jsonl"
LABEL_FIELDS = ("is_health_related", "work_category", "required_certifications")


def load(path: Path) -> tuple[list[dict], dict]:
    meta: dict = {}
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if "_meta" in payload:
                meta = payload
                continue
            rows.append(payload)
    return rows, meta


def save(path: Path, rows: list[dict], meta: dict) -> None:
    # Write via a temp file then replace, so an interrupted save cannot leave
    # a half-written case set behind.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        if meta:
            handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    shutil.move(str(tmp), str(path))


def show_case(row: dict, *, full: bool = False) -> None:
    inp = row["input"]
    print()
    print("=" * 76)
    print(f"{row['publication_number']}   [{row['stratum']}]")
    print("=" * 76)
    print(f"TITLE: {inp.get('title') or '(none)'}")
    lots = inp.get("lot_titles") or []
    if lots:
        print(f"LOTS ({len(lots)}):")
        for lot in lots[:6]:
            print(f"  - {lot}")
    desc = inp.get("description") or "(none)"
    print(f"\nDESCRIPTION:\n{desc if full else desc[:900]}")
    req = inp.get("requirement_text") or ""
    if req:
        print(f"\n{req if full else req[:900]}")
    print(f"\nTED says: cpv={row['truth'].get('cpv_division')}"
          f"  nature={row['truth'].get('contract_nature')}"
          f"  lots={row['truth'].get('lot_count')}")


def ask_bool(prompt: str) -> bool | None:
    while True:
        answer = input(f"{prompt} [y/n/s=skip/q=quit] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer in ("s", ""):
            return None
        if answer in ("q", "quit"):
            raise KeyboardInterrupt


def ask_category() -> str | None:
    print("\n  work_category:")
    for index, name in enumerate(WORK_CATEGORIES, start=1):
        print(f"    {index:>2}. {name}")
    while True:
        answer = input("  number [s=skip/q=quit] ").strip().lower()
        if answer in ("s", ""):
            return None
        if answer in ("q", "quit"):
            raise KeyboardInterrupt
        if answer.isdigit() and 1 <= int(answer) <= len(WORK_CATEGORIES):
            return WORK_CATEGORIES[int(answer) - 1]
        print("  not a valid number")


def ask_certs() -> list[str] | None:
    answer = input(
        "  required_certifications, comma separated "
        "(empty = none required, s = skip) "
    ).strip()
    if answer.lower() in ("s",):
        return None
    if not answer:
        return []
    return [part.strip() for part in answer.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(prog="label_cases.py")
    parser.add_argument("--cases", default=DEFAULT_CASES)
    parser.add_argument("--field", choices=LABEL_FIELDS, default=None)
    parser.add_argument("--stratum", default=None)
    parser.add_argument("--show", default=None, help="print one case and exit")
    parser.add_argument("--relabel", action="store_true", help="revisit labelled cases")
    parser.add_argument("--full", action="store_true", help="do not truncate text")
    args = parser.parse_args(argv)

    path = Path(args.cases)
    if not path.exists():
        print(f"no case set at {path}; run: python -m procsignal.evaluate build")
        return 1

    rows, meta = load(path)

    if args.show:
        match = next(
            (r for r in rows if r["publication_number"] == args.show), None
        )
        if match is None:
            print(f"no case for {args.show}")
            return 1
        show_case(match, full=True)
        print(f"\nlabels: {json.dumps(match.get('labels'), ensure_ascii=False)}")
        return 0

    fields = (args.field,) if args.field else LABEL_FIELDS
    todo = [
        r
        for r in rows
        if (args.stratum is None or r["stratum"] == args.stratum)
        and (
            args.relabel
            or any(r.get("labels", {}).get(f) is None for f in fields)
        )
    ]

    if not todo:
        print("nothing to label")
        return 0

    print(f"{len(todo)} case(s) to label. Ctrl-C or 'q' saves and exits.")
    labelled = 0
    try:
        for index, row in enumerate(todo, start=1):
            labels = row.setdefault("labels", {})
            print(f"\n[{index}/{len(todo)}]", end="")
            show_case(row, full=args.full)

            if "is_health_related" in fields and (
                args.relabel or labels.get("is_health_related") is None
            ):
                answer = ask_bool("\n  is the SUBJECT MATTER health/clinical?")
                if answer is not None:
                    labels["is_health_related"] = answer
                    labelled += 1

            if "work_category" in fields and (
                args.relabel or labels.get("work_category") is None
            ):
                answer = ask_category()
                if answer is not None:
                    labels["work_category"] = answer
                    labelled += 1

            if "required_certifications" in fields and (
                args.relabel or labels.get("required_certifications") is None
            ):
                answer = ask_certs()
                if answer is not None:
                    labels["required_certifications"] = answer
                    labelled += 1
    except (KeyboardInterrupt, EOFError):
        print("\ninterrupted — saving what has been labelled so far")

    save(path, rows, meta)

    remaining = {
        f: sum(1 for r in rows if r.get("labels", {}).get(f) is None)
        for f in LABEL_FIELDS
    }
    print(f"\nwrote {labelled} label(s) to {path}")
    print("still unlabelled:")
    for name, count in remaining.items():
        print(f"  {name:<28} {count}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
