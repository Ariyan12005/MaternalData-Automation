"""Reads the extended paper values (dayone/extended.py) from pages already located by live_ocr.build_draft.

Each value comes from one printed label, table cell or checkbox of one page type, and keeps its raw text, status,
flags and source. Nothing is computed across pages or visits; several photos of the same page are merged with
live_ocr.merge_readings, so a disagreement goes to a person. Checkboxes are read on the page image: the printed
square next to the label is located, then the ink inside it is measured. A box that cannot be located, or whose ink
is between the two thresholds, goes to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import extended
from .live_ocr import (
    DASHES, EXTRACT, LAB_ROWS, OcrLine, _cell_rect, _field, _Page, _read_value, _review, _value_beside, assign_cells,
    find_grid, merge_readings, norm,
)

SPECS = extended.FIELDS
# (number pattern, unit suffix) for live_ocr._read_value when the kind's default does not fit.
NUMBER = {
    "maternal_age_years": (r"\d{2}", r"(?:ans?)?"),
    "gravidity": (r"\d{1,2}", ""),
    "parity": (r"\d{1,2}", ""),
    "living_children_count": (r"\d{1,2}", ""),
    "abortions_count": (r"\d{1,2}", ""),
    "height_cm": (r"\d{3}", r"(?:cm)?"),
    "hemoglobin_g_dl": (r"\d{1,2}(?:\.\d{1,2})?", r"(?:g\s*/\s*dl)?"),
    "blood_glucose_g_l": (r"\d(?:\.\d{1,2})?", r"(?:g\s*/\s*l)?"),
    "birth_weight_g": (r"\d{3,4}", r"(?:gr?)?"),
    "birth_head_circumference_cm": (r"\d{2}(?:\.\d)?", r"(?:cm)?"),
}
# Values written after a printed label, per page section: field -> (label pattern on norm() text, printed label).
LABELLED = {
    "identification": {
        "maternal_age_years": (r"^age\s*:", "Age"),
        "education_level_text": (r"^niveau\s*d.?\s*instruction", "Niveau d'instruction"),
        "gravidity": (r"^gestation\b", "Gestation"),
        "parity": (r"^parite\b", "Parité"),
        "living_children_count": (r"enfants\s*vivants", "Nombre d'enfants vivants"),
    },
    "current_pregnancy": {
        "height_cm": (r"^taille\b", "Taille"),
    },
    "delivery": {
        "delivery_date": (r"date\s*de\s*l.?\s*accouchement", "Date de l'accouchement"),
        "delivery_gestational_age_days": (r"^age\s*gestationnel", "Âge gestationnel"),
        "newborn_sex": (r"^sexe\b", "Sexe"),
        "birth_weight_g": (r"poids\s*a\s*la\s*naissance", "Poids à la naissance"),
        "birth_head_circumference_cm": (r"perimetre\s*cranien\s*a\s*la\s*naissance", "Périmètre crânien à la naissance"),
    },
    "newborn": {
        "consultation_date": (r"date\s*de\s*la\s*consultation", "Date de la consultation"),
    },
}
CHECKBOXES = {
    "consanguinity_mark": (r"consanguinite", "Consanguinité"),
    "pregnancy_desired_mark": (r"grossesse\s*desiree", "Grossesse désirée"),
}
CHOICE_BOXES = {
    "delivery_mode": (
        ("VAGINAL_NON_INSTRUMENTAL", r"voie\s*basse\s*non\s*instrumentale", "Voie basse non instrumentale"),
        ("VAGINAL_INSTRUMENTAL", r"voie\s*basse\s*instrumentale", "Voie basse instrumentale"),
        ("CESAREAN_PLANNED", r"cesarienne", "Césarienne : Programmée"),
        ("CESAREAN_EMERGENCY", r"\burgence$", "Urgence"),
    ),
    "feeding_mode": (
        ("EXCLUSIVE_BREASTFEEDING", r"exclusivement\s*au\s*sein", "exclusivement au sein"),
        ("ARTIFICIAL", r"\bartificiel$", "Artificiel"),
        ("MIXED", r"\bmixte$", "mixte"),
    ),
}
# Always confirmed by a person: free text has no format rule, and a handwritten count is one digit that OCR reads
# confidently wrong ("1" as "7" on the specimen) with no other field to check it against.
CONFIRM_REQUIRED = {"education_level_text", "previous_delivery_mode_text", "gravidity", "parity",
                    "living_children_count", "abortions_count"}
PERIOD_TITLES = (("EARLY", r"post.?partum\s*precoce"), ("LATE", r"post.?partum\s*tardif"))
PREVIOUS_DELIVERY_HEADER = r"accouch\.?\s*(\d)"
PREVIOUS_DELIVERY_ROWS = {
    "previous_delivery_date": r"^date$",
    "previous_delivery_mode_text": r"modalite",
    "indication": r"cesarienne\s*:?\s*indication|^si\s*cesar",
    "complication": r"^complication",
    "weight": r"poids\s*nouveau",
    "newborn_complication": r"compl\.?\s*nouveau",
}
# Checkbox ink: share of dark pixels inside the located square (border excluded). Calibrated on the ten fictitious
# specimen booklets (docs/ocr.md): marked boxes 0.22-0.82, empty boxes up to 0.125 on a photo-like copy. Between the
# two limits a person looks at the photo.
MARKED_INK = 0.18
UNMARKED_INK = 0.04
BOX_SIDE_INK = 0.8


@dataclass(frozen=True)
class Mark:
    state: str  # MARKED, UNMARKED, UNCLEAR, BOX_NOT_FOUND, LABEL_NOT_FOUND
    label: str
    ink: float | None = None
    rect: tuple[float, float, float, float] | None = None

    def evidence(self) -> dict:
        return {"label": self.label, "state": self.state, "ink": None if self.ink is None else round(self.ink, 3)}


def _clean(items: list[OcrLine]) -> list[OcrLine]:
    """Drops the form's writing line, read as underscores or dots around a value."""
    out = []
    for line in items:
        text = re.sub(r"\s+", " ", re.sub(r"[_…]+|\.{2,}", " ", line.text)).strip(" .:")
        if text:
            out.append(OcrLine(text, line.confidence, line.x0, line.y0, line.x1, line.y1, source_box=line.source_box))
    return out


