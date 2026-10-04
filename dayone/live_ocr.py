"""Turns local OCR lines into a schema v1.0 draft (docs/ocr.md).

Started from Fatma's conservative extractor (origin/pinar, ff04285 to 1e4643c): photo checks before OCR, TA in
cmHg (12/7 -> 120/70), dates never read as TA, and nothing inferred from unclear text. Pages, sections, rows and
visit columns are located from the printed text that OCR finds, never from file names or fixed pixel positions.
Recognised text is never logged; names and identifiers are only reported as categories in pii_detected.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import statistics
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path

from . import schema
from .extraction import RETAKE_REASONS, ExtractionCancelled, ExtractionError, ExtractorUnavailable, PhotoRejected
from .ocr_engines import OcrLine

log = logging.getLogger("dayone")

PARSER_VERSION = "grid-4"
MIN_KNOWN_CONFIDENCE = 0.85
MAX_GA_GAP_DAYS = 14

COLUMN_SLOTS = ("T1_V1", "T1_V2", "T1_V3", "T2_V1", "T2_V2", "T2_V3", "M7", "M8", "M9")
VISIT_ROWS = {
    "visit_date": (r"venue\s*le\b", "Venue le"),
    "gestational_age_days": (r"age\s*probable", "Age probable"),
    "weight_kg": (r"poids\b", "Poids (kg)"),
    "bp": (r"t\.?\s*a\.?", "TA"),
    "fundal_height_cm": (r"h\.?\s*u\b(?!\w)", "HU (cm)"),
    "syphilis_test": (r"syph\w*\s*\(?(?:tpha|vdrl).*|syph\w*", "Syphilis (TPHA/VDRL)"),
    "hiv_test": (r"serologie\s*vih|vih", "Sérologie VIH"),
}
# Examen biologique rows, read into extended.visit_labs (dayone/ocr_extended.py). They never make a column a visit.
LAB_ROWS = {
    "hemoglobin_g_dl": (r"hemoglobine", "Hémoglobine"),
    "blood_glucose_g_l": (r"bilan\s*glyc\w*", "Bilan glycémique"),
    "albuminuria": (r"albuminurie", "Albuminurie"),
}
# A column is a visit when one of these rows holds something other than a dash, even without a date.
VISIT_EVIDENCE = tuple(VISIT_ROWS)
# A text box is split across columns when it overlaps two of them, each by more than this share of its width.
SPAN_SHARE = 0.25
PII_LABELS = (
    (r"nom\s*/?\s*prenom|parturiente", "PATIENT_NAME"),
    (r"\bcin\b", "NATIONAL_ID"),
    (r"telephone|\btel\b", "PHONE"),
    (r"adresse", "ADDRESS"),
    (r"\bmari\b", "HUSBAND_NAME"),
    (r"examen\s*fait\s*par", "STAFF_NAME"),
)
DASHES = {"-", "–", "—", "_", "--", "−"}
DATE_IN_TEXT = r"\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{2,4}"
# Column headers as OCR reads them on photos: "Visite 22", "Viste 3", "7erne mois", "Béme mois". Order gives the
# slot. A visit header needs its number, so the row label "Visites" alone does not match.
VISIT_HEADER = r"vi\S{2,4}\s+\S{1,2}|vi\S{2,4}\d"
MONTH_HEADER = r"\S{1,3}\s*\S{0,4}\s*mois"
MAX_SKEW_RADIANS = 0.2
# Printed labels of each specimen page type, found on all ten fictitious booklets. Two distinct labels identify a
# page; one label alone, or a tie, is uncertain and goes to a person.
SECTION_SIGNS = {
    "cover": (r"fiche de surveillance de la grossesse", r"type de l.?etablissement", r"grossesse classee a risque",
              r"n\s*[°o]?\s*de\s*la\s*fiche", r"nom\s*de\s*l.?\s*etablissement"),
    "identification": (r"identification et antecedents", r"antecedents de la femme", r"antecedents obstetricaux",
                       r"accouchements anterieurs", r"antecedents hereditaires"),
    "current_pregnancy": (r"grossesse actuelle", r"examen clinique", r"examen biologique", r"prestations\s*/\s*visites",
                          r"age probable", r"syphilis", r"serologie vih"),
    "delivery": (r"deroulement de l.?accouchement", r"au moment de l.?accouchement", r"voie basse instrumentale",
                 r"suites de couches"),
    "postpartum": (r"etat des lochies", r"etat du perinee", r"planification familiale", r"globe uterin"),
    "newborn": (r"etat vaccinal", r"allaitement maternel", r"affection grave", r"luxation congenitale"),
}
# Blank-cell check on the page thumbnail (calibrated in docs/ocr.md). A pixel is ink when it is darker than the paper
# of its cell by INK_CONTRAST of the page's ink depth (paper minus the darkest print, at least MIN_INK_DEPTH grey
# levels). A cell without OCR text is verified blank only when its ink share stays below MAX_BLANK_INK. A pixel row or
# column within RULE_ZONE of a cell edge that is at least RULE_SHARE ink is a printed rule, not writing.
INK_CONTRAST = 0.3
MIN_INK_DEPTH = 40
MAX_BLANK_INK = 0.01
FRAME_TOLERANCE = 0.01
RULE_ZONE, RULE_SHARE = 0.2, 0.6


@dataclass
class PageScan:
    """One page as read by the OCR process. Coordinates are pixels of the upright, rescaled image that was read.

    thumbnail is (width, height, 8-bit greyscale bytes) of that image, used to tell blank cells from unread ink.
    Nothing here is written to disk or logged.
    """

    lines: list[OcrLine]
    width: int | None = None
    height: int | None = None
    exif_transposed: bool = False
    rotated_ccw: int = 0
    scale: float = 1.0
    thumbnail: tuple[int, int, bytes] | None = None
    warnings: list[str] = field(default_factory=list)
    _depth: int | None = field(default=None, init=False, repr=False, compare=False)

    def ink_depth(self) -> int:
        """How much darker the print is than the paper on this page (90th minus 1st brightness percentile)."""
        if self._depth is None:
            data = self.thumbnail[2]
            counts = [data.count(bytes([v])) for v in range(256)]

            def percentile(share):
                seen = 0
                for value, count in enumerate(counts):
                    seen += count
                    if seen >= share * len(data):
                        return value
                return 255

            self._depth = max(MIN_INK_DEPTH, percentile(0.9) - percentile(0.01))
        return self._depth

    def ink_share(self, samples: list[list[int]]) -> float:
        """Share of ink among the grey samples of one cell (rows of pixels, one thumbnail pixel apart)."""
        values = sorted(v for row in samples for v in row)
        limit = values[int(0.9 * (len(values) - 1))] - INK_CONTRAST * self.ink_depth()
        dark = [[v < limit for v in row] for row in samples]
        height, width = len(dark), len(dark[0])

        def rule(index, size, count, length):
            return min(index, size - 1 - index) < RULE_ZONE * size and count >= RULE_SHARE * length

        columns = [c for c in range(width) if not rule(c, width, sum(r[c] for r in dark), height)]
        kept = [r for i, r in enumerate(dark) if not rule(i, height, sum(r), width)]
        if not columns or not kept:
            return 0.0
        return sum(r[c] for r in kept for c in columns) / (len(columns) * len(kept))


@dataclass(frozen=True)
class PageUnread:
    """A page that gave no reading: PHOTO_UNUSABLE (retake_reason from RETAKE_REASONS), OCR_TIMEOUT or OCR_FAILED."""

    code: str
    retake_reason: str | None = None


def norm(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", plain.lower()).strip()


@dataclass
class Grid:
    columns: list[tuple[str, float, float]]  # slot, left, right
    header_bottom: float
    rows: dict[str, tuple[float, float]]  # visit row -> top, bottom


def _headers(lines: list[OcrLine]) -> tuple[list[OcrLine], list[OcrLine]]:
    visits = sorted((l for l in lines if re.fullmatch(VISIT_HEADER, norm(l.text))), key=lambda l: l.x0)
    months = sorted((l for l in lines if re.fullmatch(MONTH_HEADER, norm(l.text))), key=lambda l: l.x0)
    return visits, months


def skew_angle(lines: list[OcrLine]) -> float:
    """Slope of the grid's header row in radians (0 when there is no grid or the slope is implausible)."""
    anchors = [l for group in _headers(lines) for l in group]
    if len(anchors) < 4:
        return 0.0
    mean_x, mean_y = statistics.fmean(a.cx for a in anchors), statistics.fmean(a.cy for a in anchors)
    spread = sum((a.cx - mean_x) ** 2 for a in anchors)
    if not spread:
        return 0.0
    angle = math.atan(sum((a.cx - mean_x) * (a.cy - mean_y) for a in anchors) / spread)
    return angle if abs(angle) <= MAX_SKEW_RADIANS else 0.0


