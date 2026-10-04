"""Real OCR on synthetic specimen pages, scored against the text layer of the specimen PDF.

    .venv\\Scripts\\python tools/ocr_specimen_eval.py --models medium
    .venv\\Scripts\\python tools/ocr_specimen_eval.py --variants --pages 3 11 --options default upscale contrast

The specimen PNG pages are renders of data/Paper Registry/dossiers_specimen_10_patientes.pdf, whose handwritten-style
values are real PDF text: that text is the ground truth. The report shows clinical grid values only. Registry
numbers and facility names are reported as correct or not, never printed; names are never read.

Generated copies (photo-like, resized, rotated, degraded) are written under random names to a temporary folder
outside the repository and deleted at the end. Originals are only read. Reports go to var/ocr-eval/.
"""

from __future__ import annotations

import argparse
import glob
import secrets
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dayone import live_ocr, ocr_engines, ocr_preprocess, schema  # noqa: E402
from dayone.extraction import ExtractionError  # noqa: E402

REGISTRY = REPO_ROOT / "data" / "Paper Registry"
PDF = REGISTRY / "dossiers_specimen_10_patientes.pdf"
OUT_DIR = REPO_ROOT / "var" / "ocr-eval"
GRID_ROWS = {"Venue le": "visit_date", "Age probable": "gestational_age_days", "Poids (kg)": "weight_kg",
             "TA": "bp", "HU (cm)": "fundal_height_cm", "Syphilis (TPHA/VDRL)": "syphilis_test",
             "Sérologie VIH": "hiv_test"}
HEADERS = ("Visite 1", "Visite 2", "Visite 3", "7ème mois", "8ème mois", "9ème mois")
# Patient n's 8 pages start at 8(n-1)+1: cover, identification, current pregnancy, …
# Two covers and the pregnancy grid of each of the ten fictitious patients.
DEFAULT_PAGES = (1, 3, 9, 11, 19, 27, 35, 43, 51, 59, 67, 75)
OPTION_SETS = {
    "default": ocr_preprocess.DEFAULT_OPTIONS,
    "noscale": ocr_preprocess.Options(max_long_side=0),
    "upscale": ocr_preprocess.Options(min_long_side=2400),
    "contrast": ocr_preprocess.Options(contrast=True),
    "noorient": ocr_preprocess.Options(orientation=False),
}


def page_png(number: int) -> Path:
    return Path(sorted(glob.glob(str(REGISTRY / f"dossiers_specimen_10_patientes-{number:02d}*.png")))[0])


def _segments(textpage, height):
    out = []
    for i in range(textpage.count_rects()):
        l, b, r, t = textpage.get_rect(i)
        text = textpage.get_text_bounded(l, b, r, t).strip()
        if text:
            out.append((text, l, height - t, r, height - b))
    return out


def _chars(textpage, height):
    for i in range(textpage.count_chars()):
        char = textpage.get_text_range(i, 1)
        if char in "\r\n":
            continue
        l, b, r, t = textpage.get_charbox(i)
        yield char, (l + r) / 2, height - (b + t) / 2


def ground_truth(number: int) -> dict:
    """Cell strings from the PDF text layer: {'grid': {(slot, field): text}, 'ddr', 'registry', 'facility'}."""
    import pypdfium2 as pdfium

    page = pdfium.PdfDocument(str(PDF))[number - 1]
    height = page.get_size()[1]
    textpage = page.get_textpage()
    segments = _segments(textpage, height)
    truth: dict = {"grid": {}}

    def label(text):
        return next((s for s in segments if s[0].startswith(text)), None)

    headers = sorted((s for s in segments if s[0] in HEADERS), key=lambda s: s[1])
    if len(headers) == 9:
        lefts = [s[1] - 4 for s in headers] + [headers[-1][1] + 41]
        centres = {field: (label(text)[2] + label(text)[4]) / 2 for text, field in GRID_ROWS.items() if label(text)}
        for char, x, y in _chars(textpage, height):
            column = next((i for i in range(9) if lefts[i] <= x < lefts[i + 1]), None)
            row = min(centres, key=lambda f: abs(y - centres[f]))
            if column is not None and abs(y - centres[row]) < 8:
                key = (live_ocr.COLUMN_SLOTS[column], row)
                truth["grid"][key] = truth["grid"].get(key, "") + char
        truth["grid"] = {k: v.strip() for k, v in truth["grid"].items()}

    for key, text, stop in (("ddr", "DDR :", "Taille"), ("registry", "N° de la fiche", None),
                            ("facility", "Nom de l'établissement sanitaire", None)):
        found = label(text)
        if not found:
            continue
        end = label(stop)[1] if stop and label(stop) else 10_000
        truth[key] = "".join(c for c, x, y in _chars(textpage, height)
                             if found[3] + 1 < x < end and abs(y - (found[2] + found[4]) / 2) < 8).strip()
    return truth