def _find(page: _Page, pattern: str) -> OcrLine | None:
    return next((l for l in page.lines if re.search(pattern, norm(l.text))), None)


def labelled(page: _Page, name: str, pattern: str, row: str) -> dict:
    """Value after a printed label. Nothing read after the label is unread, never NOT_PROVIDED: OCR may stretch the
    label's box over a handwritten digit it did not recognise (a "1" after "Nombre d'enfants vivants :" on the
    specimen), so the paper right of the box can be blank while the value is not."""
    spec = SPECS[name]
    label = _find(page, pattern)
    if label is None:
        return _review(spec, "OCR_LABEL_NOT_FOUND", page.source())
    items = _clean(_value_beside(page.lines, label))
    source = page.source(items=items or [label], row=row)
    return _confirm(name, _read_value(spec, items, source, "OCR_NO_TEXT", extract=NUMBER.get(name)))


def _confirm(name: str, fv: dict) -> dict:
    if name in CONFIRM_REQUIRED and fv["value"] is not None:
        fv["validation_flags"].append("OCR_CONFIRM_REQUIRED")
        fv["field_status"] = "NEEDS_REVIEW"
    return fv


# ---------------------------------------------------------------- checkboxes

def _square(dark: list[list[bool]], low: int, high: int) -> tuple[int, int, int, float] | None:
    """(row, column, side, score) of the best square outline of side low..high in a dark-pixel grid."""
    rows, cols = len(dark), len(dark[0])
    down = [[0] * (rows + 1) for _ in range(cols)]
    across = [[0] * (cols + 1) for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            down[c][r + 1] = down[c][r] + dark[r][c]
            across[r][c + 1] = across[r][c] + dark[r][c]

    def column(x, r0, r1):
        return (down[x][r1 + 1] - down[x][r0]) / (r1 - r0 + 1)

    def row(y, c0, c1):
        return (across[y][c1 + 1] - across[y][c0]) / (c1 - c0 + 1)

    best = None
    for side in range(low, high + 1):
        for r in range(rows - side):
            for c in range(cols - side):
                # Each side may be one pixel inside the outline (borders are one or two pixels thick).
                left = max(column(c, r, r + side), column(c + 1, r, r + side))
                right = max(column(c + side, r, r + side), column(c + side - 1, r, r + side))
                top = max(row(r, c, c + side), row(r + 1, c, c + side))
                bottom = max(row(r + side, c, c + side), row(r + side - 1, c, c + side))
                score = min(left, right, top, bottom)
                if score >= BOX_SIDE_INK and (best is None or (score, side) > (best[3], best[2])):
                    best = (r, c, side, score)
    return best


def checkbox(page: _Page, pattern: str, label_text: str) -> Mark:
    """State of the checkbox printed just before a label (left of its text, or inside the OCR box when the box was
    read together with the label)."""
    label = _find(page, pattern)
    if label is None:
        return Mark("LABEL_NOT_FOUND", label_text)
    scan = page.scan
    if scan.thumbnail is None or not scan.width or not scan.height:
        return Mark("BOX_NOT_FOUND", label_text)
    text = norm(label.text)
    start = re.search(pattern, text).start()
    h = label.height
    x = label.x0 + (label.x1 - label.x0) * start / max(len(text), 1)
    window = (x - 2.6 * h, label.cy - 1.0 * h, x + 1.8 * h, label.cy + 1.0 * h)
    samples = page.samples(window)
    if samples is None or len(samples) < 6 or len(samples[0]) < 6:
        return Mark("BOX_NOT_FOUND", label_text)
    values = sorted(v for r in samples for v in r)
    limit = values[int(0.9 * (len(values) - 1))] - 0.3 * scan.ink_depth()
    dark = [[v < limit for v in r] for r in samples]
    tw, th, _data = scan.thumbnail
    unit = th / scan.height
    found = _square(dark, max(4, round(0.5 * h * unit)), max(5, round(1.3 * h * unit)))
    if found is None:
        return Mark("BOX_NOT_FOUND", label_text)
    r, c, side, _score = found
    inside = [dark[y][c + 2:c + side - 1] for y in range(r + 2, r + side - 1)]
    cells = sum(len(line) for line in inside)
    ink = sum(sum(line) for line in inside) / cells if cells else 0.0
    step_x = (window[2] - window[0]) / len(samples[0])
    step_y = (window[3] - window[1]) / len(samples)
    rect = (window[0] + c * step_x, window[1] + r * step_y, window[0] + (c + side + 1) * step_x,
            window[1] + (r + side + 1) * step_y)
    state = "MARKED" if ink >= MARKED_INK else "UNMARKED" if ink <= UNMARKED_INK else "UNCLEAR"
    return Mark(state, label_text, ink, rect)


def _mark_flag(mark: Mark) -> str:
    return {"UNCLEAR": "CHECKBOX_UNCLEAR", "BOX_NOT_FOUND": "CHECKBOX_NOT_FOUND",
            "LABEL_NOT_FOUND": "OCR_LABEL_NOT_FOUND"}[mark.state]


def single_box(page: _Page, name: str) -> dict:
    pattern, label = CHECKBOXES[name]
    mark = checkbox(page, pattern, label)
    source = {**page.source(mark.rect, row=label), "checkboxes": [mark.evidence()]}
    if mark.state in ("MARKED", "UNMARKED"):
        return _field(SPECS[name], status="KNOWN", value=mark.state, source=source)
    return _field(SPECS[name], status="NEEDS_REVIEW", flags=[_mark_flag(mark)], source=source)


def choice_boxes(page: _Page, name: str, row: str) -> dict:
    """One value from a group of boxes: exactly one marked box, every other box clearly empty."""
    spec = SPECS[name]
    marks = [(value, checkbox(page, pattern, label)) for value, pattern, label in CHOICE_BOXES[name]]
    rects = [m.rect for _v, m in marks if m.rect]
    rect = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects),
            max(r[3] for r in rects)) if rects else None
    source = {**page.source(rect, row=row), "checkboxes": [m.evidence() for _v, m in marks]}
    marked = [value for value, m in marks if m.state == "MARKED"]
    doubts = list(dict.fromkeys(_mark_flag(m) for _v, m in marks if m.state not in ("MARKED", "UNMARKED")))
    if len(marked) > 1:
        return _field(spec, status="NEEDS_REVIEW", flags=["CHECKBOX_MULTIPLE_MARKED", *doubts], source=source)
    if marked:
        return _field(spec, status="NEEDS_REVIEW" if doubts else "KNOWN", value=marked[0], flags=doubts,
                      source=source)
    if doubts:
        return _field(spec, status="NEEDS_REVIEW", flags=doubts, source=source)
    return _field(spec, status="NOT_PROVIDED", flags=["CHECKBOX_NONE_MARKED"], source=source)