def deskew(lines: list[OcrLine], angle: float | None = None) -> list[OcrLine]:
    """Rotates box positions so that the grid's header row is horizontal (photos are rarely straight)."""
    angle = skew_angle(lines) if angle is None else angle
    if not angle:
        return lines
    cos, sin = math.cos(angle), math.sin(angle)
    straightened = []
    for l in lines:
        cx, cy = l.cx * cos + l.cy * sin, -l.cx * sin + l.cy * cos
        half_w, half_h = (l.x1 - l.x0) / 2, l.height / 2
        straightened.append(replace(l, x0=cx - half_w, y0=cy - half_h, x1=cx + half_w, y1=cy + half_h,
                                    source_box=l.box))
    return straightened


def find_grid(lines: list[OcrLine]) -> Grid | None:
    """Locates the visit grid of the 'Grossesse actuelle' page from its column headers and row labels."""
    visits, months = _headers(lines)
    if len(visits) != 6 or len(months) != 3 or visits[-1].x0 >= months[0].x0:
        return None
    anchors = visits + months
    header_y = statistics.median(a.cy for a in anchors)
    if any(abs(a.cy - header_y) > 2 * a.height for a in anchors):
        return None
    width = statistics.median(b.x0 - a.x0 for a, b in zip(anchors, anchors[1:]))
    lefts = [a.x0 - 0.15 * width for a in anchors]
    columns = [(slot, left, right) for slot, left, right in zip(COLUMN_SLOTS, lefts, lefts[1:] + [lefts[-1] + width])]
    header_bottom = max(a.y1 for a in anchors)

    labels = sorted((l for l in lines if l.cx < lefts[0] and l.cy > header_bottom), key=lambda l: l.cy)
    if len(labels) < 3:
        return None
    pitch = statistics.median(b.cy - a.cy for a, b in zip(labels, labels[1:]))
    rows: dict[str, tuple[float, float]] = {}
    for field, (pattern, _label) in {**VISIT_ROWS, **LAB_ROWS}.items():
        index = next((i for i, l in enumerate(labels)
                      if re.match(f"(?:{pattern})" + r"(?:\s*\(.*\))?$", norm(l.text))), None)
        if index is None:
            continue
        cy = labels[index].cy
        top = (labels[index - 1].cy + cy) / 2 if index > 0 else cy - pitch / 2
        bottom = (labels[index + 1].cy + cy) / 2 if index + 1 < len(labels) else cy + pitch / 2
        rows[field] = (top, bottom)
    return Grid(columns, header_bottom, rows) if "visit_date" in rows else None


