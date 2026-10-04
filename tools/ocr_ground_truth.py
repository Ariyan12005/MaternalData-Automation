"""Ground truth for the OCR evaluation, built from the synthetic specimen and checked page by page by eye.

    .venv\\Scripts\\python tools/ocr_ground_truth.py build            # writes eval/specimen-ground-truth.json
    .venv\\Scripts\\python tools/ocr_ground_truth.py sheets --pages 3 11  # verification crops, in the temp folder

Candidate values come from the text layer of data/Paper Registry/dossiers_specimen_10_patientes.pdf (the PNG pages
are renders of it). Every candidate was then compared with the rendered page; what the check changed is listed in
CORRECTIONS, and each page records how it was checked. The registry CSV/XLSX is never used: nothing shows that its
rows are the women of these pages.

Patient keys are stored as salted SHA-256 hashes of the normalised value, so that the file holds no file number.
Names, CIN, phone numbers and addresses are never read. Verification sheets show only the cropped lines being
checked, are written to the system temp folder and must be deleted after use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import ocr_specimen_eval as ev  # noqa: E402
from dayone import live_ocr, schema  # noqa: E402

OUT = REPO_ROOT / "eval" / "specimen-ground-truth.json"
KEY_SALT = "dayone-gt-v1"
# Patients 1-5 are for development and tuning; 6-10 are held out and scored only with frozen code.
DEVELOPMENT_PATIENTS = (1, 2, 3, 4, 5)
HELD_OUT_PATIENTS = (6, 7, 8, 9, 10)
# Position of a page in each patient's eight pages -> (layout, section the reader should report, supported).
LAYOUTS = {
    1: ("cover", "cover", True),
    2: ("identification", "identification", True),
    3: ("current_pregnancy", "current_pregnancy", True),
    4: ("delivery", "delivery", True),
    5: ("postpartum_mother_early", "postpartum", False),
    6: ("newborn_early", "newborn", True),
    7: ("postpartum_mother_late", "postpartum", False),
    8: ("newborn_late", "newborn", True),
}
LAB_FIELDS = tuple(ev.LAB_ROWS.values())
# (page, kind, key, field) -> what the visual check changed. "accept" lists other readings that count as right.
_NO_GLYPH = ("The handwriting font of this page has no accented letter: the render shows a gap where a person reads "
             "the accent. The text layer drops it too.")
_POS_PLUS = ("Written 'Pos +' (positive, written short with its sign). The schema parser accepts 'Pos' or '+' but not "
             "both together, so the text cannot be parsed; a person reads it as positive.")
CORRECTIONS: dict[tuple, dict] = {
    (18, "extended", "patient/None", "education_level_text"):
        {"state": "value", "text": "Collège", "accept": ["Coll ge"], "reason": _NO_GLYPH},
    (66, "extended", "patient/None", "education_level_text"):
        {"state": "value", "text": "Collège", "accept": ["Coll ge"], "reason": _NO_GLYPH},
    (58, "extended", "previous_deliveries/1", "previous_delivery_mode_text"):
        {"state": "value", "text": "Césarienne", "accept": ["C sarienne"], "reason": _NO_GLYPH},
    # "canonical" is the stored value a person gives the written text when the schema parser does not accept it.
    (67, "labs", "M8", "albuminuria"):
        {"state": "value", "text": "Pos +", "canonical": "POSITIVE", "reason": _POS_PLUS},
    (67, "labs", "M9", "albuminuria"):
        {"state": "value", "text": "Pos +", "canonical": "POSITIVE", "reason": _POS_PLUS},
}
# Seen during the check, not a change: in some handwriting fonts the 1 has a long flag and looks like a 7 at low
# resolution (file number of page 1, head circumference "31 cm" of page 44); at full size they are 1s.
CHECK = {
    "by": "AI coding assistant (Cursor agent), visual comparison of each candidate with a crop of the rendered page",
    "on": "2026-10-04",
    "human_spot_check": "pending",
}


def key_hash(field: str, value) -> str:
    """Hash of a patient key as the reader would normalise it (digits only; plain text without spaces)."""
    text = schema.plain(str(value)).replace(" ", "") if field == "facility_name" else str(value)
    return hashlib.sha256(f"{KEY_SALT}|{field}|{text}".encode()).hexdigest()


def cell(text: str | None) -> dict:
    if text is None or not text.strip():
        return {"state": "blank"}
    if text.strip() in live_ocr.DASHES:
        return {"state": "dash"}
    return {"state": "value", "text": text.strip()}


def patient_of(page: int) -> int:
    return (page - 1) // 8 + 1


def corrected(page: int, kind: str, key: str, field: str, entry: dict) -> dict:
    fix = CORRECTIONS.get((page, kind, key, field))
    if fix is None:
        return entry
    return {"state": fix["state"], **({"text": fix["text"]} if "text" in fix else {}),
            **({"accept": fix["accept"]} if "accept" in fix else {}),
            **({"canonical": fix["canonical"]} if "canonical" in fix else {}), "correction": fix["reason"]}


def grid_page(page: int) -> dict:
    truth = ev.ground_truth(page)
    labs = ev._lab_truth(*_textpage(page))
    grid = {slot: {field: corrected(page, "grid", slot, field, cell(truth["grid"].get((slot, field))))
                   for field in ev.GRID_ROWS.values()} for slot in live_ocr.COLUMN_SLOTS}
    lab_cells = {slot: {field: corrected(page, "labs", slot, field, cell(labs.get((slot, field))))
                        for field in LAB_FIELDS} for slot in live_ocr.COLUMN_SLOTS}
    # As the reader defines a visit: something other than a dash in one of the visit rows.
    visits = [slot for slot in live_ocr.COLUMN_SLOTS
              if any(grid[slot][f]["state"] in ("value", "illegible") for f in ev.GRID_ROWS.values())]
    return {"visit_slots": visits, "grid": grid, "labs": lab_cells,
            "document_fields": {"last_menstrual_period": corrected(page, "doc", "", "last_menstrual_period",
                                                                   cell(truth.get("ddr")))}}


def _textpage(page: int):
    import pypdfium2 as pdfium

    pdf_page = pdfium.PdfDocument(str(ev.PDF))[page - 1]
    height = pdf_page.get_size()[1]
    textpage = pdf_page.get_textpage()
    return textpage, height, ev._segments(textpage, height)


def cover_page(page: int) -> dict:
    truth = ev.ground_truth(page)
    keys = {}
    for key, field in (("registry", "registry_file_number"), ("facility", "facility_name")):
        entry = cell(truth.get(key))
        if entry["state"] == "value":
            entry = {"state": "value", "sha256": key_hash(field, schema.FIELDS[field].parse(entry.pop("text")))}
        keys[field] = entry
    # The specimen cover has no midwife code line.
    keys["midwife_patient_code"] = {"state": "not_on_layout"}
    return {"keys": keys}


def extended_page(page: int) -> dict:
    items = []
    for (section, key, field), text in sorted(ev.extended_truth(page).items(), key=str):
        if text in ("MARKED", "UNMARKED") or (text and text.isupper() and "_" in text) or text == "MULTIPLE":
            entry = {"state": "choice", "value": text}
        else:
            entry = cell(text)
        entry = corrected(page, "extended", f"{section}/{key}", field, entry)
        items.append({"section": section, "item": key, "field": field, **entry})
    return {"extended": items}


def build() -> dict:
    pages = []
    for page in range(1, 81):
        patient = patient_of(page)
        layout, section, supported = LAYOUTS[(page - 1) % 8 + 1]
        entry = {"page": page, "patient": patient,
                 "split": "development" if patient in DEVELOPMENT_PATIENTS else "held_out",
                 "layout": layout, "expected_section": section, "supported": supported, "checked": CHECK}
        if layout == "cover":
            entry.update(cover_page(page))
        elif layout == "current_pregnancy":
            entry.update(grid_page(page))
            # Lab rows are in entry["labs"], for every column.
            entry["extended"] = [i for i in extended_page(page)["extended"] if i["section"] != "visit_labs"]
        elif supported:
            entry.update(extended_page(page))
        pages.append(entry)
    return {
        "version": 1,
        "description": "Manually checked ground truth for the 80 rendered pages of the synthetic specimen "
                       "(10 fictitious patients x 8 pages). See docs/ocr-evaluation.md.",
        "source": "data/Paper Registry/dossiers_specimen_10_patientes.pdf and its PNG renders",
        "registry_csv_used": False,
        "splits": {"development": list(DEVELOPMENT_PATIENTS), "held_out": list(HELD_OUT_PATIENTS)},
        "key_hash": f"sha256('{KEY_SALT}|<field>|<value>'), value as normalised by the schema; facility names "
                    "lowercased without accents or spaces",
        "states": {"value": "written value", "dash": "a dash (not provided)", "blank": "nothing written",
                   "illegible": "written but unreadable by a person", "choice": "checkbox result",
                   "not_on_layout": "the layout has no such field"},
        "corrections": [{"page": p, "kind": k, "key": key, "field": f, **fix}
                        for (p, k, key, f), fix in sorted(CORRECTIONS.items(), key=str)],
        "pages": pages,
    }


# ---------------------------------------------------------------- verification sheets (temp folder only)

def _font(size):
    from PIL import ImageFont

    for name in ("C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _strip(image, scale, box_pt, caption: str):
    """A crop (PDF points -> pixels) with the candidate value written to its right."""
    from PIL import Image, ImageDraw

    l, t, r, b = (round(v * scale) for v in box_pt)
    crop = image.crop((max(0, l), max(0, t), min(image.width, r), min(image.height, b)))
    out = Image.new("RGB", (crop.width + 620, max(crop.height, 40)), "white")
    out.paste(crop, (0, 0))
    ImageDraw.Draw(out).text((crop.width + 12, 6), caption, fill=(0, 0, 160), font=_font(22))
    return out


def _stack(strips):
    from PIL import Image

    width = max(s.width for s in strips)
    out = Image.new("RGB", (width, sum(s.height + 8 for s in strips)), (230, 230, 230))
    y = 0
    for s in strips:
        out.paste(s, (0, y))
        y += s.height + 8
    return out


def sheets(page_numbers, folder: Path, truth: dict) -> list[Path]:
    from PIL import Image

    by_page = {p["page"]: p for p in truth["pages"]}
    written = []
    for page in page_numbers:
        entry = by_page[page]
        textpage, height, segments = _textpage(page)
        with Image.open(ev.page_png(page)) as source:
            image = source.convert("RGB")
        import pypdfium2 as pdfium

        scale = image.width / pdfium.PdfDocument(str(ev.PDF))[page - 1].get_size()[0]

        def label(text):
            return next((s for s in segments if s[0].startswith(text)), None)

        if entry["layout"] == "current_pregnancy":
            headers = sorted((s for s in segments if s[0] in ev.HEADERS), key=lambda s: s[1])
            rows = sorted((s for s in segments if s[0] in (*ev.GRID_ROWS, *ev.LAB_ROWS)), key=lambda s: s[2])
            strips = [image.crop((0, round((headers[0][2] - 30) * scale), image.width,
                                  round((headers[0][4] + 6) * scale)))]
            for row in rows:
                mid = (row[2] + row[4]) / 2
                strips.append(image.crop((0, round((mid - 10) * scale), image.width, round((mid + 10) * scale))))
            ddr = label("DDR :")
            if ddr:
                stop = label("Taille")
                strips.append(_strip(image, scale, (ddr[1] - 4, ddr[2] - 6, (stop[1] if stop else ddr[3] + 200),
                                                    ddr[4] + 6),
                                     f"DDR: {entry['document_fields']['last_menstrual_period']}"))
            sheet = _stack(strips)
        elif entry["layout"] == "cover":
            strips = []
            for text, field in (("N° de la fiche", "registry_file_number"),
                                ("Nom de l'établissement sanitaire", "facility_name")):
                found = label(text)
                if found:
                    value = ev.ground_truth(page).get("registry" if field == "registry_file_number" else "facility")
                    strips.append(_strip(image, scale, (found[1] - 4, found[2] - 6, found[3] + 260, found[4] + 6),
                                         f"{field}: {value}"))
            sheet = _stack(strips)
        else:
            strips = []
            for item in entry.get("extended", []):
                field = item["field"]
                want = item.get("text", item.get("value", item["state"]))
                spec = next((v for k, v in ev.EXTENDED_LABELS.get(ev.PAGE_TYPES[page % 8], {}).items()
                             if k == field), None)
                boxes = {v: k for k, v in ev.EXTENDED_BOXES.items()}
                if spec:
                    found = label(spec[0])
                    stop = label(spec[1]) if spec[1] else None
                    if found:
                        strips.append(_strip(image, scale, (found[1] - 4, found[2] - 6,
                                                            stop[1] if stop else found[3] + 200, found[4] + 6),
                                             f"{field}: {want}"))
                elif field in boxes or field in ev.BOX_GROUPS:
                    labels = [boxes[field]] if field in boxes else list(ev.BOX_GROUPS[field])
                    for text in labels:
                        found = label(text)
                        if found:
                            strips.append(_strip(image, scale, (found[1] - 24, found[2] - 6, found[3] + 6,
                                                                found[4] + 6), f"{field}: {want} [{text}]"))
            if entry["layout"] == "identification":
                header, avort = label("Nombre"), label("Avortement")
                births = sorted((s for s in segments if s[0].startswith("Accouch. ")), key=lambda s: s[1])
                if header and avort and births:
                    rows = [header, avort, births[0]] + [
                        next(s for s in segments if s[0].startswith(text) and s[2] > births[0][4])
                        for text in ("Date", "Modalité d'extraction")]
                    for row in rows:
                        mid = (row[2] + row[4]) / 2
                        strips.append(image.crop((0, round((mid - 11) * scale), image.width,
                                                  round((mid + 11) * scale))))
                    for item in entry["extended"]:
                        if item["section"] == "previous_deliveries" or item["field"] == "abortions_count":
                            strips.append(_strip(Image.new("RGB", (10, 30), "white"), 1, (0, 0, 10, 30),
                                                 f"{item['section']} {item['item']} {item['field']}: "
                                                 f"{item.get('text', item['state'])}"))
            sheet = _stack(strips) if strips else None
        if sheet is not None:
            path = folder / f"sheet-{page:02d}.png"
            sheet.save(path)
            written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="write eval/specimen-ground-truth.json")
    sheet = sub.add_parser("sheets", help="verification crops in the temp folder")
    sheet.add_argument("--pages", type=int, nargs="+", required=True)
    sheet.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "dayone-gt" / "sheets")
    args = parser.parse_args()
    truth = build()
    if args.command == "build":
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(truth, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\r\n")
        print(f"Written {OUT.relative_to(REPO_ROOT)}: {len(truth['pages'])} pages, "
              f"{len(truth['corrections'])} corrections")
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        for path in sheets(args.pages, args.out, truth):
            print(path.name)
        by_page = {p["page"]: p for p in truth["pages"]}
        show = lambda c: c.get("text", {"blank": ".", "dash": "-"}.get(c["state"], c["state"]))  # noqa: E731
        for page in args.pages:
            entry = by_page[page]
            if entry["layout"] != "current_pregnancy":
                continue
            print(f"page {page}: visits {entry['visit_slots']}")
            for field in (*ev.GRID_ROWS.values(), *LAB_FIELDS):
                cells = entry["grid"] if field in ev.GRID_ROWS.values() else entry["labs"]
                print(f"  {field[:14]:14} " + " | ".join(f"{show(cells[s][field]):>10}" for s in live_ocr.COLUMN_SLOTS))


if __name__ == "__main__":
    main()
