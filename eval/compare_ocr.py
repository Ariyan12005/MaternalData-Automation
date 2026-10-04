"""Compare OCR engines on the same pages, field by field, against a ground-truth file.

Usage (from the repo root):
    python3 eval/compare_ocr.py
    python3 eval/compare_ocr.py --engines tesseract,paddle --runs 2

The ground truth lives only in eval/ground_truth.json. Nothing in dayone/ imports
it or this script, so the extractor never sees an expected value.

Verdicts per field:
    OK         KNOWN and equal to the truth
    FALSE-KNOWN  KNOWN but different from the truth (or truth is "not on the page")  <- must be 0
    review     NEEDS_REVIEW / ILLEGIBLE / UNKNOWN (a human decides)
    missing    NOT_PROVIDED (nothing found)
A review value that already equals the truth is marked "(candidate ok)".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dayone.extraction import ExtractionError  # noqa: E402
from dayone.live_ocr import LiveOcrExtractor, OcrLine, PaddleOcrReader, TesseractOcrReader  # noqa: E402


def split_box_into_words(text: str, x_min: float, y_min: float, x_max: float, score: float) -> list[OcrLine]:
    """Turn one OCR text box into word-level lines.

    The extractor matches printed labels word by word, which is what Tesseract returns.
    A box-level engine (Paddle) gives whole phrases, so each word's left edge is
    estimated by its character offset inside the box. This is an approximation.
    """
    text = text.strip()
    if not text:
        return []
    words, cursor = [], 0
    for word in text.split():
        offset = text.index(word, cursor)
        cursor = offset + len(word)
        left = x_min + (x_max - x_min) * offset / len(text)
        words.append(OcrLine(word, float(score), int(left), int(y_min)))
    return words


class PaddleWordReader:
    """PaddleOCR v3 (``predict``) adapted to word-level OcrLine rows. Lazy import."""

    def __init__(self):
        self._engine = None

    def read(self, image_path: Path) -> list[OcrLine]:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "PaddleOCR is not installed") from exc
        if self._engine is None:
            self._engine = PaddleOCR(lang="fr")
        lines: list[OcrLine] = []
        for result in self._engine.predict(str(image_path)):
            texts, scores = result["rec_texts"], result["rec_scores"]
            boxes = result.get("rec_boxes")
            if boxes is None:
                boxes = [[min(p[0] for p in poly), min(p[1] for p in poly),
                          max(p[0] for p in poly), max(p[1] for p in poly)] for poly in result["rec_polys"]]
            for text, score, box in zip(texts, scores, boxes):
                x_min, y_min, x_max = float(box[0]), float(box[1]), float(box[2])
                lines.extend(split_box_into_words(str(text), x_min, y_min, x_max, float(score)))
        return lines


class PaddleCropReader:
    """The app's Paddle reader run only on the regions the parser reads.

    Page 01: top strip (registry number). Page 03: right-hand visit column.
    Other pages return no lines, so personal-data label detection is not exercised.
    Coordinates are shifted back to full-page positions for the fixed-layout parser.
    """

    CROPS = {"01": lambda w, h: (0, 0, w, int(h * 0.15)), "03": lambda w, h: (1380, 0, w, h)}

    def __init__(self):
        self._reader = PaddleOcrReader()

    def read(self, image_path: Path) -> list[OcrLine]:
        import tempfile
        from PIL import Image
        crop = self.CROPS.get(image_path.stem.split("-")[-1])
        if crop is None:
            return []
        with Image.open(image_path) as image:
            box = crop(*image.size)
            with tempfile.TemporaryDirectory() as tmp:
                cropped = Path(tmp) / image_path.name
                image.convert("RGB").crop(box).save(cropped)
                lines = self._reader.read(cropped)
        return [OcrLine(line.text, line.confidence, line.left + box[0], line.top + box[1]) for line in lines]


# "paddle" is the app's own reader (PP-OCRv6, box-level lines, which the current parser expects).
# "paddle-words" is the word-splitting adapter above, with PaddleOCR's default French models.
# "paddle-crop" is the app's reader on the two regions the parser reads (much faster).
ENGINES = {"tesseract": TesseractOcrReader, "paddle": PaddleOcrReader, "paddle-words": PaddleWordReader,
           "paddle-crop": PaddleCropReader}
FIELD_ORDER = [
    "registry_file_number", "midwife_patient_code", "facility_name", "last_menstrual_period",
    "visit_date", "gestational_age_days", "weight_kg", "systolic_bp_mmhg", "diastolic_bp_mmhg",
    "fundal_height_cm", "syphilis_test", "hiv_test",
]


def flatten(draft: dict) -> dict[str, dict]:
    fields = dict(draft["document_fields"])
    fields.update(draft["encounters"][0]["fields"])
    return fields


def verdict(field: dict, truth) -> tuple[str, str]:
    status, value = field["field_status"], field["value"]
    if status == "KNOWN":
        return ("OK", "") if value == truth else ("FALSE-KNOWN", "")
    if status == "NOT_PROVIDED":
        return "missing", ""
    return "review", " (candidate ok)" if value is not None and value == truth else ""


def run_case(reader, case: dict, runs: int):
    extractor = LiveOcrExtractor(REPO_ROOT, reader=reader)
    seconds, draft = [], None
    for _ in range(runs):
        started = time.perf_counter()
        draft = extractor.extract(case["pages"])
        seconds.append(time.perf_counter() - started)
    return draft, seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engines", default="tesseract,paddle")
    parser.add_argument("--truth", default=str(Path(__file__).with_name("ground_truth.json")))
    parser.add_argument("--runs", type=int, default=2, help="repeat each case; first run includes model loading")
    args = parser.parse_args()

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    names = [name.strip() for name in args.engines.split(",") if name.strip()]
    totals = {name: {"OK": 0, "FALSE-KNOWN": 0, "review": 0, "missing": 0, "candidate_ok": 0, "seconds": [], "errors": []}
              for name in names}

    for case in truth["cases"]:
        print(f"\n=== {case['id']} ===")
        results: dict[str, dict | str] = {}
        for name in names:
            if name not in ENGINES:
                results[name] = f"unknown engine '{name}'"
                continue
            try:
                draft, seconds = run_case(ENGINES[name](), case, max(1, args.runs))
            except ExtractionError as exc:
                results[name] = f"{exc.code}: {exc.message}"
                totals[name]["errors"].append(exc.code)
                continue
            except Exception as exc:  # engine crashed: report, keep comparing the others
                results[name] = f"{type(exc).__name__}: {exc}"
                totals[name]["errors"].append(type(exc).__name__)
                continue
            results[name] = flatten(draft)
            totals[name]["seconds"].append(seconds)

        header = f"{'field':24}{'truth':16}" + "".join(f"{name:34}" for name in names)
        print(header)
        print("-" * len(header))
        for field_name in FIELD_ORDER:
            expected = case["expected"].get(field_name)
            row = f"{field_name:24}{str(expected):16}"
            for name in names:
                outcome = results[name]
                if isinstance(outcome, str):
                    row += f"{'(no result)':34}"
                    continue
                field = outcome[field_name]
                label, note = verdict(field, expected)
                totals[name][label] += 1
                if note:
                    totals[name]["candidate_ok"] += 1
                shown = f"{label}: {field['value']}{note}" if field["value"] is not None else label
                row += f"{shown[:33]:34}"
            print(row)
        for name in names:
            if isinstance(results[name], str):
                print(f"  {name}: {results[name]}")

    print("\n=== Summary (all cases) ===")
    print(f"{'engine':12}{'OK':>5}{'FALSE-KNOWN':>13}{'review':>8}{'missing':>9}{'cand.ok':>9}   seconds (first -> last run)")
    for name in names:
        t = totals[name]
        if t["seconds"]:
            first = sum(s[0] for s in t["seconds"])
            last = sum(s[-1] for s in t["seconds"])
            timing = f"{first:.1f} -> {last:.1f}"
        else:
            timing = "n/a (" + ", ".join(t["errors"] or ["not run"]) + ")"
        print(f"{name:12}{t['OK']:>5}{t['FALSE-KNOWN']:>13}{t['review']:>8}{t['missing']:>9}{t['candidate_ok']:>9}   {timing}")
    print("\nRule: FALSE-KNOWN must be 0. Prefer the engine with 0 FALSE-KNOWN and more OK.")
    print("Ground truth was read by eye and is unverified; add more patients before deciding.")
    return 1 if any(t["FALSE-KNOWN"] for t in totals.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
