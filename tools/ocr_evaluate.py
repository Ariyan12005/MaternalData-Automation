"""Scores the local OCR against the manually checked ground truth (eval/specimen-ground-truth.json).

    .venv\\Scripts\\python tools/ocr_evaluate.py --split development
    .venv\\Scripts\\python tools/ocr_evaluate.py --split held_out

Pages are read through the server's OCR process pool (dayone/ocr_process.py: a separate process with the server's
page timeout), once per page and photo condition; each patient's document is then built from the same scans.

Conditions:
- clean: the PNG renders of the specimen (both splits).
- photo_like: the simulated phone photo used while tuning (development split only).
- perspective_shadow, lowres_noise: simulated photos never used for tuning (held-out split only).

Comparison rules: docs/ocr-evaluation.md. Generated copies get random names in a temporary folder that is deleted at
the end. Results go to var/ocr-eval/ (git-ignored): clinical values only; patient keys are reported as right or wrong
and never printed. Text recognised on a page is never logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import ocr_ground_truth as gt  # noqa: E402
import ocr_specimen_eval as ev  # noqa: E402
from dayone import extended, live_ocr, ocr_engines, ocr_process, schema  # noqa: E402
from dayone.extraction import ExtractionError  # noqa: E402

TRUTH = REPO_ROOT / "eval" / "specimen-ground-truth.json"
OUT_DIR = REPO_ROOT / "var" / "ocr-eval"
CONDITIONS = {"development": ("clean", "photo_like"), "held_out": ("clean", "perspective_shadow", "lowres_noise")}
GRID_FIELDS = ("visit_date", "gestational_age_days", "weight_kg", "systolic_bp_mmhg", "diastolic_bp_mmhg",
               "fundal_height_cm", "syphilis_test", "hiv_test")
WRITTEN = ("correct_known", "wrong_known", "review_right", "review_wrong", "review_empty", "marked_illegible",
           "claimed_missing", "no_field")
EMPTY = ("missing_ok", "absent_ok", "review_on_blank", "blank_marked_illegible", "wrong_known_blank")


def code_fingerprint() -> str:
    """Hash of the reader's code, so that the development and held-out runs can be shown to use the same code."""
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "dayone").glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------- photo conditions

def _perspective_shadow(page, folder: Path, seed: int) -> Path:
    """A photo taken from below the page: keystone, light falling off to one side, soft focus, JPEG 70."""
    from PIL import Image, ImageChops, ImageFilter

    w, h = page.size
    k = 0.04 + 0.01 * (seed % 3)
    quad = (-k * w, 0, 0, h, w, h, w * (1 + k), 0)  # upper-left, lower-left, lower-right, upper-right
    warped = page.transform((w, h), Image.QUAD, quad, Image.BICUBIC, fillcolor=(70, 60, 55))
    # Brightness falls from 100 % on the left to 62 % on the right.
    shade = Image.linear_gradient("L").rotate(90, expand=True).resize((w, h)).point(lambda v: 255 - round(0.38 * v))
    shaded = ImageChops.multiply(warped, Image.merge("RGB", (shade, shade, shade)))
    out = shaded.filter(ImageFilter.GaussianBlur(0.8))
    out.thumbnail((1600, 1600))
    return ev._save(out, folder, quality=70)


def _lowres_noise(page, folder: Path, seed: int) -> Path:
    """A small, noisy phone photo: 2.5 degrees of tilt, long side 1100 px, sensor noise, JPEG 55."""
    import numpy as np
    from PIL import Image

    tilted = page.rotate(2.5 if seed % 2 else -2.5, expand=True, resample=Image.BICUBIC, fillcolor=(80, 70, 60))
    tilted.thumbnail((1100, 1100), Image.LANCZOS)
    pixels = np.asarray(tilted, dtype=np.float32)
    noise = np.random.default_rng(seed).normal(0, 6, pixels.shape)
    noisy = Image.fromarray(np.clip(pixels + noise, 0, 255).astype("uint8"))
    return ev._save(noisy, folder, quality=55)