# ---------------------------------------------------------------- tables

def _row_bands(labels: list[OcrLine]) -> list[tuple[OcrLine, float, float]]:
    labels = sorted(labels, key=lambda l: l.cy)
    if len(labels) < 2:
        return []
    pitch = sorted(b.cy - a.cy for a, b in zip(labels, labels[1:]))[(len(labels) - 1) // 2]
    bands = []
    for i, label in enumerate(labels):
        top = (labels[i - 1].cy + label.cy) / 2 if i else label.cy - pitch / 2
        bottom = (labels[i + 1].cy + label.cy) / 2 if i + 1 < len(labels) else label.cy + pitch / 2
        bands.append((label, top, bottom))
    return bands


def _cell(page: _Page, name: str, rect: tuple[float, float, float, float], row: str, column: str) -> dict:
    x0, y0, x1, y1 = rect
    items = sorted((l for l in page.lines if x0 <= l.cx < x1 and y0 <= l.cy < y1), key=lambda l: l.x0)
    empty = "OCR_NO_TEXT" if items else page.empty_cell(rect)
    return _confirm(name, _read_value(SPECS[name], _clean(items), page.source(rect, items, row=row, column=column),
                                      empty, extract=NUMBER.get(name)))


def abortions(page: _Page) -> dict:
    """Antécédents obstétricaux: row "Avortement", column "Nombre" (up to the next header, "Date")."""
    spec = SPECS["abortions_count"]
    header = next((l for l in page.lines if norm(l.text) == "nombre"), None)
    row = _find(page, r"^avortement")
    if header is None or row is None:
        return _review(spec, "OCR_ROW_NOT_FOUND", page.source())
    following = [l for l in page.lines if l.x0 > header.x1 and abs(l.cy - header.cy) < header.height]
    if not following:
        return _review(spec, "OCR_ROW_NOT_FOUND", page.source())
    pitch = min(l.x0 for l in following) - header.x0
    left, right = header.x0 - 0.1 * pitch, header.x0 + 0.9 * pitch
    labels = [l for l in page.lines if l.x1 < left and header.cy - 2 * header.height < l.cy < row.cy + 4 * row.height]
    band = next(((top, bottom) for label, top, bottom in _row_bands(labels) if label is row), None)
    if band is None:
        return _review(spec, "OCR_ROW_NOT_FOUND", page.source())
    return _cell(page, "abortions_count", (left, band[0], right, band[1]), "Avortement", "Nombre")


def previous_deliveries(page: _Page) -> list[dict] | None:
    """Déroulement des accouchements antérieurs: one item per column with writing; None when the table is not found."""
    headers = {}
    for line in page.lines:
        match = re.fullmatch(PREVIOUS_DELIVERY_HEADER, norm(line.text))
        if match and 1 <= int(match.group(1)) <= 5:
            headers.setdefault(int(match.group(1)), line)
    if len(headers) < 3:
        return None
    ordered = sorted(headers.items(), key=lambda kv: kv[1].x0)
    if [n for n, _l in ordered] != sorted(headers):
        return None
    (first_n, first), (last_n, last) = ordered[0], ordered[-1]
    pitch = (last.x0 - first.x0) / (last_n - first_n)
    lefts = {n: first.x0 + (n - first_n - 0.1) * pitch for n in range(1, 6)}
    bottom = max(l.y1 for _n, l in ordered)
    stop = min((l.cy for l in page.lines if re.match(r"^gestation\b", norm(l.text)) and l.cy > bottom), default=None)
    labels = [l for l in page.lines if l.x1 < lefts[1] and l.cy > bottom and (stop is None or l.cy < stop)]
    rows = {}
    for label, top, low in _row_bands(labels):
        for key, pattern in PREVIOUS_DELIVERY_ROWS.items():
            if key not in rows and re.search(pattern, norm(label.text)):
                rows[key] = (top, low)
    if "previous_delivery_date" not in rows or "previous_delivery_mode_text" not in rows:
        return None
    items = []
    for n in range(1, 6):
        left, right = lefts[n], lefts[n] + pitch
        written = any(" ".join(l.text for l in page.lines if left <= l.cx < right and top <= l.cy < low).strip()
                      not in DASHES | {""} for top, low in rows.values())
        if not written:
            continue
        fields = {}
        for name, row in (("previous_delivery_date", "Date"), ("previous_delivery_mode_text", "Modalité d'extraction")):
            top, low = rows[name]
            fields[name] = _cell(page, name, (left, top, right, low), row, f"Accouch. {n}")
        items.append({"column": n, "fields": fields})
    return items


def visit_labs(page: _Page) -> dict[str, dict]:
    """Lab cells of each grid column: {slot: {field: value}}, for columns with lab text and all others too, so a
    visit column with empty lab cells says so."""
    grid = find_grid(page.lines)
    if grid is None:
        return {}
    cells = assign_cells(page.lines, grid)
    out = {}
    for slot, left, right in grid.columns:
        fields = {}
        for name, (_pattern, label) in LAB_ROWS.items():
            if name not in grid.rows:
                fields[name] = _review(SPECS[name], "OCR_ROW_NOT_FOUND", page.source())
                continue
            items = cells.text.get((slot, name), [])
            rect = _cell_rect(grid, name, left, right)
            spans = (slot, name) in cells.spanning
            empty = "OCR_TEXT_SPANS_COLUMNS" if spans and not items else "OCR_NO_TEXT" if items else page.empty_cell(rect)
            fields[name] = _read_value(SPECS[name], items, page.source(rect, items, row=label, column=slot), empty,
                                       ("OCR_TEXT_SPANS_COLUMNS",) if spans and items else (),
                                       extract=NUMBER.get(name))
        out[slot] = fields
    return out


def _period(page: _Page) -> str | None:
    text = " ".join(norm(l.text) for l in page.lines)
    found = [period for period, pattern in PERIOD_TITLES if re.search(pattern, text)]
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------- assembly

def build_extended(read: list[tuple[_Page, str, dict]], encounter_slots: set[str]) -> dict:
    """The draft's "extended" object from the read pages; sections without a page of their type are absent."""
    single: dict[str, dict[str, list[dict]]] = {}
    listed: dict[str, dict[object, dict[str, list[dict]]]] = {}

    def add(section, name, fv, key=None):
        if key is None:
            single.setdefault(section, {}).setdefault(name, []).append(fv)
        else:
            listed.setdefault(section, {}).setdefault(key, {}).setdefault(name, []).append(fv)

    for page, section, _entry in read:
        # A newborn consultation is keyed by its period, read from the page title; without one it is not kept.
        period = _period(page) if section == "newborn" else None
        if section == "newborn" and period is None:
            continue
        for name, (pattern, row) in LABELLED.get(section, {}).items():
            target = extended.SECTION_OF[name]
            # One newborn block per delivery page: index 1. Several newborns are decision D6.
            key = {"newborns": 1, "newborn_consultations": period}.get(target)
            add(target, name, labelled(page, name, pattern, row), key)
        if section == "identification":
            for name in CHECKBOXES:
                add("pregnancy", name, single_box(page, name))
            add("pregnancy", "abortions_count", abortions(page))
            for item in previous_deliveries(page) or []:
                for name, fv in item["fields"].items():
                    add("previous_deliveries", name, fv, item["column"])
        elif section == "current_pregnancy":
            for slot, fields in visit_labs(page).items():
                written = any(fv["value"] is not None or (fv["raw_text"] or "").strip() not in DASHES | {""}
                              for fv in fields.values())
                if slot in encounter_slots or written:
                    for name, fv in fields.items():
                        add("visit_labs", name, fv, slot)
        elif section == "delivery":
            add("delivery", "delivery_mode", choice_boxes(page, "delivery_mode", "Mode de l'accouchement"))
        elif section == "newborn":
            add("newborn_consultations", "feeding_mode", choice_boxes(page, "feeding_mode", "Allaitement"), period)

    ext = extended.empty()
    for section, fields in single.items():
        specs = {s.name: s for s in extended.SECTIONS[section].fields}
        ext[section] = {name: merge_readings(specs[name], readings) for name, readings in fields.items()}
    for section, items in listed.items():
        spec_section = extended.SECTIONS[section]
        specs = {s.name: s for s in spec_section.fields}
        order = list(spec_section.item_keys)
        ext[section] = [{spec_section.item_key: key,
                         "fields": {name: merge_readings(specs[name], readings) for name, readings in fields.items()}}
                        for key, fields in sorted(items.items(), key=lambda kv: order.index(kv[0]))]
    for item in ext.get("visit_labs", []):
        if item["slot"] not in encounter_slots:
            for fv in item["fields"].values():
                if "LAB_WITHOUT_VISIT" not in fv["validation_flags"]:
                    fv["validation_flags"].append("LAB_WITHOUT_VISIT")
                if fv["field_status"] == "KNOWN":
                    fv["field_status"] = "NEEDS_REVIEW"
    return ext