# Extended fields (dayone/extended.py): page type -> field -> (printed label, label that ends the value or None).
EXTENDED_LABELS = {
    "identification": {"maternal_age_years": ("Age :", "CIN :"),
                       "education_level_text": ("Niveau d'instruction :", "Profession :"),
                       "gravidity": ("Gestation :", "Parité :"), "parity": ("Parité :", "Nombre d'enfants vivants :"),
                       "living_children_count": ("Nombre d'enfants vivants :", None)},
    "current_pregnancy": {"height_cm": ("Taille :", "Groupage :")},
    "delivery": {"delivery_date": ("Date de l'accouchement :", None),
                 "delivery_gestational_age_days": ("Âge gestationnel :", None), "newborn_sex": ("Sexe :", None),
                 "birth_weight_g": ("Poids à la naissance :", None),
                 "birth_head_circumference_cm": ("Périmètre crânien à la naissance :", None)},
    "newborn": {"consultation_date": ("Date de la consultation :", None)},
}
LAB_ROWS = {"Hémoglobine": "hemoglobin_g_dl", "Bilan glycémique": "blood_glucose_g_l", "Albuminurie": "albuminuria"}
EXTENDED_BOXES = {"Consanguinité": "consanguinity_mark", "Grossesse désirée": "pregnancy_desired_mark"}
BOX_GROUPS = {"delivery_mode": {"Voie basse non instrumentale": "VAGINAL_NON_INSTRUMENTAL",
                                "Voie basse instrumentale": "VAGINAL_INSTRUMENTAL",
                                "Césarienne : Programmée": "CESAREAN_PLANNED", "Urgence": "CESAREAN_EMERGENCY"},
              "feeding_mode": {"exclusivement au sein": "EXCLUSIVE_BREASTFEEDING", "Artificiel": "ARTIFICIAL",
                               "mixte": "MIXED"}}
# Patient n's pages: 8(n-1) + 2 identification, 3 current pregnancy, 4 delivery, 6 and 8 newborn consultations.
EXTENDED_PAGES = tuple(8 * (n - 1) + k for n in range(1, 11) for k in (2, 3, 4, 6, 8))
PAGE_TYPES = {2: "identification", 3: "current_pregnancy", 4: "delivery", 6: "newborn", 0: "newborn"}


def _checkboxes(page, height, segments) -> dict[str, bool]:
    """{label: marked} from the PDF drawing: 9.5 pt squares in the form's colour, and small strokes in another
    colour (crosses and ticks, in several ink colours) whose centre is in one."""
    import ctypes

    import pypdfium2.raw as raw

    def bounds(obj):
        l, b, r, t = (ctypes.c_float() for _ in range(4))
        raw.FPDFPageObj_GetBounds(obj.raw, l, b, r, t)
        return l.value, height - t.value, r.value, height - b.value

    def stroke(obj):
        r, g, b, a = (ctypes.c_uint() for _ in range(4))
        raw.FPDFPageObj_GetStrokeColor(obj.raw, r, g, b, a)
        return r.value, g.value, b.value

    squares, marks = [], []
    for obj in page.get_objects():
        if obj.type != raw.FPDF_PAGEOBJ_PATH:
            continue
        l, t, r, b = bounds(obj)
        colour = stroke(obj)
        if colour not in ((31, 20, 26), (0, 0, 0)) and r - l < 20 and b - t < 20:
            marks.append(((l + r) / 2, (t + b) / 2))
        elif colour == (31, 20, 26) and raw.FPDFPath_CountSegments(obj.raw) == 5 and 8 < r - l < 11 and 8 < b - t < 11:
            squares.append((l, t, r, b))
    out = {}
    for l, t, r, b in squares:
        right = sorted((s for s in segments if s[1] >= r - 1 and s[1] - r < 40 and abs((s[2] + s[4]) / 2 - (t + b) / 2) < 6
                        and len(s[0]) > 1), key=lambda s: s[1])
        if right:
            out[right[0][0]] = any(l - 3 <= x <= r + 3 and t - 3 <= y <= b + 3 for x, y in marks)
    return out