@dataclass
class _Page:
    """A read page: deskewed lines plus what is needed to locate a cell on the image that was read."""

    ref: str
    lines: list[OcrLine]
    scan: PageScan
    angle: float = 0.0

    def _to_image(self, x: float, y: float) -> tuple[float, float]:
        cos, sin = math.cos(self.angle), math.sin(self.angle)
        return x * cos - y * sin, x * sin + y * cos

    def _image_rect(self, rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = rect
        corners = [self._to_image(x, y) for x in (x0, x1) for y in (y0, y1)]
        return (min(c[0] for c in corners), min(c[1] for c in corners),
                max(c[0] for c in corners), max(c[1] for c in corners))

    def source(self, rect: tuple[float, float, float, float] | None = None, items: list[OcrLine] = (),
               **where) -> dict:
        """Field source: page_ref, the printed row/column, and bbox as fractions of the image that was read."""
        source = {"page_ref": self.ref, **where}
        width, height = self.scan.width, self.scan.height
        if width and height and (items or rect):
            if items:
                boxes = [l.box for l in items]
                x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
                x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            else:
                x0, y0, x1, y1 = self._image_rect(rect)
            source["bbox"] = [round(min(max(v / size, 0.0), 1.0), 4)
                              for v, size in ((x0, width), (y0, height), (x1, width), (y1, height))]
        return source

    def empty_cell(self, rect: tuple[float, float, float, float]) -> str:
        """Why a cell without OCR text has no value: verified blank, unread ink, outside the photo, or unchecked."""
        x0, y0, x1, y1 = self._image_rect(rect)
        width, height = self.scan.width, self.scan.height
        if width and height and (x0 < -FRAME_TOLERANCE * width or y0 < -FRAME_TOLERANCE * height
                                 or x1 > (1 + FRAME_TOLERANCE) * width or y1 > (1 + FRAME_TOLERANCE) * height):
            return "CELL_OUT_OF_FRAME"
        # Inset so that the printed cell borders are not taken for ink.
        cx0, cy0, cx1, cy1 = rect
        inset = (cx0 + 0.15 * (cx1 - cx0), cy0 + 0.25 * (cy1 - cy0), cx1 - 0.15 * (cx1 - cx0), cy1 - 0.25 * (cy1 - cy0))
        samples = self.samples(inset)
        if samples is None:
            return "OCR_NO_TEXT"
        return "CELL_INK_UNREAD" if self.scan.ink_share(samples) > MAX_BLANK_INK else "CELL_BLANK"

    def samples(self, rect: tuple[float, float, float, float]) -> list[list[int]] | None:
        """Grey levels of a rectangle of the straightened page, one thumbnail pixel apart, so that printed rules
        stay horizontal and vertical even on a tilted photo. None when there is no thumbnail."""
        scan = self.scan
        if scan.thumbnail is None or not scan.width or not scan.height:
            return None
        tw, th, data = scan.thumbnail
        fx, fy = tw / scan.width, th / scan.height
        x0, y0, x1, y1 = rect
        columns, rows = max(1, round((x1 - x0) * fx)), max(1, round((y1 - y0) * fy))
        grid = []
        for r in range(rows):
            y = y0 + (r + 0.5) * (y1 - y0) / rows
            row = []
            for c in range(columns):
                ix, iy = self._to_image(x0 + (c + 0.5) * (x1 - x0) / columns, y)
                row.append(data[min(th - 1, max(0, int(iy * fy))) * tw + min(tw - 1, max(0, int(ix * fx)))])
            grid.append(row)
        return grid


def _cell_rect(grid: Grid, field: str, left: float, right: float) -> tuple[float, float, float, float]:
    top, bottom = grid.rows[field]
    return left, max(top, grid.header_bottom), right, bottom


def _split_words(line: OcrLine) -> list[OcrLine]:
    """Words of a box, each placed in proportion to its characters (OCR gives one box per line, not per word)."""
    words = list(re.finditer(r"\S+", line.text))
    if len(words) < 2:
        return [line]
    size, width = len(line.text), line.x1 - line.x0
    pieces = []
    for word in words:
        a, b = word.start() / size, word.end() / size
        original = None
        if line.source_box:
            sx0, sy0, sx1, sy1 = line.source_box
            original = (sx0 + a * (sx1 - sx0), sy0, sx0 + b * (sx1 - sx0), sy1)
        pieces.append(OcrLine(word.group(), line.confidence, line.x0 + a * width, line.y0, line.x0 + b * width,
                              line.y1, source_box=original))
    return pieces


@dataclass
class Cells:
    """Text of each (slot, row) cell of one page. A box across a column rule ("62 64" read as one line) is split into
    words placed by position, and the cells it touches are flagged: values of two visits are never joined."""

    text: dict[tuple[str, str], list[OcrLine]]
    spanning: set[tuple[str, str]]
    weak: set[int]  # id() of split words without a digit, which alone do not make a column a visit


def assign_cells(lines: list[OcrLine], grid: Grid) -> Cells:
    text: dict[tuple[str, str], list[OcrLine]] = {}
    spanning: set[tuple[str, str]] = set()
    weak: set[int] = set()
    for line in lines:
        if line.cy <= grid.header_bottom:
            continue
        row = next((f for f, (top, bottom) in grid.rows.items() if top <= line.cy < bottom), None)
        if row is None:
            continue
        width = max(line.x1 - line.x0, 1e-6)
        touched = [slot for slot, left, right in grid.columns if min(line.x1, right) - max(line.x0, left) > SPAN_SHARE * width]
        pieces = [line]
        if len(touched) > 1:
            spanning.update((slot, row) for slot in touched)
            pieces = _split_words(line)
            weak.update(id(p) for p in pieces if not re.search(r"\d", p.text))
        for piece in pieces:
            slot = next((s for s, left, right in grid.columns if left <= piece.cx < right), None)
            if slot is not None:
                text.setdefault((slot, row), []).append(piece)
    for items in text.values():
        items.sort(key=lambda l: l.x0)
    return Cells(text, spanning, weak)


def _field(spec: schema.FieldSpec, *, status: str, raw_text: str | None = None, value=None,
           confidence: float | None = None, flags: list[str] | None = None, source: dict | None = None) -> dict:
    return {
        "raw_text": raw_text, "value": value, "unit": spec.unit,
        "confidence": None if confidence is None else round(confidence, 3),
        "field_status": status, "validation_flags": flags or [], "source": source,
        "verification": {"state": "UNVERIFIED", "by": None, "at": None}, "corrections": [],
    }


def _review(spec: schema.FieldSpec, flag: str, source: dict | None = None) -> dict:
    return _field(spec, status="NEEDS_REVIEW", flags=[flag], source=source)


SWAPS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "º": "0", "°": "0", "b": "6"})


def _numeric_variants(raw: str) -> tuple[list[str], list[str]]:
    """Clean-ups of a number cell. '|' is the cell border or a handwritten 1, so both readings are kept."""
    swapped = raw.translate(SWAPS)
    flags = ["OCR_CHARACTER_SUBSTITUTED"] if swapped != raw else []
    text = re.sub(r"\s*([/.\-])\s*", r"\1", swapped.replace(",", ".").replace("_", ""))
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text).strip()
    if "|" not in text:
        return [text], flags
    return [text.replace("|", "").strip(), text.replace("|", "1")], flags + ["OCR_CHARACTER_UNCLEAR"]