def page_file(number: int, condition: str, folder: Path) -> Path:
    from PIL import Image

    if condition == "clean":
        return ev.page_png(number)
    if condition == "photo_like":
        return ev.degraded_copy(number, folder)
    with Image.open(ev.page_png(number)) as source:
        page = source.convert("RGB")
    make = {"perspective_shadow": _perspective_shadow, "lowres_noise": _lowres_noise}[condition]
    return make(page, folder, number)


# ---------------------------------------------------------------- reading

class CachingReader:
    """Reads each file once through the OCR pool and remembers the scan (or the error) and the time it took."""

    def __init__(self, pool):
        self.pool = pool
        self.scans: dict[Path, object] = {}
        self.seconds: dict[Path, float] = {}

    def read(self, path, *, force=False, timeout=None):
        path = Path(path)
        if path not in self.scans:
            started = time.perf_counter()
            try:
                self.scans[path] = self.pool.read(path, force=force, timeout=timeout)
            except ExtractionError as exc:
                self.scans[path] = exc
            self.seconds[path] = time.perf_counter() - started
        result = self.scans[path]
        if isinstance(result, Exception):
            raise result
        return result


def extract(engine, reader, files: list[Path]) -> tuple[dict | None, str]:
    try:
        draft = live_ocr.extract_draft(engine, [(f"whatsapp-media/{p.name}", p) for p in files], read_page=reader.read)
    except ExtractionError as exc:
        return None, exc.code
    return draft, "read"


# ---------------------------------------------------------------- comparison rules (docs/ocr-evaluation.md)

def canonical(field: str, cell: dict):
    """The value the schema would store for a written truth cell."""
    if "canonical" in cell:
        return cell["canonical"]
    text = cell["text"]
    if field in extended.FIELDS:
        return ev.extended_expected(field, text)
    return schema.FIELDS[field].parse(text)


def same(field: str, got, want, accept=()) -> bool:
    if got is None or want is None:
        return False
    spec = extended.FIELDS.get(field) or schema.FIELDS.get(field)
    if spec is not None and spec.kind == "text":
        plain = lambda v: schema.plain(str(v)).replace(" ", "")  # noqa: E731
        return plain(got) == plain(want) or plain(got) in {plain(a) for a in accept}
    if isinstance(want, float) or isinstance(got, float):
        return abs(float(got) - float(want)) < 1e-6
    return got == want


def classify(fv: dict | None, cell: dict, field: str, want=None) -> str:
    state = cell["state"]
    if state in ("value", "choice") and not (state == "choice" and cell["value"] is None):
        if fv is None:
            return "no_field"
        status, value = fv["field_status"], fv["value"]
        if state == "choice" and cell["value"] == "MULTIPLE":
            return "review_right" if status == "NEEDS_REVIEW" else "wrong_known"
        right = same(field, value, want, cell.get("accept", ()))
        if status == "KNOWN":
            return "correct_known" if right else "wrong_known"
        if status == "NEEDS_REVIEW":
            return "review_right" if right else ("review_wrong" if value is not None else "review_empty")
        return "marked_illegible" if status == "ILLEGIBLE" else "claimed_missing"
    if fv is None:
        return "absent_ok"
    status = fv["field_status"]
    if status in ("NOT_PROVIDED", "NOT_APPLICABLE", "UNKNOWN"):
        return "missing_ok"
    if status == "ILLEGIBLE":
        return "blank_marked_illegible"
    return "wrong_known_blank" if status == "KNOWN" else "review_on_blank"


def _record(rows: list, base: dict, field: str, group: str, cell: dict, fv: dict | None, want=None, *, key=False):
    outcome = classify(fv, cell, field, want)
    row = {**base, "field": field, "group": group, "truth_state": cell["state"], "outcome": outcome,
           "status": fv and fv["field_status"], "confidence": fv and fv.get("confidence"),
           "flags": (fv or {}).get("validation_flags", [])}
    if not key:  # clinical values only; patient keys are never written out
        row["expected"] = str(want) if want is not None else cell.get("value")
        row["got"] = None if fv is None else str(fv["value"])
        row["raw_text"] = None if fv is None else fv.get("raw_text")
    rows.append(row)


def _key_cell(cell: dict, field: str, fv: dict | None):
    """Patient keys are compared by hash: (cell with the stored value replaced by the hash, the hash of the read value)."""
    got = None if fv is None or fv["value"] is None else gt.key_hash(field, fv["value"])
    return got, (None if fv is None else {**fv, "value": got})


