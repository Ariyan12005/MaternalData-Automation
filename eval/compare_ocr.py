"""Score OCR engines field by field against eval/ground_truth.json.

Usage (from the repo root):
    python3 eval/compare_ocr.py                      # Tesseract, all patients
    python3 eval/compare_ocr.py --engines tesseract,paddle --markdown

The ground truth lives only in eval/ground_truth.json. Nothing in dayone/ imports
it or this script, so the extractor never sees an expected value.

Verdicts per field (visits are matched by grid column, e.g. M9):
    OK            KNOWN and equal to the truth
    FALSE-KNOWN   KNOWN but different from the truth, or KNOWN where the page is blank  <- must be 0
    review        NEEDS_REVIEW / ILLEGIBLE / UNKNOWN: a person decides ("candidate ok" = suggestion is right)
    review-blank  sent to review although the cell is blank (costs a click, never wrong data)
    empty-ok      NOT_PROVIDED where the page is blank or holds a dash
    missed        NOT_PROVIDED, or the visit not found, although the page has a value  <- should be 0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dayone import schema  # noqa: E402
from dayone.extraction import ExtractionError  # noqa: E402
from dayone.live_ocr import LiveOcrExtractor, PaddleOcrReader, TesseractOcrReader  # noqa: E402

ENGINES = {"tesseract": TesseractOcrReader, "paddle": PaddleOcrReader}
VERDICTS = ("OK", "FALSE-KNOWN", "review", "review-blank", "empty-ok", "missed")


def verdict(field: dict | None, truth) -> tuple[str, bool]:
    """Return (verdict, candidate_is_right)."""
    if field is None:
        return ("missed", False) if truth is not None else ("empty-ok", False)
    status, value = field["field_status"], field["value"]
    if status == "KNOWN":
        return ("OK" if value == truth else "FALSE-KNOWN"), False
    if status == "NOT_PROVIDED":
        return ("empty-ok" if truth is None else "missed"), False
    if truth is None:
        return "review-blank", False
    return "review", value is not None and value == truth


def score_case(draft: dict, expected: dict, totals: Counter, by_field: dict, problems: list, case_id: str) -> None:
    def record(name, label, field, truth):
        result, candidate = verdict(field, truth)
        totals[result] += 1
        by_field.setdefault(name, Counter())[result] += 1
        if candidate:
            totals["candidate ok"] += 1
            by_field[name]["candidate ok"] += 1
        if result in ("FALSE-KNOWN", "missed"):
            got = f"{field['field_status']} {field['value']!r}" if field else "no visit"
            problems.append(f"{case_id} {label} {name}: truth {truth!r}, got {got}")

    for name, truth in expected["document"].items():
        record(name, "document", draft["document_fields"][name], truth)
    found = {encounter["slot"]: encounter["fields"] for encounter in draft["encounters"]}
    expected_slots = {encounter["slot"]: encounter for encounter in expected["encounters"]}
    for slot, truth_encounter in expected_slots.items():
        for spec in schema.ENCOUNTER_FIELDS:
            record(spec.name, slot, found.get(slot, {}).get(spec.name), truth_encounter[spec.name])
    for slot, fields in found.items():  # visits read where the page has none
        if slot not in expected_slots:
            for name, field in fields.items():
                record(name, f"{slot}(no visit on page)", field, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engines", default="tesseract")
    parser.add_argument("--truth", default=str(Path(__file__).with_name("ground_truth.json")))
    parser.add_argument("--markdown", action="store_true", help="print the per-field table as Markdown")
    args = parser.parse_args()

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    exit_code = 0
    for name in [n.strip() for n in args.engines.split(",") if n.strip()]:
        extractor = LiveOcrExtractor(REPO_ROOT, reader=ENGINES[name]())
        totals, by_field, problems, seconds = Counter(), {}, [], []
        for case in truth["cases"]:
            started = time.perf_counter()
            try:
                draft = extractor.extract(case["pages"])
            except ExtractionError as exc:
                print(f"{name} {case['id']}: {exc.code}: {exc.message}")
                exit_code = 1
                continue
            seconds.append(time.perf_counter() - started)
            score_case(draft, case["expected"], totals, by_field, problems, case["id"])

        print(f"\n=== {name}: {len(seconds)} patients, {sum(seconds) / max(1, len(seconds)):.1f} s per patient ===")
        columns = (*VERDICTS, "candidate ok")
        rows = [[field, *(str(counter[v]) for v in columns)] for field, counter in by_field.items()]
        rows.append(["TOTAL", *(str(totals[v]) for v in columns)])
        if args.markdown:
            print("| field | " + " | ".join(columns) + " |")
            print("|---|" + "---:|" * len(columns))
            for row in rows:
                print("| " + " | ".join(row) + " |")
        else:
            print(f"{'field':>24}" + "".join(f"{c:>13}" for c in columns))
            for row in rows:
                print(f"{row[0]:>24}" + "".join(f"{c:>13}" for c in row[1:]))
        valued = totals["OK"] + totals["FALSE-KNOWN"] + totals["review"] + totals["missed"]
        if valued:
            print(f"\nValues on the page: {valued}. KNOWN and right: {totals['OK']} ({totals['OK'] / valued:.0%}); "
                  f"to review: {totals['review']} (right value suggested for {totals['candidate ok']}); "
                  f"FALSE-KNOWN: {totals['FALSE-KNOWN']}; missed: {totals['missed']}.")
        for line in problems:
            print("  !", line)
        if totals["FALSE-KNOWN"] or totals["missed"]:
            exit_code = 1
    print("\nRule: FALSE-KNOWN and missed must be 0. Ground truth is unverified (see its _note).")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