EXTRACT = {
    "date": (DATE_IN_TEXT, ""),
    "gestational_age": (schema.GESTATIONAL_AGE, ""),
    "decimal": (r"\d{2,3}(?:\.\d)?", r"(?:kg)?"),
    "int": (r"\d{1,2}", r"(?:cm)?"),
}


def _blank(spec: schema.FieldSpec, items: list[OcrLine], source: dict, empty: str = "OCR_NO_TEXT") -> dict | None:
    """Field for an empty cell or a dash; None when the cell holds something to read.

    Only a verified blank cell (CELL_BLANK) or a dash is NOT_PROVIDED; any other empty reading goes to review.
    """
    if not items:
        verified = empty == "CELL_BLANK" and not spec.required
        return _field(spec, status="NOT_PROVIDED" if verified else "NEEDS_REVIEW", flags=[empty], source=source)
    raw = " ".join(l.text for l in items).strip()
    if raw in DASHES:
        return _field(spec, status="NOT_PROVIDED", raw_text=raw, confidence=min(l.confidence for l in items),
                      flags=["MARKED_DASH"], source=source)
    return None


def _decide(spec: schema.FieldSpec, raw: str, confidence: float, readings: dict, flags: list[str],
            source: dict) -> dict:
    """KNOWN only for one confident reading without any doubt flag; otherwise NEEDS_REVIEW."""
    if len(readings) != 1:
        flag = "OCR_UNPARSED" if not readings else "OCR_CHARACTER_UNCLEAR"
        return _field(spec, status="NEEDS_REVIEW", raw_text=raw, confidence=confidence,
                      flags=list(dict.fromkeys(flags + [flag])), source=source)
    value, extra = next(iter(readings.items()))
    flags = flags + (["OCR_EXTRA_TEXT"] if extra else []) + (
        ["OCR_LOW_CONFIDENCE"] if confidence < MIN_KNOWN_CONFIDENCE else [])
    return _field(spec, status="NEEDS_REVIEW" if flags else "KNOWN", raw_text=raw, value=value,
                  confidence=confidence, flags=flags, source=source)


TEXT_KINDS = ("enum", "choice", "text")


def _read_value(spec: schema.FieldSpec, items: list[OcrLine], source: dict, empty: str = "OCR_NO_TEXT",
                flags: tuple[str, ...] = (), extract: tuple[str, str] | None = None) -> dict:
    """flags are doubts found before reading (e.g. text crossing a column rule); any flag sends the field to review.
    extract is (number pattern, unit suffix) when the kind's default does not fit the field (e.g. 0.93 g/L)."""
    blank = _blank(spec, items, source, empty)
    if blank:
        return blank
    raw = " ".join(l.text for l in items).strip()
    confidence = min(l.confidence for l in items)
    readings: dict = {}
    pattern, suffix = extract or EXTRACT.get(spec.kind, (r".+", ""))
    if spec.kind in TEXT_KINDS:
        variants, found = [raw.strip(" _|")], []
    else:
        # The unit written after the number is not read as digits: "g/l" must not become "g/1".
        body = re.sub(rf"\s*{suffix}\s*$", "", raw, flags=re.IGNORECASE) if suffix else raw
        variants, found = _numeric_variants(body)
    flags = list(flags) + found
    if spec.kind == "date" and any(re.fullmatch(r"\d{2}1\d{2}/\d{4}", t) for t in variants):
        # The first slash is often read as a 1: "05106/2025".
        variants = [re.sub(r"^(\d{2})1(\d{2}/)", r"\1/\2", t) for t in variants]
        flags = flags + ["OCR_CHARACTER_SUBSTITUTED"]
    if spec.kind == "gestational_age" and any(re.search(r"\d\.\d", t) for t in variants):
        # "28.5" or "28,3": decimal weeks or weeks.days? Reading 28 would drop the days.
        return _field(spec, status="NEEDS_REVIEW", raw_text=raw, confidence=confidence,
                      flags=list(dict.fromkeys(flags + ["GA_FORMAT_AMBIGUOUS"])), source=source)
    for text in variants:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is None:
            continue
        try:
            value = spec.parse(match.group(0))
        except schema.InvalidValue:
            continue
        readings.setdefault(value, not re.fullmatch(re.escape(match.group(0)) + r"\s*" + suffix, text, re.IGNORECASE))
    result = _decide(spec, raw, confidence, readings, flags, source)
    if spec.kind == "gestational_age" and result["value"] is not None:
        weeks, days = divmod(result["value"], 7)
        result["source"] = {**source, "normalization": {"from": "weeks+days", "to": "days", "weeks": weeks,
                                                        "days": days}}
    return result


BP_TEXT = r"(\d{2,3}(?:\.\d)?)/(\d{1,3}(?:\.\d)?)"


def _read_bp(items: list[OcrLine], source: dict, empty: str = "OCR_NO_TEXT",
             flags: tuple[str, ...] = ()) -> tuple[dict, dict]:
    systolic, diastolic = schema.FIELDS["systolic_bp_mmhg"], schema.FIELDS["diastolic_bp_mmhg"]
    if _blank(systolic, items, source, empty):
        return _blank(systolic, items, source, empty), _blank(diastolic, items, source, empty)
    raw = " ".join(l.text for l in items).strip()
    confidence = min(l.confidence for l in items)
    variants, found = _numeric_variants(raw)
    flags = list(flags) + found
    readings: dict = {}
    units: dict = {}
    for text in variants:
        # A date is never a TA: "10/05/2025", and "10/05" or "12/07", whose leading zero no TA is written with.
        if re.search(DATE_IN_TEXT, text) or re.fullmatch(r"\d{1,2}/0\d", text):
            flags = flags + ["BP_LOOKS_LIKE_DATE"]
            continue
        match = re.fullmatch(BP_TEXT, text)
        if match is None:
            continue
        high, low = float(match.group(1)), float(match.group(2))
        # "13/60" mixes cmHg and mmHg: a digit was lost ("113/60") and 130/60 would be a confident misread.
        if (high < 30) != (low < 30):
            flags = flags + ["BP_UNITS_MIXED"]
            continue
        try:
            pair = (systolic.parse(match.group(1)), diastolic.parse(match.group(2)))
        except schema.InvalidValue:
            continue
        # "11/12" (a date?) would give 110/120: the diastolic is always below the systolic.
        if pair[1] >= pair[0]:
            flags = flags + ["BP_ORDER_IMPLAUSIBLE"]
            continue
        readings.setdefault(pair, False)
        units.setdefault(pair, "cmHg" if high < 30 else "mmHg")
    pair = _decide(systolic, raw, confidence, readings, list(dict.fromkeys(flags)), source)
    values = next(iter(readings)) if pair["value"] is not None else (None, None)
    if pair["value"] is not None and units[values] == "cmHg":
        source = {**source, "normalization": {"from": "cmHg", "to": "mmHg", "factor": 10}}
    return tuple(_field(spec, status=pair["field_status"], raw_text=raw, value=value, confidence=confidence,
                        flags=list(pair["validation_flags"]), source=source)
                 for spec, value in ((systolic, values[0]), (diastolic, values[1])))