def score_page(page: dict, draft: dict, base: dict, rows: list, visits: list) -> None:
    by_slot = {e["slot"]: e for e in draft["encounters"] if e["slot"] != "MANUAL"}
    ext = {(section, str(key) if key is not None else None, name): fv
           for section, _i, key, name, fv in extended.iter_fields(draft)}
    if page["layout"] == "current_pregnancy":
        truth_values: dict[tuple, object] = {}
        for slot, cells in page["grid"].items():
            base_slot = {**base, "slot": slot, "in_visit": slot in page["visit_slots"]}
            for row_name, cell in cells.items():
                fields = ("systolic_bp_mmhg", "diastolic_bp_mmhg") if row_name == "bp" else (row_name,)
                parts = cell["text"].split("/") if row_name == "bp" and cell["state"] == "value" else None
                for i, field in enumerate(fields):
                    sub = {"state": "value", "text": parts[i]} if parts else cell
                    want = canonical(field, sub) if sub["state"] == "value" else None
                    truth_values[(slot, field)] = want
                    encounter = by_slot.get(slot)
                    _record(rows, base_slot, field, "grid", sub,
                            encounter["fields"][field] if encounter else None, want)
            for field, cell in page["labs"][slot].items():
                want = canonical(field, cell) if cell["state"] == "value" else None
                _record(rows, base_slot, field, "labs", cell, ext.get(("visit_labs", slot, field)), want)
        cell = page["document_fields"]["last_menstrual_period"]
        _record(rows, base, "last_menstrual_period", "pregnancy_key", cell,
                draft["document_fields"]["last_menstrual_period"],
                canonical("last_menstrual_period", cell) if cell["state"] == "value" else None)
        expected, found = set(page["visit_slots"]), set(by_slot)
        # A value that is not its own cell's but is the same field's value in a neighbouring column.
        shifted = 0
        for slot, encounter in by_slot.items():
            index = live_ocr.COLUMN_SLOTS.index(slot)
            for field in GRID_FIELDS:
                value = encounter["fields"][field]["value"]
                if value is None or same(field, value, truth_values.get((slot, field))):
                    continue
                neighbours = [live_ocr.COLUMN_SLOTS[j] for j in (index - 1, index + 1) if 0 <= j < 9]
                shifted += any(same(field, value, truth_values.get((n, field))) for n in neighbours)
        visits.append({**base, "expected": sorted(expected), "found": sorted(found), "missed": len(expected - found),
                       "extra": len(found - expected), "exact": expected == found, "shifted_values": shifted})
    if page["layout"] == "cover":
        for field, cell in page["keys"].items():
            if cell["state"] == "not_on_layout":
                fv = draft["document_fields"][field]
                outcome_cell = {"state": "blank"}
                _record(rows, base, field, "patient_key", outcome_cell, fv, key=True)
                continue
            got, fv = _key_cell(cell, field, draft["document_fields"][field])
            _record(rows, base, field, "patient_key", {**cell, "text": None}, fv, cell.get("sha256"), key=True)
    for item in page.get("extended", []):
        cell = {k: v for k, v in item.items() if k not in ("section", "item", "field")}
        field = item["field"]
        want = (cell["value"] if cell["state"] == "choice" else canonical(field, cell)) \
            if cell["state"] in ("value", "choice") else None
        key = None if item["item"] is None else str(item["item"])
        _record(rows, base, field, "extended", cell, ext.get((item["section"], key, field)), want)


# ---------------------------------------------------------------- run