def extended_truth(number: int) -> dict:
    """{(section, item key, field): text, or a choice value for boxes; None for blank} from the PDF of one page."""
    import pypdfium2 as pdfium

    from dayone import extended

    page = pdfium.PdfDocument(str(PDF))[number - 1]
    height = page.get_size()[1]
    textpage = page.get_textpage()
    segments = _segments(textpage, height)
    chars = list(_chars(textpage, height))
    kind = PAGE_TYPES[number % 8]

    def label(text):
        return next((s for s in segments if s[0].startswith(text)), None)

    def text_in(x0, x1, cy):
        return "".join(c for c, x, y in chars if x0 < x < x1 and abs(y - cy) < 8).strip() or None

    truth: dict = {}
    key_of = {"newborns": 1}
    if kind == "newborn":
        title = " ".join(s[0] for s in segments[:3])
        key_of["newborn_consultations"] = "EARLY" if "PRÉCOCE" in title else "LATE" if "TARDIF" in title else None
    for field, (start, stop) in EXTENDED_LABELS[kind].items():
        found = label(start)
        if found:
            end = label(stop)[1] if stop and label(stop) else 10_000
            section = extended.SECTION_OF[field]
            truth[(section, key_of.get(section), field)] = text_in(found[3] + 1, end, (found[2] + found[4]) / 2)
    boxes = _checkboxes(page, height, segments)
    for text, field in EXTENDED_BOXES.items():
        if kind == "identification" and text in boxes:
            truth[("pregnancy", None, field)] = "MARKED" if boxes[text] else "UNMARKED"
    for field, options in BOX_GROUPS.items():
        if all(text in boxes for text in options):
            marked = [value for text, value in options.items() if boxes[text]]
            section = extended.SECTION_OF[field]
            truth[(section, key_of.get(section), field)] = marked[0] if len(marked) == 1 else (
                None if not marked else "MULTIPLE")
    if kind == "identification":
        header, row = label("Nombre"), label("Avortement")
        following = [s for s in segments if s[1] > header[3] and abs(s[2] - header[2]) < 4]
        right = min(s[1] for s in following) - 2
        truth[("pregnancy", None, "abortions_count")] = text_in(header[1] - 10, right, (row[2] + row[4]) / 2)
        headers = sorted((s for s in segments if s[0].startswith("Accouch. ")), key=lambda s: s[1])

        def row_label(text):
            return next(s for s in segments if s[0].startswith(text) and s[2] > headers[0][4])

        rows = {"previous_delivery_date": row_label("Date"),
                "previous_delivery_mode_text": row_label("Modalité d'extraction")}
        others = [row_label(t) for t in ("Si césarienne", "Complication (type)", "Poids nouveau-né", "Compl. nouveau-né")]
        for n, header_seg in enumerate(headers, start=1):
            x0, x1 = header_seg[1] - 4, header_seg[1] + (headers[1][1] - headers[0][1]) - 4
            cells = {name: text_in(x0, x1, (seg[2] + seg[4]) / 2) for name, seg in rows.items()}
            if any(cells.values()) or any(text_in(x0, x1, (s[2] + s[4]) / 2) for s in others if s):
                for name, text in cells.items():
                    truth[("previous_deliveries", n, name)] = text
    if kind == "current_pregnancy":
        grid = ground_truth(number)["grid"]
        visits = {slot for (slot, field), text in grid.items()
                  if field in live_ocr.VISIT_EVIDENCE and text not in live_ocr.DASHES}
        lab_cells = _lab_truth(textpage, height, segments)
        for slot in live_ocr.COLUMN_SLOTS:
            if slot in visits or any(lab_cells.get((slot, f)) for f in LAB_ROWS.values()):
                for field in LAB_ROWS.values():
                    truth[("visit_labs", slot, field)] = lab_cells.get((slot, field))
    return truth