@dataclass
class Column:
    """One visit column of one page, with its fields even when empty, so that a blank column on one photo can
    contradict a visit read on another photo of the same grid.

    populated: OCR read text other than a dash in one of its rows. ink_only: no text was read, but a cell holds ink;
    such a column is not made a visit (the blank-cell check also sees ink in some empty cells), the page goes to review.
    """

    slot: str
    populated: bool
    fields: dict
    ink_only: bool = False


def read_columns(page: _Page, grid: Grid) -> list[Column]:
    found = assign_cells(page.lines, grid)
    columns = []
    for slot, left, right in grid.columns:
        cells = {f: found.text.get((slot, f), []) for f in VISIT_ROWS}
        rects = {f: _cell_rect(grid, f, left, right) for f in VISIT_ROWS if f in grid.rows}
        empty = {f: "OCR_TEXT_SPANS_COLUMNS" if (slot, f) in found.spanning else page.empty_cell(rects[f])
                 for f in rects if not cells[f]}
        written = any(" ".join(l.text for l in cells[f] if id(l) not in found.weak).strip() not in DASHES | {""}
                      for f in VISIT_EVIDENCE)
        ink_only = not written and any(empty.get(f) == "CELL_INK_UNREAD" for f in VISIT_EVIDENCE)

        def source(f):
            return page.source(rects[f], cells[f], row=VISIT_ROWS[f][1], column=schema.SLOTS[slot])

        def doubts(f):
            return ("OCR_TEXT_SPANS_COLUMNS",) if (slot, f) in found.spanning and cells[f] else ()

        fields = {}
        for spec in schema.ENCOUNTER_FIELDS:
            if spec.name in ("systolic_bp_mmhg", "diastolic_bp_mmhg"):
                continue
            if spec.name not in grid.rows:
                fields[spec.name] = _review(spec, "OCR_ROW_NOT_FOUND", page.source())
            else:
                fields[spec.name] = _read_value(spec, cells[spec.name], source(spec.name),
                                                empty.get(spec.name, "OCR_NO_TEXT"), doubts(spec.name))
        if "bp" in grid.rows:
            fields["systolic_bp_mmhg"], fields["diastolic_bp_mmhg"] = _read_bp(
                cells["bp"], source("bp"), empty.get("bp", "OCR_NO_TEXT"), doubts("bp"))
        else:
            for name in ("systolic_bp_mmhg", "diastolic_bp_mmhg"):
                fields[name] = _review(schema.FIELDS[name], "OCR_ROW_NOT_FOUND", page.source())
        columns.append(Column(slot, written, {spec.name: fields[spec.name] for spec in schema.ENCOUNTER_FIELDS},
                              ink_only))
    return columns


def read_visits(page: _Page, grid: Grid) -> list[dict]:
    """One encounter per populated column of one page, in column order."""
    return [{"encounter_type": "ANTENATAL", "slot": c.slot, "fields": c.fields}
            for c in read_columns(page, grid) if c.populated]


def _reading_kind(fv: dict) -> str:
    """value, written (text or ink that gave no value), blank (verified empty or a dash) or unchecked."""
    flags = fv["validation_flags"]
    if fv["value"] is not None:
        return "value"
    if "CELL_BLANK" in flags or "MARKED_DASH" in flags:
        return "blank"
    if fv["raw_text"] or fv["field_status"] == "ILLEGIBLE" or "CELL_INK_UNREAD" in flags:
        return "written"
    return "unchecked"


def _evidence(fv: dict) -> dict:
    item = {"page_ref": fv["source"]["page_ref"], "raw_text": fv["raw_text"], "value": fv["value"],
            "field_status": fv["field_status"], "confidence": fv["confidence"],
            "validation_flags": list(fv["validation_flags"])}
    if "bbox" in fv["source"]:
        item["bbox"] = fv["source"]["bbox"]
    return item


def merge_readings(spec: schema.FieldSpec, readings: list[dict]) -> dict:
    """One field from the pages that show it, in page order. Agreeing pages keep the best reading; different values,
    or a value (or unread writing) on one page and a verified blank on another, are a conflict for a person: no value
    is chosen. source.readings lists every page's reading, so the evidence is never overwritten."""
    if len(readings) == 1:
        return readings[0]
    kinds = [_reading_kind(r) for r in readings]
    values = {json.dumps(r["value"]) for r, kind in zip(readings, kinds) if kind == "value"}
    evidence = [_evidence(r) for r in readings]
    if len(values) > 1 or ("blank" in kinds and ("value" in kinds or "written" in kinds)):
        first = next(r for r, kind in zip(readings, kinds) if kind in ("value", "written"))
        source = {k: v for k, v in first["source"].items() if k != "normalization"}
        return _field(spec, status="NEEDS_REVIEW", flags=["CONFLICT_ACROSS_PAGES"],
                      source={**source, "readings": evidence})
    order = ("value", "written", "blank", "unchecked")
    best, _kind = min(zip(readings, kinds), key=lambda rk: (order.index(rk[1]), rk[0]["field_status"] != "KNOWN",
                                                            -(rk[0]["confidence"] or 0.0)))
    chosen = copy.deepcopy(best)
    chosen["source"] = {**chosen["source"], "readings": evidence}
    return chosen