def run(args) -> dict:
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    pages = [p for p in truth["pages"] if p["split"] == args.split and (not args.pages or p["page"] in args.pages)]
    environ = {"DAYONE_OCR_MODELS": args.models}
    engine = ocr_engines.create_engine(args.engine, environ=environ)
    missing = engine.missing()
    if missing:
        raise SystemExit(f"OCR engine not ready: missing {', '.join(missing)} (docs/ocr.md)")
    settings = ocr_process.settings_from_env()
    pool = ocr_process.OcrProcessPool(
        ocr_process.EngineSpec("dayone.ocr_engines:create_engine", {"choice": args.engine, "environ": environ}),
        workers=1, page_timeout=settings["page_timeout"], start_timeout=settings["start_timeout"],
        temp_root=Path(tempfile.gettempdir()) / "dayone-ocr-eval-pool")
    started = time.perf_counter()
    pool.warm_up().join()
    load_seconds = time.perf_counter() - started
    rows, visits, page_runs, documents = [], [], [], []
    conditions = args.conditions or CONDITIONS[args.split]
    try:
        with tempfile.TemporaryDirectory(prefix="dayone-ocr-eval-") as tmp:
            folder = Path(tmp)
            for condition in conditions:
                reader = CachingReader(pool)
                chosen = [p for p in pages if condition == "clean" or p["supported"]]
                files = {}
                for page in chosen:
                    path = page_file(page["page"], condition, folder)
                    files[page["page"]] = path
                    draft, result = extract(engine, reader, [path])
                    seconds = reader.seconds[path]
                    base = {"split": args.split, "condition": condition, "page": page["page"],
                            "patient": page["patient"], "layout": page["layout"]}
                    entry = draft["pages"][0] if draft else None
                    page_runs.append({**base, "result": result, "seconds": round(seconds, 2),
                                      "expected_section": page["expected_section"],
                                      "section": entry and entry["section"],
                                      "section_confidence": entry and entry["section_confidence"],
                                      "review_reason": entry and entry["review_reason"],
                                      "warnings": entry and entry.get("warnings"),
                                      "encounters": None if draft is None else
                                      len([e for e in draft["encounters"] if e["slot"] != "MANUAL"]),
                                      "extended_fields": None if draft is None else
                                      sum(1 for _ in extended.iter_fields(draft))})
                    print(f"{condition} page {page['page']:02d}: {result}, {seconds:.1f} s", file=sys.stderr,
                          flush=True)
                    if draft is not None and page["supported"]:
                        score_page(page, draft, base, rows, visits)
                # One document per patient, from the scans already made (as if all its pages came in one message).
                for patient in sorted({p["patient"] for p in chosen}):
                    own = [p for p in chosen if p["patient"] == patient]
                    draft, result = extract(engine, reader, [files[p["page"]] for p in own])
                    doc = {"split": args.split, "condition": condition, "patient": patient, "pages": len(own),
                           "result": result}
                    if draft is not None:
                        grid = next(p for p in own if p["layout"] == "current_pregnancy")
                        found = sorted((e["slot"] for e in draft["encounters"] if e["slot"] != "MANUAL"),
                                       key=live_ocr.COLUMN_SLOTS.index)
                        cover = next(p for p in own if p["layout"] == "cover")
                        keys = {}
                        for field in ("registry_file_number", "facility_name"):
                            fv = draft["document_fields"][field]
                            keys[field] = {"status": fv["field_status"],
                                           "right": fv["value"] is not None
                                           and gt.key_hash(field, fv["value"]) == cover["keys"][field].get("sha256")}
                        ddr = draft["document_fields"]["last_menstrual_period"]
                        want = grid["document_fields"]["last_menstrual_period"]
                        doc.update({"expected_visits": grid["visit_slots"], "found_visits": found,
                                    "encounters_right": found == grid["visit_slots"], "keys": keys,
                                    "ddr": {"status": ddr["field_status"],
                                            "right": same("last_menstrual_period", ddr["value"],
                                                          canonical("last_menstrual_period", want))},
                                    "blocking_fields": sum(1 for e in draft["encounters"] for f in e["fields"].values()
                                                           if f["field_status"] in ("NEEDS_REVIEW", "ILLEGIBLE"))})
                    documents.append(doc)
    finally:
        pool.close()
    return {"split": args.split, "engine": engine.version, "models": args.models,
            "known_threshold": max(live_ocr.MIN_KNOWN_CONFIDENCE, engine.known_confidence),
            "parser": live_ocr.PARSER_VERSION, "code_fingerprint": code_fingerprint(),
            "truth_version": truth["version"], "model_load_seconds": round(load_seconds, 1),
            "page_timeout_seconds": settings["page_timeout"], "conditions": list(conditions),
            "versions": {k: v for k, v in ocr_engines.installed_versions().items() if v},
            "pages": page_runs, "fields": rows, "visits": visits, "documents": documents}


# ---------------------------------------------------------------- report