def _lab_truth(textpage, height, segments) -> dict:
    headers = sorted((s for s in segments if s[0] in HEADERS), key=lambda s: s[1])
    if len(headers) != 9:
        return {}
    lefts = [s[1] - 4 for s in headers] + [headers[-1][1] + 41]
    centres = {field: (s[2] + s[4]) / 2 for text, field in LAB_ROWS.items()
               for s in segments if s[0] == text}
    cells: dict = {}
    for char, x, y in _chars(textpage, height):
        column = next((i for i in range(9) if lefts[i] <= x < lefts[i + 1]), None)
        row = min(centres, key=lambda f: abs(y - centres[f]))
        if column is not None and abs(y - centres[row]) < 8:
            key = (live_ocr.COLUMN_SLOTS[column], row)
            cells[key] = cells.get(key, "") + char
    return {k: v.strip() for k, v in cells.items()}


def extended_expected(field: str, text: str | None):
    from dayone import extended

    if text is None or text in live_ocr.DASHES:
        return None
    if text == "MULTIPLE":
        return "MULTIPLE"
    spec = extended.FIELDS[field]
    if spec.kind == "choice" and text in [value for value, _s in spec.choices]:
        return text
    # No range check: a fictitious date after today is still what the page says.
    return schema._PARSERS[spec.kind](spec, text)


def score_extended(draft: dict, truth: dict, label: str, details: list, tally: dict) -> None:
    """tally: {field: {outcome: count}}. Outcomes as for grid fields; a value shown on paper but absent from the
    draft is "missed (no field)"."""
    from dayone import extended

    found = {(section, key, name): fv for section, _i, key, name, fv in extended.iter_fields(draft)}
    for (section, key, name), text in sorted(truth.items(), key=str):
        try:
            want = extended_expected(name, text)
        except schema.InvalidValue:
            want = f"unparsed {text!r}"
        fv = found.get((section, key, name))
        if fv is None:
            result = "ok (blank)" if want is None else "missed (no field)"
        elif want == "MULTIPLE":
            result = "review (several boxes on paper)" if fv["field_status"] == "NEEDS_REVIEW" else "WRONG KNOWN"
        elif extended.FIELDS[name].kind == "text" and isinstance(want, str) and fv["value"] is not None:
            # Free text is compared without case or accents (the PDF text layer drops some accented letters).
            same = schema.plain(fv["value"]).replace(" ", "") == schema.plain(want).replace(" ", "")
            result = outcome({**fv, "value": want if same else fv["value"]}, want)
        else:
            result = outcome(fv, want)
        tally.setdefault(name, {})
        tally[name][result] = tally[name].get(result, 0) + 1
        if not result.startswith(("correct", "ok")):
            fv = fv or {}
            details.append(f"| {label} | {section} {key or ''} | {name} | {want} | {fv.get('raw_text')} | "
                           f"{fv.get('confidence')} | {fv.get('field_status')} | "
                           f"{', '.join(fv.get('validation_flags', []))} | {result} |")


def expected(field: str, text: str | None):
    """Normalised value expected from a ground-truth string; None for a blank cell or a dash."""
    if not text or text in live_ocr.DASHES:
        return None
    if field == "bp":
        systolic, diastolic = text.split("/")
        return (schema.FIELDS["systolic_bp_mmhg"].parse(systolic), schema.FIELDS["diastolic_bp_mmhg"].parse(diastolic))
    return schema.FIELDS[field].parse(text)


def outcome(fv: dict | None, want) -> str:
    if fv is None:
        return "visit missed" if want is not None else "ok (blank)"
    status, value = fv["field_status"], fv["value"]
    if want is None:
        if status in schema.MISSING_STATUSES:
            return "ok (blank)"
        return "WRONG KNOWN" if status == "KNOWN" else "review (blank on paper)"
    if status == "KNOWN":
        return "correct KNOWN" if value == want else "WRONG KNOWN"
    if status == "NEEDS_REVIEW":
        return "review, value right" if value == want else "review"
    return "missed (blank claimed)"