def _value_beside(lines: list[OcrLine], label: OcrLine) -> list[OcrLine]:
    """Text written after 'label :' on the same line, in the label's own box or in the next box to its right."""
    _head, colon, tail = label.text.partition(":")
    if colon and tail.strip():
        return [OcrLine(tail.strip(), label.confidence, label.x1, label.y0, label.x1, label.y1, source_box=label.box)]
    right = sorted((l for l in lines if l is not label and l.x0 >= label.x1 - 2 and abs(l.cy - label.cy) < 0.6 * label.height
                    and l.x0 - label.x1 < 8 * label.height), key=lambda l: l.x0)
    return right[:1]


def _labelled(page: _Page, spec: schema.FieldSpec, pattern: str, row: str) -> dict:
    """Value written after a printed label. A missing label or an empty line is unread, so it goes to review."""
    label = next((l for l in page.lines if re.search(pattern, norm(l.text))), None)
    if label is None:
        return _review(spec, "OCR_LABEL_NOT_FOUND", page.source())
    items = _value_beside(page.lines, label)
    source = page.source(items=items or [label], row=row)
    if spec.kind == "date":
        return _read_value(spec, items, source)
    if not items:
        return _review(spec, "OCR_NO_TEXT", source)
    raw, confidence = items[0].text.strip(), items[0].confidence
    # The form's writing line is often read as underscores or dots around the value.
    cleaned = re.sub(r"\s+", " ", re.sub(r"[_…]+|\.{2,}", " ", raw)).strip(" .:")
    try:
        value = spec.parse(cleaned)
    except schema.InvalidValue:
        return _field(spec, status="NEEDS_REVIEW", raw_text=raw, confidence=confidence, flags=["OCR_UNPARSED"],
                      source=source)
    # No range or format rule catches a misread digit in a file number or a letter in a name.
    flags = (["OCR_CONFIRM_REQUIRED"] if spec.kind in ("digits", "code", "text") else []) + (
        ["OCR_LOW_CONFIDENCE"] if confidence < MIN_KNOWN_CONFIDENCE else [])
    return _field(spec, status="NEEDS_REVIEW" if flags else "KNOWN", raw_text=raw, value=value,
                  confidence=confidence, flags=flags, source=source)


def detect_section(lines: list[OcrLine]) -> tuple[str, str]:
    """(section, confidence) from the printed text: "high", "low" (one label or a tie) or "none" (unknown layout).

    A visit-like table on a page whose labels say delivery, postpartum or another section is uncertain, so that it
    never gives antenatal visits without a person's choice.
    """
    text = " ".join(norm(l.text) for l in lines)
    signs = {section: sum(bool(re.search(p, text)) for p in patterns) for section, patterns in SECTION_SIGNS.items()}
    if find_grid(lines):
        other = max(score for section, score in signs.items() if section != "current_pregnancy")
        if other >= 2 and other > signs["current_pregnancy"]:
            return "unknown", "low"
        return "current_pregnancy", "high"
    scores = sorted(((score, section) for section, score in signs.items()), reverse=True)
    (best, section), (second, _other) = scores[0], scores[1]
    if best == 0:
        return "unknown", "none"
    if best == second:
        return "unknown", "low"
    return section, "high" if best >= 2 else "low"


def _pii(pages: list[tuple[str, list[OcrLine]]]) -> list[dict]:
    found = []
    for page_ref, lines in pages:
        text = " ".join(norm(l.text) for l in lines)
        found += [{"page_ref": page_ref, "category": category, "action": "NOT_EXTRACTED"}
                  for pattern, category in PII_LABELS if re.search(pattern, text)]
    return found


def _demote(fv: dict, flag: str) -> None:
    if flag not in fv["validation_flags"]:
        fv["validation_flags"].append(flag)
    if fv["field_status"] == "KNOWN":
        fv["field_status"] = "NEEDS_REVIEW"


def cross_check(document_fields: dict, encounters: list[dict]) -> None:
    """Plausibility across fields: a misread digit usually breaks the DDR, the dates or the gestational age."""
    lmp = document_fields["last_menstrual_period"]["value"]
    previous = None
    for encounter in encounters:
        fields = encounter["fields"]
        visit = fields["visit_date"]["value"]
        if visit is None:
            continue
        if previous is not None and visit <= previous["value"]:
            _demote(fields["visit_date"], "DATE_ORDER")
            _demote(previous, "DATE_ORDER")
        previous = fields["visit_date"]
        if lmp is None:
            continue
        elapsed = (date.fromisoformat(visit) - date.fromisoformat(lmp)).days
        if not 0 <= elapsed <= 315:
            _demote(fields["visit_date"], "DATE_INCONSISTENT_WITH_DDR")
            _demote(document_fields["last_menstrual_period"], "DATE_INCONSISTENT_WITH_DDR")
        elif fields["gestational_age_days"]["value"] is not None and \
                abs(elapsed - fields["gestational_age_days"]["value"]) > MAX_GA_GAP_DAYS:
            _demote(fields["gestational_age_days"], "GA_INCONSISTENT_WITH_DDR")
            _demote(fields["visit_date"], "GA_INCONSISTENT_WITH_DDR")
    # Each visit implies a conception date (visit minus GA); a misread GA or date stands out from the others.
    implied = [(date.fromisoformat(e["fields"]["visit_date"]["value"]).toordinal()
                - e["fields"]["gestational_age_days"]["value"], e["fields"]) for e in encounters
               if e["fields"]["visit_date"]["value"] is not None
               and e["fields"]["gestational_age_days"]["value"] is not None]
    if len(implied) < 2:
        return
    # With two visits there is no majority, so a disagreement flags both.
    reference = statistics.median_low(day for day, _fields in implied) if len(implied) >= 3 else None
    for day, fields in implied:
        other = reference if reference is not None else next(d for d, f in implied if f is not fields)
        if abs(day - other) > MAX_GA_GAP_DAYS:
            _demote(fields["gestational_age_days"], "GA_INCONSISTENT_WITH_VISITS")
            _demote(fields["visit_date"], "GA_INCONSISTENT_WITH_VISITS")