def _counts(rows, key):
    """Outcome counts; empty cells of columns that are not visits are left out when nothing was made for them
    (the visit-column counts cover those columns)."""
    out: dict = {}
    for row in rows:
        if not row.get("in_visit", True) and row["outcome"] == "absent_ok":
            continue
        bucket = out.setdefault(key(row), {})
        bucket[row["outcome"]] = bucket.get(row["outcome"], 0) + 1
    return out


def _field_table(rows, key, title):
    lines = [f"| {title} | Written | Correct KNOWN | Right value suggested | Wrong/no suggestion | "
             "Claimed blank/illegible | Not found | **Wrong KNOWN** | Blank/dash | Blank kept blank | "
             "Blank sent to review | **Blank filled KNOWN** |", "|---" * 12 + "|"]
    for name, c in sorted(_counts(rows, key).items(), key=lambda kv: str(kv[0])):
        written = sum(c.get(k, 0) for k in WRITTEN)
        empty = sum(c.get(k, 0) for k in EMPTY)
        pct = lambda n, d: f"{n}/{d}" + (f" ({100 * n / d:.0f} %)" if d else "")  # noqa: E731
        lines.append(f"| {name} | {written} | {pct(c.get('correct_known', 0), written)} | "
                     f"{c.get('review_right', 0)} | {c.get('review_wrong', 0) + c.get('review_empty', 0)} | "
                     f"{c.get('claimed_missing', 0) + c.get('marked_illegible', 0)} | {c.get('no_field', 0)} | "
                     f"**{c.get('wrong_known', 0)}** | {empty} | "
                     f"{pct(c.get('missing_ok', 0) + c.get('absent_ok', 0), empty)} | "
                     f"{c.get('review_on_blank', 0) + c.get('blank_marked_illegible', 0)} | "
                     f"**{c.get('wrong_known_blank', 0)}** |")
    return lines