def score(draft: dict, truth: dict, label: str, details: list, known_confidence: dict) -> dict:
    found = {e["slot"]: e for e in draft["encounters"] if e["slot"] != "MANUAL"}
    tally: dict[str, int] = {}
    if truth["grid"]:
        for slot in live_ocr.COLUMN_SLOTS:
            for field in GRID_ROWS.values():
                want = expected(field, truth["grid"].get((slot, field)))
                encounter = found.get(slot)
                pairs = ([("systolic_bp_mmhg", want and want[0]), ("diastolic_bp_mmhg", want and want[1])]
                         if field == "bp" else [(field, want)])
                for name, value in pairs:
                    fv = encounter["fields"][name] if encounter else None
                    result = outcome(fv, value)
                    tally[result] = tally.get(result, 0) + 1
                    if result.endswith("KNOWN"):
                        known_confidence.setdefault(result, []).append(fv["confidence"])
                    if not result.startswith(("correct", "ok")):
                        details.append(f"| {label} | {slot} | {name} | {value} | "
                                       f"{(fv or {}).get('raw_text')} | {(fv or {}).get('confidence')} | "
                                       f"{(fv or {}).get('field_status')} | "
                                       f"{', '.join((fv or {}).get('validation_flags', []))} | {result} |")
    docs = draft["document_fields"]
    for key, name in (("ddr", "last_menstrual_period"), ("registry", "registry_file_number"),
                      ("facility", "facility_name")):
        if key in truth:
            try:
                want = schema.FIELDS[name].parse(truth[key])
            except schema.InvalidValue:
                continue
            result = outcome(docs[name], want)
            tally[result] = tally.get(result, 0) + 1
    return tally


def _engine(args):
    engine = (ocr_engines.PaddleEngine(ocr_engines.models_dir_from_env(), model_set=args.models)
              if args.engine == "paddle" else ocr_engines.create_engine("tesseract"))
    if args.min_confidence is not None:
        engine.known_confidence = args.min_confidence
    return engine


def _save(image, folder: Path, *, exif_orientation: int | None = None, quality: int = 90) -> Path:
    """Saves a generated copy under a random WhatsApp-like name, so nothing can be inferred from the name."""
    target = folder / f"{secrets.token_hex(32)}.jpg"
    if exif_orientation:
        exif = image.getexif()
        exif[0x0112] = exif_orientation
        image.save(target, quality=quality, exif=exif)
    else:
        image.save(target, quality=quality)
    return target


def variants(number: int, folder: Path) -> list[tuple[str, Path]]:
    """Copies of one specimen page: renamed, resized, rotated, EXIF-rotated, skewed and degraded."""
    from PIL import Image, ImageEnhance, ImageFilter

    with Image.open(page_png(number)) as source:
        page = source.convert("RGB")
    paper = (235, 225, 225)
    out = [("renamed copy", _save(page, folder))]
    for factor in (0.5, 0.75, 1.6):
        out.append((f"resized x{factor}", _save(page.resize((round(page.width * factor), round(page.height * factor)),
                                                            Image.LANCZOS), folder)))
    for angle in (90, 180, 270):
        out.append((f"rotated {angle}", _save(page.rotate(angle, expand=True), folder)))
    # Pixels stored sideways with an EXIF tag saying how to show them, as many phones do (6 = rotate 90 cw).
    out.append(("EXIF orientation 6", _save(page.rotate(90, expand=True), folder, exif_orientation=6)))
    for angle in (3, 6):
        out.append((f"skewed {angle} deg", _save(page.rotate(angle, expand=True, fillcolor=paper), folder)))
    for radius in (1.5, 2.5, 4):
        out.append((f"blur r{radius}", _save(page.filter(ImageFilter.GaussianBlur(radius)), folder)))
    out.append(("dark x0.45", _save(ImageEnhance.Brightness(page).enhance(0.45), folder)))
    out.append(("very dark x0.12", _save(ImageEnhance.Brightness(page).enhance(0.12), folder)))
    out.append(("low contrast x0.35", _save(ImageEnhance.Contrast(page).enhance(0.35), folder)))
    out.append(("faded x0.12", _save(ImageEnhance.Contrast(page).enhance(0.12), folder)))
    out.append(("JPEG q15", _save(page, folder, quality=15)))
    photo = page.rotate(1.5, expand=True, fillcolor=paper)
    photo = ImageEnhance.Brightness(photo.filter(ImageFilter.GaussianBlur(1.0))).enhance(0.9)
    photo.thumbnail((1280, 1280))
    out.append(("photo-like", _save(photo, folder, quality=60)))
    return out


def degraded_copy(number: int, folder: Path) -> Path:
    """A phone-photo-like copy (rotation, blur, JPEG, downscale); the original is untouched."""
    return dict(variants(number, folder))["photo-like"]