def _page_entry(ref: str, scan: PageScan | PageUnread, section: str, confidence: str | None,
                hint: str | None) -> dict:
    """The draft's pages[] item: section and how it was decided, why a person must look, and the image geometry."""
    entry = {"page_ref": ref, "section": section, "section_source": "reviewer" if hint else "detected",
             "section_confidence": confidence, "read": isinstance(scan, PageScan)}
    if isinstance(scan, PageUnread):
        if scan.retake_reason:
            entry.update(retake_reason=scan.retake_reason, retake_message=RETAKE_REASONS[scan.retake_reason])
        review = scan.code
    else:
        review = {"none": "UNKNOWN_LAYOUT", "low": "SECTION_UNCERTAIN"}.get(confidence)
        if scan.width and scan.height:
            entry["image"] = {"width": scan.width, "height": scan.height, "exif_transposed": scan.exif_transposed,
                              "rotated_ccw": scan.rotated_ccw, "scale": round(scan.scale, 4)}
        if scan.warnings:
            entry["warnings"] = list(scan.warnings)
    # A section chosen by the reviewer settles the page; its unread fields still go to review one by one.
    entry["review_reason"] = None if hint else review
    return entry


def build_draft(pages: list[tuple[str, PageScan | PageUnread | list[OcrLine]]], *, extractor: str,
                extractor_version: str, min_confidence: float = MIN_KNOWN_CONFIDENCE,
                section_hints: list[str | None] | None = None) -> dict:
    """min_confidence is the engine's own KNOWN threshold; it can only be stricter than MIN_KNOWN_CONFIDENCE.

    section_hints, aligned with pages, are sections chosen by a reviewer; they replace detection for that page.
    """
    hints = list(section_hints) if section_hints else [None] * len(pages)
    entries, read = [], []
    for (ref, scan), hint in zip(pages, hints):
        scan = PageScan(scan) if isinstance(scan, list) else scan
        if isinstance(scan, PageUnread):
            entries.append(_page_entry(ref, scan, hint or "unknown", None, hint))
            continue
        # NFKC folds the full-width colon some models emit ("：") and "º" into plain characters.
        lines = [replace(l, text=unicodedata.normalize("NFKC", l.text)) for l in scan.lines]
        angle = skew_angle(lines)
        page = _Page(ref, deskew(lines, angle), scan, angle)
        section, confidence = (hint, None) if hint else detect_section(page.lines)
        entries.append(_page_entry(ref, scan, section, confidence, hint))
        read.append((page, section, entries[-1]))

    # A section that no page shows is unavailable; when a page could not be read, it may be on that page.
    missing = "PAGE_NOT_READ" if any(not e["read"] for e in entries) else "SECTION_NOT_CAPTURED"
    found: dict[str, list[dict]] = {spec.name: [] for spec in schema.DOCUMENT_FIELDS}
    for page, section, _entry in read:
        if section != "cover":
            continue
        found["registry_file_number"].append(_labelled(page, schema.FIELDS["registry_file_number"],
                                                       r"n\s*[°o]?\s*de\s*la\s*fiche", "N° de la fiche"))
        found["facility_name"].append(_labelled(page, schema.FIELDS["facility_name"],
                                                r"nom\s*de\s*l.?\s*etablissement", "Nom de l'établissement sanitaire"))
        # The specimen cover has no midwife code; the pink booklet has it as "CM: …". Only a line that starts with
        # the label counts, so that "Code postal" or a code elsewhere on the page never becomes a link key.
        found["midwife_patient_code"].append(_labelled(page, schema.FIELDS["midwife_patient_code"],
                                                       r"^(?:code(?!\s*postal)(?:\s+\w+){0,2}|cm)\s*:", "Code"))

    # Visits: one encounter per slot populated on at least one page; the same slot on several pages is merged.
    columns: dict[str, list[Column]] = {}
    no_grid = []
    for page, section, entry in read:
        if section != "current_pregnancy":
            continue
        found["last_menstrual_period"].append(_labelled(page, schema.FIELDS["last_menstrual_period"], r"^ddr\b", "DDR"))
        grid = find_grid(page.lines)
        if grid is None:
            entry["grid"] = {"found": False}
            no_grid.append(page)
            # Its visits are unread: a person looks at the page (retake, or confirm its section) before registration.
            if entry["review_reason"] is None and entry["section_source"] == "detected":
                entry["review_reason"] = "GRID_NOT_FOUND"
            continue
        page_columns = read_columns(page, grid)
        entry["grid"] = {"found": True, "visit_columns": [c.slot for c in page_columns if c.populated],
                         "unread_columns": [c.slot for c in page_columns if c.ink_only],
                         "blank_columns": [c.slot for c in page_columns if not c.populated and not c.ink_only]}
        # A column with ink but no text may be a visit OCR missed, or a smudge: a person looks at the photo.
        if entry["grid"]["unread_columns"] and entry["review_reason"] is None and entry["section_source"] == "detected":
            entry["review_reason"] = "GRID_COLUMN_UNREAD"
        for column in page_columns:
            columns.setdefault(column.slot, []).append(column)

    document_fields = {spec.name: merge_readings(spec, found[spec.name]) if found[spec.name] else _review(spec, missing)
                       for spec in schema.DOCUMENT_FIELDS}
    encounters = [{"encounter_type": "ANTENATAL", "slot": slot,
                   "fields": {spec.name: merge_readings(spec, [c.fields[spec.name] for c in columns[slot]])
                              for spec in schema.ENCOUNTER_FIELDS}}
                  for slot in COLUMN_SLOTS if any(c.populated for c in columns.get(slot, ()))]
    # A grid page whose table was not found still holds visits: each such page opens one manual visit sourced to it,
    # even when other pages gave visits, so that no unread page is hidden behind the read ones.
    # Other pages (cover, history, delivery, postpartum, newborn) never give an antenatal visit.
    encounters += [_manual_encounter("OCR_GRID_NOT_FOUND", page.source()) for page in no_grid]
    cross_check(document_fields, encounters)
    from . import extended, ocr_extended

    ext = ocr_extended.build_extended(read, {e["slot"] for e in encounters})
    draft_extended = {"extended": ext} if len(ext) > 1 else {}
    degraded = {page.ref for page, _section, _entry in read if page.scan.warnings}
    for fv in [*document_fields.values(), *(fv for e in encounters for fv in e["fields"].values()),
               *(fv for *_where, fv in extended.iter_fields(draft_extended))]:
        if fv["field_status"] == "KNOWN" and fv["confidence"] is not None and fv["confidence"] < min_confidence:
            _demote(fv, "OCR_LOW_CONFIDENCE")
        if fv["source"] and fv["source"]["page_ref"] in degraded:
            _demote(fv, "PHOTO_QUALITY_LOW")
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "layout_id": schema.LAYOUT_ID,
        "notes": "OCR local sur la mise en page du spécimen (décision D7 en attente) : toute valeur incertaine "
                 "nécessite une validation humaine.",
        "extraction": {"extractor": extractor, "extractor_version": extractor_version,
                       "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()},
        "pages": entries,
        "pii_detected": _pii([(page.ref, page.lines) for page, _section, _entry in read]),
        "document_fields": document_fields,
        "encounters": encounters,
        **draft_extended,
    }