def report(result: dict) -> str:
    split = result["split"]
    lines = [f"# OCR evaluation, {split} split", "",
             f"Engine `{result['engine']}`, parser `{result['parser']}`, KNOWN threshold "
             f"{result['known_threshold']:.2f}, code fingerprint `{result['code_fingerprint']}`, ground truth "
             f"v{result['truth_version']}. Models loaded in {result['model_load_seconds']} s; page timeout "
             f"{result['page_timeout_seconds']:.0f} s.", "",
             "Versions: " + ", ".join(f"{k} {v}" for k, v in result["versions"].items()), ""]
    for condition in result["conditions"]:
        runs = [p for p in result["pages"] if p["condition"] == condition]
        rows = [r for r in result["fields"] if r["condition"] == condition]
        read = [p for p in runs if p["result"] == "read"]
        times = sorted(p["seconds"] for p in runs)
        failures: dict = {}
        for p in runs:
            if p["result"] != "read":
                failures[p["result"]] = failures.get(p["result"], 0) + 1
        lines += [f"## Condition `{condition}`", "",
                  f"- Pages: {len(runs)}; read {len(read)}; not read {len(runs) - len(read)}"
                  + (f" ({', '.join(f'{k} {v}' for k, v in failures.items())})" if failures else ""),
                  f"- Time per page: median {statistics.median(times):.1f} s, 90th percentile "
                  f"{times[int(0.9 * (len(times) - 1))]:.1f} s, max {times[-1]:.1f} s (models already loaded)", ""]
        sections = _counts([{"layout": p["layout"], "outcome": "right" if p["section"] == p["expected_section"]
                             else f"{p['section'] or 'not read'}"} for p in runs], lambda r: r["layout"])
        lines += ["| Layout | Section detected (expected → found) | Review reasons |", "|---|---|---|"]
        for layout, c in sorted(sections.items()):
            reasons = {}
            for p in runs:
                if p["layout"] == layout and p["review_reason"]:
                    reasons[p["review_reason"]] = reasons.get(p["review_reason"], 0) + 1
            lines.append(f"| {layout} | " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) + " | "
                         + (", ".join(f"{k} {v}" for k, v in reasons.items()) or "—") + " |")
        unsupported = [p for p in runs if p["layout"].startswith("postpartum")]
        if unsupported:
            lines += ["", f"- Unsupported post-partum mother pages: {len(unsupported)}; visits created "
                      f"{sum(p['encounters'] or 0 for p in unsupported)}, extended fields created "
                      f"{sum(p['extended_fields'] or 0 for p in unsupported)}."]
        visits = [v for v in result["visits"] if v["condition"] == condition]
        if visits:
            lines += ["", f"- Visit columns: expected {sum(len(v['expected']) for v in visits)}, found "
                      f"{sum(len(v['found']) for v in visits)}, missed {sum(v['missed'] for v in visits)}, extra "
                      f"{sum(v['extra'] for v in visits)}; grid pages with exactly the right columns "
                      f"{sum(v['exact'] for v in visits)}/{len(visits)}; values placed in a neighbouring column "
                      f"{sum(v['shifted_values'] for v in visits)}."]
        docs = [d for d in result["documents"] if d["condition"] == condition]
        if docs:
            ok = [d for d in docs if d["result"] == "read"]
            lines += [f"- Patient documents ({len(docs)}, {docs[0]['pages']} pages each): read {len(ok)}; right "
                      f"encounters {sum(d['encounters_right'] for d in ok)}/{len(ok)}; file number suggested right "
                      f"{sum(d['keys']['registry_file_number']['right'] for d in ok)}/{len(ok)}; facility right "
                      f"{sum(d['keys']['facility_name']['right'] for d in ok)}/{len(ok)}; DDR right "
                      f"{sum(d['ddr']['right'] for d in ok)}/{len(ok)} (KNOWN "
                      f"{sum(d['ddr']['right'] and d['ddr']['status'] == 'KNOWN' for d in ok)}); blocking fields per "
                      f"document: {', '.join(str(d['blocking_fields']) for d in ok)}."]
        lines += ["", "### By field", ""] + _field_table(rows, lambda r: f"{r['group']}: {r['field']}", "Field")
        lines += ["", "### By layout", ""] + _field_table(rows, lambda r: r["layout"], "Layout")
        scored = [r for r in rows if r["confidence"] is not None and r["outcome"] in
                  ("correct_known", "wrong_known", "review_right", "review_wrong")]
        if scored:
            lines += ["", "### OCR score of values read (not calibrated; counts only)", "",
                      "| Score | Right value | Wrong value |", "|---|---|---|"]
            for low, high in ((0, 0.9), (0.9, 0.97), (0.97, 1.01)):
                band = [r for r in scored if low <= r["confidence"] < high]
                right = sum(r["outcome"] in ("correct_known", "review_right") for r in band)
                lines.append(f"| {low:.2f}–{min(high, 1):.2f} | {right} | {len(band) - right} |")
        wrong = [r for r in rows if r["outcome"] in ("wrong_known", "wrong_known_blank")]
        if wrong:
            lines += ["", "### Wrong values marked KNOWN", "", "| Page | Slot | Field | Expected | Read | Score |",
                      "|---|---|---|---|---|---|"]
            lines += [f"| {r['page']} | {r.get('slot', '')} | {r['field']} | {r.get('expected', '(key)')} | "
                      f"{r.get('got', '(key)')} | {r['confidence']} |" for r in wrong]
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=tuple(CONDITIONS), required=True)
    parser.add_argument("--conditions", nargs="+",
                        choices=("clean", "photo_like", "perspective_shadow", "lowres_noise"))
    parser.add_argument("--engine", choices=("paddle", "tesseract"), default="paddle")
    parser.add_argument("--models", choices=tuple(ocr_engines.PADDLE_MODEL_SETS),
                        default=ocr_engines.DEFAULT_PADDLE_MODEL_SET)
    parser.add_argument("--pages", type=int, nargs="+", help="only these pages (smoke test; documents need a cover "
                                                                 "and a grid page of the same patient)")
    parser.add_argument("--report-only", type=Path, help="rewrite the report from a saved results file")
    args = parser.parse_args()
    if args.report_only:
        result = json.loads(args.report_only.read_text(encoding="utf-8"))
    else:
        result = run(args)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"eval-{args.split}-{args.models}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\r\n")
    out = OUT_DIR / f"eval-{result['split']}-{result['models']}.md"
    out.write_text(report(result), encoding="utf-8", newline="\r\n")
    print(f"Written {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