def _read(engine, path: Path, options) -> tuple[dict | None, str, float]:
    reader = live_ocr.InProcessReader(engine, options)
    started = time.perf_counter()
    try:
        draft = live_ocr.extract_draft(engine, [(f"whatsapp-media/{path.name}", path)], read_page=reader.read)
        result = "read"
    except ExtractionError as exc:
        draft, result = None, f"{exc.code} ({', '.join(p['reason'] for p in exc.pages)})"
    return draft, result, time.perf_counter() - started


def _metrics(path: Path) -> str:
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        m = ocr_preprocess.photo_metrics(ImageOps.exif_transpose(image).convert("RGB"))
    return f"mean {m['mean']:.0f}, contrast {m['contrast']}, sharpness {m['sharpness']:.0f}"


def run_variants(args) -> str:
    engine = _engine(args)
    missing = engine.missing()
    if missing:
        return f"Engine {args.engine} not ready: missing {', '.join(missing)} (docs/ocr.md)."
    lines = [f"# Preprocessing evaluation on generated copies: {engine.version}", "",
             f"KNOWN threshold {max(live_ocr.MIN_KNOWN_CONFIDENCE, engine.known_confidence):.2f}. "
             "Columns: correct KNOWN / WRONG KNOWN / review with the right value / other review / blank claimed "
             "for a written cell / visit columns missed / extra.", ""]
    with tempfile.TemporaryDirectory(prefix="dayone-ocr-eval-") as tmp:
        folder = Path(tmp)
        for number in args.pages:
            truth = ground_truth(number)
            expected_slots = {slot for (slot, field), text in truth["grid"].items()
                              if field in live_ocr.VISIT_EVIDENCE and text not in live_ocr.DASHES}
            lines += [f"## Page -{number:02d}", "",
                      "| Copy | Photo metrics | " + " | ".join(args.options) + " |",
                      "|---|---|" + "---|" * len(args.options)]
            for label, path in variants(number, folder):
                if args.only and not any(word in label for word in args.only):
                    continue
                cells = []
                for option in args.options:
                    draft, result, seconds = _read(engine, path, OPTION_SETS[option])
                    if draft is None:
                        cells.append(f"{result}, {seconds:.0f} s")
                        continue
                    tally = score(draft, truth, label, [], {})
                    found = {e["slot"] for e in draft["encounters"] if e["slot"] != "MANUAL"}
                    page = draft["pages"][0]
                    cells.append(
                        f"{tally.get('correct KNOWN', 0)}/{tally.get('WRONG KNOWN', 0)}/"
                        f"{tally.get('review, value right', 0)}/{tally.get('review', 0)}/"
                        f"{tally.get('missed (blank claimed)', 0)}/{len(expected_slots - found)}/"
                        f"{len(found - expected_slots)} `{page['section']}`"
                        f"{' rot ' + str(page.get('image', {}).get('rotated_ccw')) if page.get('image') else ''}"
                        f"{' ' + page['review_reason'] if page.get('review_reason') else ''}, {seconds:.0f} s")
                lines.append(f"| {label} | {_metrics(path)} | " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines) + "\n"