def _manual_encounter(flag: str, source: dict | None) -> dict:
    return {"encounter_type": "ANTENATAL", "slot": "MANUAL",
            "fields": {spec.name: _review(spec, flag, source) for spec in schema.ENCOUNTER_FIELDS}}


class InProcessReader:
    """Preprocessing and OCR in this process, without a timeout (evaluation tool, tests); the server uses
    dayone/ocr_process.py. The prepared image lives in a temporary folder deleted after each page."""

    def __init__(self, engine, options=None):
        self.engine = engine
        self.options = options

    def read(self, path: Path, *, force: bool = False, timeout: float | None = None) -> PageScan:
        """timeout is not enforced here (no separate process to stop); the server's OCR process pool enforces it."""
        from . import ocr_preprocess

        with tempfile.TemporaryDirectory(prefix="dayone-ocr-") as work_dir:
            return ocr_preprocess.scan_page(self.engine, Path(path), Path(work_dir), force=force,
                                            options=self.options or ocr_preprocess.DEFAULT_OPTIONS)


def _unread(exc: Exception) -> PageUnread:
    if isinstance(exc, PhotoRejected):
        return PageUnread("PHOTO_UNUSABLE", exc.reason)
    if isinstance(exc, ExtractionError) and exc.code == "OCR_TIMEOUT":
        return PageUnread("OCR_TIMEOUT")
    return PageUnread("OCR_FAILED")


def extract_draft(engine, pages: list[tuple[str, Path]], *, section_hints: list[str | None] | None = None,
                  read_page=None, is_current=None, deadline: float | None = None, progress=None,
                  monotonic=time.monotonic) -> dict:
    """Reads resolved page files one by one, then builds the draft.

    read_page(path, force=...) returns a PageScan or raises; force (a reviewer chose the section) reads a photo
    that failed the photo checks instead of rejecting it. is_current() is asked between pages so that a
    replaced photo or a new section choice stops the run. A page that cannot be read does not stop the others;
    when no page can be read, the document fails with the per-page reasons.

    deadline (a monotonic() time) bounds the whole document: read_page also gets timeout=<seconds left>, and pages
    not started in time are OCR_TIMEOUT. progress(done, total, outcome) is called after each page; outcome is "read"
    or the reason the page was not read.
    """
    read_page = read_page or InProcessReader(engine).read
    hints = list(section_hints) if section_hints else [None] * len(pages)
    scans: list[tuple[str, PageScan | PageUnread]] = []
    for index, ((page_ref, path), hint) in enumerate(zip(pages, hints)):
        if is_current is not None and not is_current():
            raise ExtractionCancelled()
        started = monotonic()
        options = {"force": bool(hint)}
        if deadline is not None:
            options["timeout"] = deadline - started
        if options.get("timeout", 1) <= 0:
            scan = PageUnread("OCR_TIMEOUT")
        else:
            try:
                scan = read_page(path, **options)
            except ExtractionError as exc:
                if exc.code == "OCR_DEPENDENCY_MISSING":
                    raise
                scan = _unread(exc)
            except (ExtractorUnavailable, ExtractionCancelled):
                raise
            except Exception as exc:
                # The type only: messages may quote page text.
                log.error("local OCR failed on one page (%s)", type(exc).__name__)
                scan = PageUnread("OCR_FAILED")
        scans.append((page_ref, scan))
        outcome = "read" if isinstance(scan, PageScan) else scan.retake_reason or scan.code
        log.info("OCR page %d/%d: %s in %.1f s", index + 1, len(pages), outcome, monotonic() - started)
        if progress is not None:
            progress(index + 1, len(pages), outcome)
    unread = [(i, scan) for i, (_ref, scan) in enumerate(scans) if isinstance(scan, PageUnread)]
    if len(unread) == len(scans):
        _fail(unread)
    draft = build_draft(scans, extractor=engine.name, extractor_version=f"{engine.version}+{PARSER_VERSION}",
                        min_confidence=max(MIN_KNOWN_CONFIDENCE, getattr(engine, "known_confidence", 0.0)),
                        section_hints=hints)
    problems = schema.validate_draft(draft)
    if problems:
        raise ExtractionError("INVALID_LIVE_OCR_DRAFT", problems[0])
    return draft


def _fail(unread: list[tuple[int, PageUnread]]) -> None:
    pages = [{"index": i, "reason": scan.retake_reason or scan.code} for i, scan in unread]
    if all(scan.retake_reason for _i, scan in unread):
        details = " ; ".join(f"page {i + 1} : {RETAKE_REASONS[scan.retake_reason]}" for i, scan in unread)
        raise ExtractionError("RETAKE_REQUIRED", f"Photo inutilisable ({details}) Demandez une reprise.", pages=pages)
    if any(scan.code == "OCR_TIMEOUT" for _i, scan in unread):
        raise ExtractionError("OCR_TIMEOUT", "La lecture a dépassé le temps maximal : demandez une reprise de la "
                                             "photo ou saisissez les données à la main.", pages=pages)
    raise ExtractionError("OCR_FAILED", "L'OCR local a échoué sur ces pages : saisie manuelle ou reprise de la photo.",
                          pages=pages)
