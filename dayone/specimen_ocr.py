"""Reader for the specimen booklet layout (``dossiers_specimen_10_patientes``).

Every patient booklet has eight printed pages. Pages are recognised by their
printed title, never by file name, so camera photos work too. The visit grid
("GROSSESSE ACTUELLE") is read cell by cell: rows are anchored on their printed
labels and columns on the fixed table geometry.

A value becomes ``KNOWN`` only when it is confirmed twice:

1. at least three of four independent OCR readings agree and none disagrees
   (two page-level readings in French and English, two readings of the cell
   crop with and without a character whitelist); the reported confidence is
   the share of readings that agree;
2. it is consistent with the rest of the booklet (gestational age against DDR
   and visit date, visit dates in order, weight and fundal height plausible
   against neighbouring visits).

Everything else goes to review with the best candidate, if any, and a flag that
says why. Nothing is inferred from other fields: a check can only demote a
value, never supply one.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from . import schema

READINGS = 4
MIN_AGREEING = 3
GA_TOLERANCE_DAYS = 4
MAX_WEIGHT_DROP_KG = 1.5
MAX_WEIGHT_GAIN_KG_PER_DAY = 0.15
MAX_WEIGHT_STEP_KG = 8  # when a visit date is missing
MAX_FUNDAL_GAP_CM = 6
INK_LEVEL = 150  # grey level below which a pixel counts as pen ink or print

# Table geometry of the printed grid (pixels on the 1654 x 2339 specimen scan).
COLUMN_EDGES = (421, 546, 670, 795, 919, 1044, 1168, 1293, 1417, 1542)
LABEL_COLUMN_RIGHT = 400
NOMINAL_FIRST_LABEL_LEFT = 122
GRID_SLOTS = ("T1_V1", "T1_V2", "T1_V3", "T2_V1", "T2_V2", "T2_V3", "M7", "M8", "M9")

# Grid rows read for the v1 contract: row key -> (printed label prefix, whitelist, row label for the reviewer).
GRID_ROWS = {
    "visit_date": ("Venue", "0123456789/", "Venue le"),
    "gestational_age_days": ("Age", "0123456789SA", "Âge probable"),
    "weight_kg": ("Poids", "0123456789.", "Poids (kg)"),
    "bp": ("TA", "0123456789/", "TA"),
    "fundal_height_cm": ("HU", "0123456789", "HU (cm)"),
    "syphilis_test": ("Syphilis", "NegPosnégatif", "Syphilis (TPHA/VDRL)"),
    "hiv_test": ("Sérologie", "NegPosnégatif", "Sérologie VIH"),
}

# Printed titles -> section. Order matters: the first match wins.
TITLES = (
    ("cover", ("FICHE", "SURVEILLANCE")),
    ("identification", ("IDENTIFICATION",)),
    ("current_pregnancy", ("GROSSESSE", "ACTUELLE")),
    ("delivery_not_in_v1", ("ACCOUCHEMENT",)),
    ("postpartum_newborn_not_in_v1", ("POST-PARTUM", "NOUVEAU-NÉ")),
    ("postpartum_mother_not_in_v1", ("POST-PARTUM",)),
)

# Document fields read next to a printed label: field -> (section, label regex, right edge, whitelist).
DOCUMENT_LABELS = {
    "registry_file_number": ("cover", r"^fiche$", 900, "0123456789-"),
    "facility_name": ("cover", r"^sanitaire$", 1100, None),
    "last_menstrual_period": ("current_pregnancy", r"^DDR", 430, "0123456789/"),
}


@dataclass
class Reading:
    """Normalized candidate for one cell: best value, its raw text, votes and disagreement."""

    value: object = None
    raw_text: str | None = None
    votes: int = 0
    dissent: bool = False
    ink: str = "ink"  # "ink", "blank" or "dash"
    flags: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float | None:
        return round(self.votes / READINGS, 2) if self.value is not None else None

    @property
    def agreed(self) -> bool:
        return self.value is not None and not self.dissent and self.votes >= MIN_AGREEING


def classify(words) -> tuple[str | None, bool]:
    """Return (section, is_specimen_layout) from the printed title near the top of the page."""
    top = {w.text.upper().strip(":.,") for w in words if w.top < 140}
    specimen = "SPÉCIMEN" in top or "SPECIMEN" in top or "FICTIVES" in top
    for section, needed in TITLES:
        if all(token in top for token in needed):
            return section, specimen
    return None, specimen


def without_rules(crop):
    """White out printed table lines that leak into a cell crop (rows or columns that are almost all dark)."""
    width, height = crop.size
    mask = crop.point(lambda v: 255 if v < INK_LEVEL else 0)
    from PIL import Image

    row_share = mask.resize((1, height), Image.BOX).tobytes()
    column_share = mask.resize((width, 1), Image.BOX).tobytes()
    cleaned = crop.copy()
    pixels = cleaned.load()
    for y, share in enumerate(row_share):
        if share > 255 * 0.6:
            for x in range(width):
                pixels[x, y] = 255
    for x, share in enumerate(column_share):
        if share > 255 * 0.8:
            for y in range(height):
                pixels[x, y] = 255
    return cleaned


def ink_state(page, box) -> str:
    """"blank" when the cell has no pen mark, "dash" for a lone horizontal stroke, else "ink"."""
    mask = without_rules(page.crop(box)).point(lambda v: 255 if v < INK_LEVEL else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return "blank"
    return "dash" if bbox[3] - bbox[1] <= 6 else "ink"


def normalize(name: str, text: str):
    """Strictly parse one OCR reading of a cell; anything ambiguous returns None."""
    t = text.strip().replace(",", ".")
    try:
        if name == "gestational_age_days":
            match = re.fullmatch(r"(\d{1,2})\s*S?A?", t, re.IGNORECASE)
            return schema.FIELDS[name].parse(match.group(1)) if match else None
        if name == "bp":
            match = re.fullmatch(r"(\d{2,3})/(\d{2,3})", t.replace(" ", ""))
            if not match:
                return None
            systolic, diastolic = int(match.group(1)), int(match.group(2))
            if schema.FIELDS["systolic_bp_mmhg"].range_error(systolic) or \
                    schema.FIELDS["diastolic_bp_mmhg"].range_error(diastolic) or systolic <= diastolic:
                return None
            return systolic, diastolic
        if name in ("syphilis_test", "hiv_test"):
            if re.fullmatch(r"n[ée]g(atif)?\.?", t, re.IGNORECASE):
                return "NEGATIVE"
            return "POSITIVE" if re.fullmatch(r"pos(itif)?\.?", t, re.IGNORECASE) else None
        if name in ("visit_date", "last_menstrual_period"):
            t = re.sub(r"^\D+|\D+$", "", t)
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", t):
                return None
        elif name == "weight_kg" and not re.fullmatch(r"\d{2,3}\.\d", t):
            return None
        elif name == "fundal_height_cm" and not re.fullmatch(r"\d{1,2}", t):
            return None
        elif name == "registry_file_number":
            t = re.sub(r"^\D+|\D+$", "", t)
            if not re.fullmatch(r"\d[\d-]{2,14}\d", t):
                return None
        elif name == "facility_name":
            t = re.sub(r"^[^0-9A-Za-zÀ-ÿ(]+", "", t)
            if not re.fullmatch(r"[A-Za-zÀ-ÿ'][A-Za-zÀ-ÿ' -]{1,60}", t):
                return None
        value = schema.FIELDS[name].parse(t)
        spec = schema.FIELDS[name]
        return None if spec.range_error(value) else value
    except (schema.InvalidValue, ValueError):
        return None


def combine(name: str, texts: list[str | None]) -> Reading:
    """Vote over the readings: the most frequent normalized value wins only if it is unique."""
    parsed = [(text, normalize(name, text)) for text in texts if text]
    values = [value for _, value in parsed if value is not None]
    counts = Counter(values).most_common()
    raw = next((text for text in texts if text), None)
    if not counts or (len(counts) > 1 and counts[0][1] == counts[1][1]):
        reading = Reading(raw_text=raw, dissent=len(counts) > 1)
        reading.flags.append("OCR_READINGS_DISAGREE" if counts else "OCR_VALUE_UNCLEAR")
        return reading
    value, votes = counts[0]
    raw = next(text for text, parsed_value in parsed if parsed_value == value)
    reading = Reading(value=value, raw_text=raw, votes=votes, dissent=len(counts) > 1)
    if reading.dissent:
        reading.flags.append("OCR_READINGS_DISAGREE")
    elif votes < MIN_AGREEING:
        reading.flags.append("OCR_FEW_READINGS_AGREE")
    return reading


def _words_in(words, x0: float, x1: float, centre_y: float, tolerance: float = 22):
    hits = [w for w in words
            if x0 <= w.left + w.width / 2 < x1 and abs(w.top + w.height / 2 - centre_y) < tolerance]
    return " ".join(w.text for w in sorted(hits, key=lambda w: w.left)) or None


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class SpecimenReader:
    """Reads grid cells and labelled document fields; ``engine`` provides page and line OCR."""

    def __init__(self, engine, workers: int = 8):
        self.engine = engine
        self.workers = workers

    def _line_texts(self, crops: list[tuple[object, str | None]]) -> list[tuple[str | None, str | None]]:
        """Two crop readings per box: with the whitelist and without it."""
        def read(job):
            image, whitelist = job
            data = _png(image)
            with_list = " ".join(w.text for w in self.engine.read_line(data, whitelist)) or None
            without = " ".join(w.text for w in self.engine.read_line(data, None)) or None
            return with_list, without
        if not crops:
            return []
        with ThreadPoolExecutor(min(self.workers, len(crops))) as pool:
            return list(pool.map(read, crops))

    def read_grid(self, page, words_fr, words_en) -> dict | None:
        """Return {slot: {row: Reading}} for columns with a visit date, or None when the layout is not found."""
        anchors = {}
        for row, (prefix, _, _) in GRID_ROWS.items():
            hits = [w for w in words_fr if w.left < LABEL_COLUMN_RIGHT and w.text.startswith(prefix)]
            if not hits:
                return None
            anchors[row] = min(hits, key=lambda w: w.top).top
        first = [w for w in words_fr if w.text.startswith("Rendez")]
        dx = first[0].left - NOMINAL_FIRST_LABEL_LEFT if first else 0

        cells, crops, keys = {}, [], []
        for row, top in anchors.items():
            for column, slot in enumerate(GRID_SLOTS):
                box = (COLUMN_EDGES[column] + dx + 6, top - 15, COLUMN_EDGES[column + 1] + dx - 6, top + 27)
                state = ink_state(page, box)
                cells[(row, slot)] = {"box": box, "ink": state, "texts": []}
                if state == "ink":
                    centre = top + 9
                    x0, x1 = COLUMN_EDGES[column] + dx, COLUMN_EDGES[column + 1] + dx
                    cells[(row, slot)]["texts"] = [_words_in(words_fr, x0, x1, centre), _words_in(words_en, x0, x1, centre)]
                    crops.append((without_rules(page.crop(box)), GRID_ROWS[row][1]))
                    keys.append((row, slot))
        for key, (with_list, without) in zip(keys, self._line_texts(crops)):
            cells[key]["texts"] += [with_list, without]

        grid: dict[str, dict] = {}
        for column, slot in enumerate(GRID_SLOTS):
            if cells[("visit_date", slot)]["ink"] != "ink":
                continue
            grid[slot] = {}
            for row in GRID_ROWS:
                cell = cells[(row, slot)]
                if cell["ink"] == "ink":
                    reading = combine(row, cell["texts"])
                else:
                    reading = Reading(ink=cell["ink"], raw_text="–" if cell["ink"] == "dash" else None)
                reading.box = cell["box"]
                grid[slot][row] = reading
        return grid

    def read_labelled(self, name: str, page, words_fr, words_en) -> Reading | None:
        """Read the handwritten value printed to the right of a label (cover fields, DDR)."""
        _, pattern, right, whitelist = DOCUMENT_LABELS[name]
        labels = [w for w in words_fr if re.search(pattern, w.text) and w.left < 700 and w.top > 150]
        if not labels:
            return None
        label = labels[0]
        x0, centre = label.left + label.width + 4, label.top + 8
        box = (x0, label.top - 14, right, label.top + 30)
        if ink_state(page, box) == "blank":
            return Reading(ink="blank")

        def beside(words):
            hits = [w for w in words if x0 - 2 <= w.left < right and abs(w.top + w.height / 2 - centre) < 20
                    and w.text != ":"]
            return " ".join(w.text for w in sorted(hits, key=lambda w: w.left)) or None

        (with_list, without), = self._line_texts([(without_rules(page.crop(box)), whitelist)])
        reading = combine(name, [beside(words_fr), beside(words_en), with_list, without])
        reading.box = box
        return reading


# ---------------------------------------------------------------- consistency

def _days(later: str, earlier: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def check_booklet(grid: dict, lmp: Reading | None) -> None:
    """Demote agreed values that contradict the rest of the booklet; add flags explaining why."""
    def value(slot, row):
        reading = grid[slot].get(row)
        return reading.value if reading is not None else None

    slots = [slot for slot in GRID_SLOTS if slot in grid]
    lmp_value = lmp.value if lmp is not None else None

    # DDR is KNOWN only when visits confirm it: date - DDR matches the written gestational age.
    if lmp is not None and lmp_value is not None:
        matches = sum(
            1 for slot in slots
            if value(slot, "visit_date") and value(slot, "gestational_age_days") is not None
            and abs(_days(value(slot, "visit_date"), lmp_value) - value(slot, "gestational_age_days")) <= GA_TOLERANCE_DAYS
        )
        if not (matches >= 2 or (matches == 1 and lmp.agreed)):
            lmp.flags.append("DDR_NOT_CONFIRMED_BY_VISITS")
            lmp.dissent = True
        else:
            lmp.votes = max(lmp.votes, MIN_AGREEING)  # corroborated by the visit grid
            lmp.dissent = False
            lmp.flags = [f for f in lmp.flags if not f.startswith("OCR_")]
    trusted_lmp = lmp_value if lmp is not None and lmp.agreed else None

    def demote(slot, row, flag):
        reading = grid[slot][row]
        if flag not in reading.flags:
            reading.flags.append(flag)
        reading.dissent = True

    for index, slot in enumerate(slots):
        visit = value(slot, "visit_date")
        ga = value(slot, "gestational_age_days")
        earlier = [value(s, "visit_date") for s in slots[:index] if value(s, "visit_date")]
        later = [value(s, "visit_date") for s in slots[index + 1:] if value(s, "visit_date")]
        if visit and ((earlier and earlier[-1] >= visit) or (later and later[0] <= visit)):
            demote(slot, "visit_date", "VISIT_DATES_OUT_OF_ORDER")

        consistent = None
        if visit and ga is not None and trusted_lmp:
            consistent = abs(_days(visit, trusted_lmp) - ga) <= GA_TOLERANCE_DAYS
        elif visit and ga is not None:
            neighbours = [s for s in slots if s != slot and value(s, "visit_date")
                          and value(s, "gestational_age_days") is not None]
            if neighbours:
                consistent = any(abs((ga - value(s, "gestational_age_days")) - _days(visit, value(s, "visit_date"))) <= 7
                                 for s in neighbours)
        if consistent is False:
            demote(slot, "gestational_age_days", "AGE_INCONSISTENT_WITH_DATES")
            demote(slot, "visit_date", "AGE_INCONSISTENT_WITH_DATES")
        elif consistent is None and ga is not None:
            demote(slot, "gestational_age_days", "AGE_NOT_CONFIRMED")

        weight = value(slot, "weight_kg")
        if weight is not None:
            for other in slots[max(0, index - 1):index] + slots[index + 1:index + 2]:
                other_weight = value(other, "weight_kg")
                if other_weight is None:
                    continue
                first, second = (other, slot) if GRID_SLOTS.index(other) < GRID_SLOTS.index(slot) else (slot, other)
                change = value(second, "weight_kg") - value(first, "weight_kg")
                if value(first, "visit_date") and value(second, "visit_date"):
                    days = _days(value(second, "visit_date"), value(first, "visit_date"))
                    plausible = -MAX_WEIGHT_DROP_KG <= change <= MAX_WEIGHT_DROP_KG + MAX_WEIGHT_GAIN_KG_PER_DAY * max(days, 0)
                else:
                    plausible = abs(change) <= MAX_WEIGHT_STEP_KG
                if not plausible:
                    demote(slot, "weight_kg", "WEIGHT_CHANGE_IMPLAUSIBLE")

    # Fundal height is checked against confirmed gestational ages only (after the checks above).
    for slot in slots:
        fundal = value(slot, "fundal_height_cm")
        if fundal is None:
            continue
        age = grid[slot].get("gestational_age_days")
        if age is None or not age.agreed:
            demote(slot, "fundal_height_cm", "FUNDAL_HEIGHT_NOT_CONFIRMED")
        elif abs(fundal - age.value // 7) > MAX_FUNDAL_GAP_CM:
            demote(slot, "fundal_height_cm", "FUNDAL_HEIGHT_INCONSISTENT_WITH_AGE")