def run(args) -> str:
    engine = _engine(args)
    missing = engine.missing()
    if missing:
        return f"Engine {args.engine} not ready: missing {', '.join(missing)} (docs/ocr.md)."

    lines = [f"# Real OCR on synthetic specimen pages: {engine.version}", "",
             "Versions: " + ", ".join(f"{k} {v}" for k, v in ocr_engines.installed_versions().items() if v), "",
             f"KNOWN threshold: OCR confidence >= {max(live_ocr.MIN_KNOWN_CONFIDENCE, engine.known_confidence):.2f}",
             ""]
    tally: dict[str, int] = {}
    details = []
    timing = []
    known_confidence: dict[str, list[float]] = {}
    with tempfile.TemporaryDirectory(prefix="dayone-ocr-eval-") as tmp:
        pages = [(f"-{n:02d}", page_png(n), n) for n in args.pages]
        pages += [(f"-{n:02d} photo-like", degraded_copy(n, Path(tmp)), n) for n in args.photo_like]
        for label, path, number in pages:
            truth = ground_truth(number)
            draft, result, seconds = _read(engine, path, ocr_preprocess.DEFAULT_OPTIONS)
            timing.append(seconds)
            if draft is None:
                lines += [f"## Page {label}: not read ({result}), {seconds:.1f} s", ""]
                continue
            page = draft["pages"][0]
            found = {e["slot"]: e for e in draft["encounters"] if e["slot"] != "MANUAL"}
            visit_slots = sorted({slot for (slot, field), text in truth["grid"].items()
                                  if field in live_ocr.VISIT_EVIDENCE and text not in live_ocr.DASHES})
            page_tally = score(draft, truth, label, details, known_confidence)
            for key, count in page_tally.items():
                tally[key] = tally.get(key, 0) + count
            lines.append(f"## Page {label}: section `{page['section']}` ({page['section_confidence']}), {seconds:.1f} s")
            lines.append(f"- Visit columns expected {visit_slots}, found {sorted(found)}")
            lines.append("- PII labels seen (not extracted): "
                         + (", ".join(sorted({p['category'] for p in draft['pii_detected']})) or "none"))
            lines.append("- Fields: " + ", ".join(f"{k} {v}" for k, v in sorted(page_tally.items())))
            lines.append("")
    lines += ["## Overall", "", "| Outcome | Fields |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(tally.items())]
    lines += ["", f"Time per page: {min(timing):.1f}–{max(timing):.1f} s (the first page includes model loading).", ""]
    for result, values in sorted(known_confidence.items()):
        lines.append(f"- OCR confidence of {result} grid values: min {min(values):.3f}, "
                     f"median {statistics.median(values):.3f}, n={len(values)}")
    lines.append("")
    if details:
        lines += ["## Fields not read correctly (clinical values only)", "",
                  "| Page | Slot | Field | Expected | OCR text | Confidence | Status | Flags | Outcome |",
                  "|---|---|---|---|---|---|---|---|---|"]
        lines += details
    return "\n".join(lines) + "\n"


def _box_inks(draft: dict, truth: dict, inks: dict) -> None:
    """Ink measured in each located checkbox, filed under what the PDF shows: inks {"marked": [...], "empty": [...]}."""
    from dayone import extended, ocr_extended

    option_of = {label: value for options in ocr_extended.CHOICE_BOXES.values() for value, _p, label in options}
    for section, _i, key, name, fv in extended.iter_fields(draft):
        want = truth.get((section, key, name))
        for box in (fv.get("source") or {}).get("checkboxes", []):
            if box["ink"] is None or want in (None, "MULTIPLE"):
                continue
            marked = want == "MARKED" if name in ocr_extended.CHECKBOXES else option_of.get(box["label"]) == want
            inks["marked" if marked else "empty"].append(box["ink"])


def run_extended(args) -> str:
    """Extended fields (dayone/extended.py) on the specimen page types that carry them, against the PDF text layer
    and checkbox drawings. Only clinical values are printed; names, CIN and phone numbers are never read here."""
    from dayone import ocr_extended

    engine = _engine(args)
    missing = engine.missing()
    if missing:
        return f"Engine {args.engine} not ready: missing {', '.join(missing)} (docs/ocr.md)."
    lines = [f"# Extended fields, real OCR on synthetic specimen pages: {engine.version}", "",
             "Versions: " + ", ".join(f"{k} {v}" for k, v in ocr_engines.installed_versions().items() if v), "",
             f"KNOWN threshold: OCR confidence >= {max(live_ocr.MIN_KNOWN_CONFIDENCE, engine.known_confidence):.2f}; "
             f"checkboxes: ink share >= {ocr_extended.MARKED_INK} marked, <= {ocr_extended.UNMARKED_INK} empty, "
             "otherwise review.", ""]
    tally: dict[str, dict[str, int]] = {}
    details: list[str] = []
    inks = {"marked": [], "empty": []}
    timing = []
    with tempfile.TemporaryDirectory(prefix="dayone-ocr-eval-") as tmp:
        pages = [(f"-{n:02d}", page_png(n), n) for n in args.pages]
        pages += [(f"-{n:02d} photo-like", degraded_copy(n, Path(tmp)), n) for n in args.photo_like]
        for label, path, number in pages:
            draft, result, seconds = _read(engine, path, ocr_preprocess.DEFAULT_OPTIONS)
            timing.append(seconds)
            print(f"page {label}: {result}, {seconds:.1f} s", file=sys.stderr, flush=True)
            if draft is None:
                lines += [f"- Page {label}: not read ({result}), {seconds:.1f} s"]
                continue
            truth = extended_truth(number)
            page_tally: dict[str, dict[str, int]] = {}
            score_extended(draft, truth, label, details, page_tally)
            _box_inks(draft, truth, inks)
            for name, outcomes in page_tally.items():
                for key, count in outcomes.items():
                    tally.setdefault(name, {})
                    tally[name][key] = tally[name].get(key, 0) + count
            page = draft["pages"][0]
            counts: dict[str, int] = {}
            for outcomes in page_tally.values():
                for key, count in outcomes.items():
                    counts[key] = counts.get(key, 0) + count
            lines.append(f"- Page {label} ({PAGE_TYPES[number % 8]}): section `{page['section']}`, {seconds:.1f} s; "
                         + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    outcomes = ("correct KNOWN", "WRONG KNOWN", "review, value right", "review", "ok (blank)",
                "missed (blank claimed)", "missed (no field)", "review (several boxes on paper)")
    lines += ["", "## Per field", "", "| Field | " + " | ".join(outcomes) + " | other |",
              "|---|" + "---|" * (len(outcomes) + 1)]
    from dayone import extended

    for name in sorted(tally, key=list(extended.FIELDS).index):
        row = tally[name]
        other = sum(v for k, v in row.items() if k not in outcomes)
        lines.append(f"| `{name}` | " + " | ".join(str(row.get(k, 0)) for k in outcomes) + f" | {other} |")
    lines += ["", "## Checkbox ink (share of dark pixels inside the located square)", ""]
    for kind, values in inks.items():
        if values:
            lines.append(f"- Boxes {kind} on the PDF: n={len(values)}, min {min(values):.3f}, "
                         f"median {statistics.median(values):.3f}, max {max(values):.3f}")
    lines += ["", f"Time per page: {min(timing):.1f}–{max(timing):.1f} s (the first page includes model loading).", ""]
    if details:
        lines += ["## Fields not read as correct KNOWN (clinical values only)", "",
                  "| Page | Item | Field | Expected | OCR text | Confidence | Status | Flags | Outcome |",
                  "|---|---|---|---|---|---|---|---|---|"]
        lines += details
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=("paddle", "tesseract"), default="paddle")
    parser.add_argument("--models", choices=tuple(ocr_engines.PADDLE_MODEL_SETS),
                        default=ocr_engines.DEFAULT_PADDLE_MODEL_SET, help="PaddleOCR model set")
    parser.add_argument("--min-confidence", type=float, help="override the model set's KNOWN threshold")
    parser.add_argument("--pages", type=int, nargs="+", default=None)
    parser.add_argument("--photo-like", type=int, nargs="*", default=None,
                        help="pages also tested as degraded copies (default 3 11; with --extended 2 4 6)")
    parser.add_argument("--variants", action="store_true", help="evaluate preprocessing on generated copies")
    parser.add_argument("--extended", action="store_true",
                        help="score the extended fields (docs/excel-field-mapping.md) on pages 8(n-1)+{2,3,4,6,8}")
    parser.add_argument("--options", nargs="+", choices=tuple(OPTION_SETS), default=["default"])
    parser.add_argument("--only", nargs="*", help="variants whose label contains one of these words")
    parser.add_argument("--no-photo-checks", action="store_true",
                        help="OCR every copy, to see where reading breaks down (calibration of the photo checks)")
    args = parser.parse_args()
    if args.no_photo_checks:
        ocr_preprocess.photo_problem = lambda image: None
        ocr_preprocess.MIN_TEXT_LINES = 0
    if args.photo_like is None:
        args.photo_like = [2, 4, 6] if args.extended else [3, 11]
    if args.extended:
        args.pages = args.pages or list(EXTENDED_PAGES)
        report = run_extended(args)
        name = f"report-extended-{args.engine}-{args.models}"
    else:
        args.pages = args.pages or ([3, 11] if args.variants else list(DEFAULT_PAGES))
        report = run_variants(args) if args.variants else run(args)
        name = f"variants-{args.engine}-{args.models}-{'-'.join(args.options)}" if args.variants else \
            f"report-{args.engine}-{args.models}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{name}.md"
    out.write_text(report, encoding="utf-8", newline="\r\n")
    print(report)
    print(f"Written to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
