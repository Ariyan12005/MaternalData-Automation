"""Parser of dayone/live_ocr.py on synthetic OCR boxes (mocked OCR: no engine, no image is read).

Real OCR results on the specimen pages are in docs/ocr.md and tools/ocr_specimen_eval.py, not here. Every name and
number below is invented; the tests check that names never reach the draft.
"""

import json
import math
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from dayone import live_ocr, ocr_engines, schema
from dayone.extraction import ExtractionCancelled, ExtractionError, PhotoRejected
from dayone.live_ocr import PageScan, PageUnread
from dayone.ocr_engines import OcrLine

PAGE = "data/Paper Registry/dossiers_specimen_10_patientes-03.png"
COVER = "data/Paper Registry/dossiers_specimen_10_patientes-01.png"
HEADERS = ("Visite 1", "Visite 2", "Visite 3", "Visite 1", "Visite 2", "Visite 3", "7ème mois", "8ème mois",
           "9ème mois")
ROWS = (("Venue le", "visit_date"), ("Age probable", "gestational_age_days"), ("Poids (kg)", "weight_kg"),
        ("TA", "bp"), ("HU (cm)", "fundal_height_cm"), ("Syphilis (TPHA/VDRL)", "syphilis_test"),
        ("Sérologie VIH", "hiv_test"), ("Examen fait par", "staff"))
STAFF = "Dr Inventé Exemple"
# Three visits consistent with DDR 12/04/2025: 4, 12 and 28 weeks.
VISITS = {
    "T1_V1": {"visit_date": "10/05/2025", "gestational_age_days": "4 SA", "weight_kg": "60.5", "bp": "12/7",
              "fundal_height_cm": "-", "syphilis_test": "Neg", "hiv_test": "Neg", "staff": STAFF},
    "T2_V1": {"visit_date": "05/07/2025", "gestational_age_days": "12 SA", "weight_kg": "62", "bp": "11/7",
              "fundal_height_cm": "12", "syphilis_test": "-", "hiv_test": "-"},
    "M7": {"visit_date": "25/10/2025", "gestational_age_days": "28 SA", "weight_kg": "68.4", "bp": "120/80",
           "fundal_height_cm": "28"},
}


def box(text: str, x: float, y: float, confidence: float = 0.99, width: float | None = None) -> OcrLine:
    width = width or 10 * len(text)
    return OcrLine(text, confidence, x, y - 10, x + width, y + 10)


def grid_page(visits: dict, *, ddr: str | None = "DDR : 12/04/2025", headers=HEADERS, angle: float = 0.0,
              confidence: float = 0.99) -> list[OcrLine]:
    """Boxes laid out like the specimen 'Grossesse actuelle' page; a cell value may be (text, confidence)."""
    lines = [box("Grossesse actuelle", 50, 80)] + ([box(ddr, 50, 140)] if ddr else [])
    lines += [box(text, 300 + 100 * i, 200, width=70) for i, text in enumerate(headers)]
    row_y = {field: 260 + 40 * i for i, (_label, field) in enumerate(ROWS)}
    lines += [box(label, 40, row_y[field], width=200) for label, field in ROWS]
    for slot, cells in visits.items():
        column = live_ocr.COLUMN_SLOTS.index(slot)
        for field, value in cells.items():
            text, conf = value if isinstance(value, tuple) else (value, confidence)
            lines.append(box(text, 305 + 100 * column, row_y[field], conf, width=60))
    if angle:
        cos, sin = math.cos(angle), math.sin(angle)
        lines = [replace(l, x0=cx - (l.x1 - l.x0) / 2, x1=cx + (l.x1 - l.x0) / 2, y0=cy - l.height / 2,
                         y1=cy + l.height / 2)
                 for l in lines for cx, cy in [(l.cx * cos - l.cy * sin, l.cx * sin + l.cy * cos)]]
    return lines


def one_visit(**overrides) -> dict:
    cells = {k: v for k, v in VISITS["T1_V1"].items() if k != "staff"}
    cells.update(overrides)
    return {"T1_V1": {k: v for k, v in cells.items() if v is not None}}


def page_scan(lines, *, width=1300, height=700, ink=(), warnings=()) -> PageScan:
    """A read page: the image is white paper (thumbnail at full scale) with dark ink only in the `ink` boxes."""
    pixels = bytearray([235]) * (width * height)
    for x0, y0, x1, y1 in ink:
        for y in range(int(y0), int(y1)):
            pixels[y * width + int(x0):y * width + int(x1)] = bytes(int(x1) - int(x0))
    return PageScan(list(lines), width=width, height=height, thumbnail=(width, height, bytes(pixels)),
                    warnings=list(warnings))


def draft_of(*pages, **kwargs) -> dict:
    """Pages may be box lists (wrapped as a white page scan), PageScan or PageUnread objects."""
    refs = [PAGE, COVER, "data/Paper Registry/x.png"]
    pages = [page_scan(p) if isinstance(p, list) else p for p in pages]
    draft = live_ocr.build_draft([(refs[i], scan) for i, scan in enumerate(pages)], extractor="paddleocr",
                                 extractor_version="test", **kwargs)
    assert schema.validate_draft(draft) == [], schema.validate_draft(draft)
    return draft


def cell_box(slot: str, field: str) -> tuple[int, int, int, int]:
    """Where grid_page writes a cell value (see its layout)."""
    row = [f for _label, f in ROWS].index(field)
    x, y = 305 + 100 * live_ocr.COLUMN_SLOTS.index(slot), 260 + 40 * row
    return x, y - 10, x + 60, y + 10


def pen_stroke(slot: str, field: str) -> tuple[int, int, int, int]:
    """A short handwritten stroke inside a cell, covering a small part of it."""
    x0, y0, _x1, _y1 = cell_box(slot, field)
    return x0 + 10, y0 + 7, x0 + 40, y0 + 13


def fields_by_slot(draft: dict) -> dict:
    return {e["slot"]: e["fields"] for e in draft["encounters"]}


class GridTest(unittest.TestCase):
    def test_clear_page_gives_one_encounter_per_visit_column(self):
        draft = draft_of(grid_page(VISITS))
        visits = fields_by_slot(draft)
        self.assertEqual(list(visits), ["T1_V1", "T2_V1", "M7"])
        values = {slot: {name: f["value"] for name, f in fields.items()} for slot, fields in visits.items()}
        self.assertEqual(values["T1_V1"], {
            "visit_date": "2025-05-10", "gestational_age_days": 28, "weight_kg": 60.5, "systolic_bp_mmhg": 120,
            "diastolic_bp_mmhg": 70, "fundal_height_cm": None, "syphilis_test": "NEGATIVE", "hiv_test": "NEGATIVE"})
        self.assertEqual((values["T2_V1"]["gestational_age_days"], values["M7"]["systolic_bp_mmhg"]), (84, 120))
        [page] = draft["pages"]
        self.assertEqual({k: page[k] for k in ("page_ref", "section", "section_source", "section_confidence",
                                               "read", "review_reason")},
                         {"page_ref": PAGE, "section": "current_pregnancy", "section_source": "detected",
                          "section_confidence": "high", "read": True, "review_reason": None})
        self.assertEqual(draft["document_fields"]["last_menstrual_period"]["value"], "2025-04-12")
        bp = visits["T1_V1"]["systolic_bp_mmhg"]
        self.assertEqual(bp["field_status"], "KNOWN")
        self.assertEqual({k: bp["source"][k] for k in ("page_ref", "row", "column")},
                         {"page_ref": PAGE, "row": "TA", "column": schema.SLOTS["T1_V1"]})

    def test_field_locations_are_fractions_of_the_image(self):
        visits = fields_by_slot(draft_of(grid_page(VISITS)))
        x0, y0, x1, y1 = cell_box("T1_V1", "bp")
        self.assertEqual(visits["T1_V1"]["systolic_bp_mmhg"]["source"]["bbox"],
                         [round(x0 / 1300, 4), round(y0 / 700, 4), round(x1 / 1300, 4), round(y1 / 700, 4)])
        # An empty cell is located by its row and column: the reviewer sees where to look.
        blank = visits["M7"]["syphilis_test"]["source"]["bbox"]
        self.assertTrue(0.65 < blank[0] < blank[2] < 0.8 and 0.6 < blank[1] < blank[3] < 0.7, blank)

    def test_locations_on_a_tilted_photo_are_where_the_text_was_read(self):
        tilted = grid_page(VISITS, angle=math.radians(4))
        original = next(l for l in tilted if l.text == "68.4")
        weight = fields_by_slot(draft_of(page_scan(tilted, height=800)))["M7"]["weight_kg"]
        self.assertEqual(weight["source"]["bbox"], [round(original.x0 / 1300, 4), round(original.y0 / 800, 4),
                                                    round(original.x1 / 1300, 4), round(original.y1 / 800, 4)])

    def test_dash_and_verified_blank_cells_are_not_provided(self):
        visits = fields_by_slot(draft_of(grid_page(VISITS)))
        dash, blank = visits["T1_V1"]["fundal_height_cm"], visits["M7"]["syphilis_test"]
        self.assertEqual((dash["field_status"], dash["validation_flags"]), ("NOT_PROVIDED", ["MARKED_DASH"]))
        self.assertEqual((blank["field_status"], blank["validation_flags"]), ("NOT_PROVIDED", ["CELL_BLANK"]))

    def test_empty_cells_that_cannot_be_verified_go_to_review(self):
        cases = [
            ("no image to check", PageScan(grid_page(VISITS)), "OCR_NO_TEXT"),
            ("ink that OCR did not read", page_scan(grid_page(VISITS), ink=[pen_stroke("M7", "syphilis_test")]),
             "CELL_INK_UNREAD"),
            ("row below the photo", page_scan(grid_page(VISITS), height=470), "CELL_OUT_OF_FRAME"),
        ]
        for label, scan, flag in cases:
            with self.subTest(label):
                syphilis = fields_by_slot(draft_of(scan))["M7"]["syphilis_test"]
                self.assertEqual((syphilis["field_status"], syphilis["validation_flags"]), ("NEEDS_REVIEW", [flag]))

    def test_unread_ink_in_an_empty_column_sends_the_page_to_review_without_a_phantom_visit(self):
        page = page_scan(grid_page(VISITS), ink=[pen_stroke("M8", "weight_kg")])
        draft = draft_of(page)
        self.assertEqual(list(fields_by_slot(draft)), ["T1_V1", "T2_V1", "M7"])
        entry = draft["pages"][0]
        self.assertEqual((entry["review_reason"], entry["grid"]["unread_columns"]), ("GRID_COLUMN_UNREAD", ["M8"]))
        self.assertNotIn("M8", entry["grid"]["blank_columns"])
        self.assertEqual(schema.blocking_fields(draft)[0]["reason"], "GRID_COLUMN_UNREAD")
        # Once a person has looked at the page and confirmed its section, the column no longer blocks.
        confirmed = draft_of(page, section_hints=["current_pregnancy"])
        self.assertEqual((confirmed["pages"][0]["review_reason"], list(fields_by_slot(confirmed))),
                         (None, ["T1_V1", "T2_V1", "M7"]))

    def test_column_of_dashes_is_not_a_visit(self):
        page = grid_page({**VISITS, "T1_V2": {"visit_date": "-", "weight_kg": "—", "bp": "-"}})
        self.assertNotIn("T1_V2", fields_by_slot(draft_of(page)))

    def test_misread_headers_and_a_tilted_photo_still_give_the_grid(self):
        headers = ("Visite 1", "Visite 22", "Viste 3", "Visite 1", "Visite 2", "Visite 3", "7erne mois", "8ème mois",
                   "Béme mois")
        tilted = grid_page(VISITS, headers=headers, angle=math.radians(6))
        self.assertIsNone(live_ocr.find_grid(tilted), "without deskew the header row is not level")
        visits = fields_by_slot(draft_of(tilted))
        self.assertEqual(list(visits), ["T1_V1", "T2_V1", "M7"])
        self.assertEqual(visits["M7"]["weight_kg"]["value"], 68.4)

    def test_row_label_alone_is_not_a_visit_header(self):
        page = grid_page(VISITS) + [box("Visites", 300, 200)]
        self.assertEqual(list(fields_by_slot(draft_of(page))), ["T1_V1", "T2_V1", "M7"])


class CellReadingTest(unittest.TestCase):
    def read(self, field: str, text, **kwargs) -> dict:
        visits = fields_by_slot(draft_of(grid_page(one_visit(**{field: text})), **kwargs))
        name = {"bp": "systolic_bp_mmhg"}.get(field, field)
        return visits["T1_V1"][name]

    def test_uncertain_readings_go_to_review(self):
        cases = [
            # field, OCR text, status, suggested value, flag
            ("weight_kg", "|75.2", "NEEDS_REVIEW", None, "OCR_CHARACTER_UNCLEAR"),  # 75.2 or 175.2
            ("bp", "|139/89", "NEEDS_REVIEW", 139, "OCR_CHARACTER_UNCLEAR"),  # 1139 is not a pressure
            ("bp", "13/60", "NEEDS_REVIEW", None, "BP_UNITS_MIXED"),
            ("bp", "10/05/2025", "NEEDS_REVIEW", None, "OCR_UNPARSED"),
            ("fundal_height_cm", "3l", "NEEDS_REVIEW", 31, "OCR_CHARACTER_SUBSTITUTED"),
            ("weight_kg", "7º.7", "NEEDS_REVIEW", 70.7, "OCR_CHARACTER_SUBSTITUTED"),
            ("weight_kg", "78.", "NEEDS_REVIEW", 78, "OCR_EXTRA_TEXT"),
            ("weight_kg", ("60.5", 0.80), "NEEDS_REVIEW", 60.5, "OCR_LOW_CONFIDENCE"),
            ("visit_date", "10105/2025", "NEEDS_REVIEW", "2025-05-10", "OCR_CHARACTER_SUBSTITUTED"),
            ("hiv_test", "Ney", "NEEDS_REVIEW", None, "OCR_UNPARSED"),
            ("visit_date", None, "NEEDS_REVIEW", None, "CELL_BLANK"),  # required
        ]
        for field, text, status, value, flag in cases:
            with self.subTest(field=field, text=text):
                result = self.read(field, text)
                self.assertEqual((result["field_status"], result["value"]), (status, value))
                self.assertIn(flag, result["validation_flags"])

    def test_digits_split_by_a_space_are_joined(self):
        result = self.read("weight_kg", "6 0.5")
        self.assertEqual((result["field_status"], result["value"]), ("KNOWN", 60.5))

    def test_engine_threshold_is_stricter_than_the_default(self):
        self.assertEqual(self.read("weight_kg", ("60.5", 0.95))["field_status"], "KNOWN")
        strict = self.read("weight_kg", ("60.5", 0.95), min_confidence=0.97)
        self.assertEqual((strict["field_status"], strict["value"], strict["validation_flags"]),
                         ("NEEDS_REVIEW", 60.5, ["OCR_LOW_CONFIDENCE"]))


class CrossCheckTest(unittest.TestCase):
    def test_misread_age_stands_out_from_the_other_visits_without_a_ddr(self):
        visits = {slot: dict(cells) for slot, cells in VISITS.items()}
        visits["T2_V1"]["gestational_age_days"] = "18 SA"
        result = fields_by_slot(draft_of(grid_page(visits, ddr=None)))
        flagged = {slot for slot, f in result.items()
                   if "GA_INCONSISTENT_WITH_VISITS" in f["gestational_age_days"]["validation_flags"]}
        self.assertEqual(flagged, {"T2_V1"})
        self.assertEqual(result["M7"]["gestational_age_days"]["field_status"], "KNOWN")

    def test_two_disagreeing_visits_are_both_flagged(self):
        visits = {"T1_V1": dict(VISITS["T1_V1"]), "T2_V1": dict(VISITS["T2_V1"], gestational_age_days="18 SA")}
        result = fields_by_slot(draft_of(grid_page(visits, ddr=None)))
        self.assertEqual({s: f["gestational_age_days"]["field_status"] for s, f in result.items()},
                         {"T1_V1": "NEEDS_REVIEW", "T2_V1": "NEEDS_REVIEW"})

    def test_age_and_dates_are_checked_against_the_ddr(self):
        result = fields_by_slot(draft_of(grid_page(one_visit(gestational_age_days="9 SA"))))
        self.assertIn("GA_INCONSISTENT_WITH_DDR", result["T1_V1"]["gestational_age_days"]["validation_flags"])
        early = draft_of(grid_page(one_visit(visit_date="10/03/2025", gestational_age_days="-")))
        self.assertIn("DATE_INCONSISTENT_WITH_DDR", fields_by_slot(early)["T1_V1"]["visit_date"]["validation_flags"])
        self.assertEqual(early["document_fields"]["last_menstrual_period"]["field_status"], "NEEDS_REVIEW")

    def test_visit_dates_must_increase_across_columns(self):
        visits = {slot: dict(cells) for slot, cells in VISITS.items()}
        visits["T2_V1"]["visit_date"] = "05/05/2025"
        result = fields_by_slot(draft_of(grid_page(visits)))
        for slot in ("T1_V1", "T2_V1"):
            self.assertIn("DATE_ORDER", result[slot]["visit_date"]["validation_flags"])


def pages_draft(*pages, hints=None) -> dict:
    """A document of several read pages (box lists on white paper) named like WhatsApp media."""
    scans = [(f"whatsapp-media/{i}.jpg", page_scan(p) if isinstance(p, list) else p) for i, p in enumerate(pages)]
    draft = live_ocr.build_draft(scans, extractor="paddleocr", extractor_version="test", section_hints=hints)
    assert schema.validate_draft(draft) == [], schema.validate_draft(draft)
    return draft


def straddling(text: str, field: str, left_slot: str) -> OcrLine:
    """One OCR box laid across the rule between left_slot's column and the next one (half in each)."""
    row = [f for _label, f in ROWS].index(field)
    rule = 285 + 100 * (live_ocr.COLUMN_SLOTS.index(left_slot) + 1)
    return box(text, rule - 45, 260 + 40 * row, width=90)


def blocking_of(draft: dict) -> set:
    return {(b["slot"], b["field"], b["reason"]) for b in schema.blocking_fields(draft) if b["scope"] == "encounter"}


class MultiVisitTest(unittest.TestCase):
    def test_each_column_keeps_its_own_date_and_values(self):
        visits = {**VISITS, "T2_V2": {"visit_date": "02/08/2025", "weight_kg": "64", "bp": "11/6"}}
        result = fields_by_slot(draft_of(grid_page(visits)))
        self.assertEqual({slot: f["visit_date"]["value"] for slot, f in result.items()},
                         {"T1_V1": "2025-05-10", "T2_V1": "2025-07-05", "T2_V2": "2025-08-02", "M7": "2025-10-25"})
        self.assertEqual({slot: f["weight_kg"]["value"] for slot, f in result.items()},
                         {"T1_V1": 60.5, "T2_V1": 62, "T2_V2": 64, "M7": 68.4})
        for slot, fields in result.items():
            with self.subTest(slot):
                self.assertEqual({f["source"]["column"] for f in fields.values()}, {schema.SLOTS[slot]})

    def test_text_across_a_column_rule_is_split_and_reviewed_not_joined(self):
        page = grid_page(VISITS) + [straddling("62 64", "weight_kg", "T2_V2")]
        result = fields_by_slot(draft_of(page))
        for slot, value in (("T2_V2", 62), ("T2_V3", 64)):
            with self.subTest(slot):
                weight = result[slot]["weight_kg"]
                self.assertEqual((weight["value"], weight["field_status"]), (value, "NEEDS_REVIEW"))
                self.assertIn("OCR_TEXT_SPANS_COLUMNS", weight["validation_flags"])
                # Populated but undated: kept, and the missing date blocks registration.
                self.assertIn((slot, "visit_date", "REQUIRED_MISSING"), blocking_of(draft_of(page)))

    def test_one_word_across_a_rule_does_not_create_a_visit(self):
        page = grid_page(VISITS) + [straddling("Négatif", "syphilis_test", "M7")]
        result = fields_by_slot(draft_of(page))
        self.assertEqual(list(result), ["T1_V1", "T2_V1", "M7"])
        self.assertIn("OCR_TEXT_SPANS_COLUMNS", result["M7"]["syphilis_test"]["validation_flags"])

    def test_undated_populated_column_stays_visible_for_review(self):
        page = grid_page({**VISITS, "T1_V2": {"weight_kg": "61", "bp": "12/8"}, "T1_V3": {"hiv_test": "Neg"}})
        draft = draft_of(page)
        result = fields_by_slot(draft)
        self.assertEqual(list(result), ["T1_V1", "T1_V2", "T1_V3", "T2_V1", "M7"])
        date = result["T1_V2"]["visit_date"]
        self.assertEqual((date["value"], date["field_status"], date["validation_flags"]),
                         (None, "NEEDS_REVIEW", ["CELL_BLANK"]))
        self.assertEqual((result["T1_V2"]["weight_kg"]["value"], result["T1_V2"]["weight_kg"]["field_status"]),
                         (61, "KNOWN"))
        self.assertEqual(result["T1_V3"]["hiv_test"]["value"], "NEGATIVE")
        self.assertTrue({("T1_V2", "visit_date", "REQUIRED_MISSING"),
                         ("T1_V3", "visit_date", "REQUIRED_MISSING")} <= blocking_of(draft))

    def test_same_date_in_two_columns_stays_blocked(self):
        draft = draft_of(grid_page({"T1_V1": VISITS["T1_V1"], "T1_V2": dict(VISITS["T1_V1"], weight_kg="61")}))
        self.assertIn(("T1_V2", "visit_date", "DUPLICATE_ENCOUNTER_DATE"), blocking_of(draft))

    def test_blank_columns_are_reported_but_create_no_visit(self):
        draft = draft_of(grid_page(VISITS))
        self.assertEqual(list(fields_by_slot(draft)), ["T1_V1", "T2_V1", "M7"])
        grid = draft["pages"][0]["grid"]
        self.assertEqual(grid["visit_columns"], ["T1_V1", "T2_V1", "M7"])
        self.assertEqual(grid["blank_columns"], ["T1_V2", "T1_V3", "T2_V2", "T2_V3", "M8", "M9"])

    def test_unclear_dates_are_never_guessed(self):
        cases = [("1?/05/2025", "OCR_UNPARSED"), ("10/05", "OCR_UNPARSED"), ("31/02/2025", "OCR_UNPARSED"),
                 ("12/7", "OCR_UNPARSED"), ("10/05/2O25", "OCR_CHARACTER_SUBSTITUTED")]
        for text, flag in cases:
            with self.subTest(text):
                draft = draft_of(grid_page(one_visit(visit_date=text), ddr=None))
                date = fields_by_slot(draft)["T1_V1"]["visit_date"]
                self.assertEqual(date["field_status"], "NEEDS_REVIEW")
                self.assertIn(flag, date["validation_flags"])
                self.assertEqual(date["raw_text"], text)
                if date["value"] is None:
                    self.assertIn(("T1_V1", "visit_date", "REQUIRED_MISSING"), blocking_of(draft))
        substituted = fields_by_slot(draft_of(grid_page(one_visit(visit_date="10/05/2O25"), ddr=None)))
        self.assertEqual(substituted["T1_V1"]["visit_date"]["value"], "2025-05-10")

    def test_a_date_is_never_read_as_a_blood_pressure(self):
        cases = [("10/05/2025", "BP_LOOKS_LIKE_DATE"), ("10/05", "BP_LOOKS_LIKE_DATE"), ("12/07", "BP_LOOKS_LIKE_DATE"),
                 ("11/12", "BP_ORDER_IMPLAUSIBLE"), ("70/120", "BP_ORDER_IMPLAUSIBLE")]
        for text, flag in cases:
            with self.subTest(text):
                fields = fields_by_slot(draft_of(grid_page(one_visit(bp=text))))["T1_V1"]
                for name in ("systolic_bp_mmhg", "diastolic_bp_mmhg"):
                    self.assertEqual((fields[name]["value"], fields[name]["field_status"], fields[name]["raw_text"]),
                                     (None, "NEEDS_REVIEW", text))
                    self.assertIn(flag, fields[name]["validation_flags"])

    def test_blood_pressure_units_are_normalized_explicitly(self):
        cases = [("12/7", (120, 70), {"from": "cmHg", "to": "mmHg", "factor": 10}),
                 ("12/7,5", (120, 75), {"from": "cmHg", "to": "mmHg", "factor": 10}),
                 ("118/76", (118, 76), None)]
        for text, values, normalization in cases:
            with self.subTest(text):
                fields = fields_by_slot(draft_of(grid_page(one_visit(bp=text))))["T1_V1"]
                systolic, diastolic = fields["systolic_bp_mmhg"], fields["diastolic_bp_mmhg"]
                self.assertEqual((systolic["value"], diastolic["value"]), values)
                self.assertEqual((systolic["unit"], systolic["raw_text"], systolic["field_status"]),
                                 ("mmHg", text, "KNOWN"))
                self.assertEqual(systolic["source"].get("normalization"), normalization)

    def test_gestational_age_keeps_weeks_and_days(self):
        cases = [("28 SA + 3 j", 199, "KNOWN"), ("28SA3j", 199, "KNOWN"), ("28 s 3 j", 199, "KNOWN"),
                 ("28+3", 199, "KNOWN"), ("28 SA", 196, "KNOWN"), ("28.5", None, "NEEDS_REVIEW"),
                 ("28,3 SA", None, "NEEDS_REVIEW"), ("283", None, "NEEDS_REVIEW"), ("28 SA 9 j", None, "NEEDS_REVIEW")]
        for text, days, status in cases:
            with self.subTest(text):
                fields = fields_by_slot(draft_of(grid_page(one_visit(gestational_age_days=text), ddr=None)))
                age = fields["T1_V1"]["gestational_age_days"]
                self.assertEqual((age["value"], age["field_status"], age["raw_text"], age["unit"]),
                                 (days, status, text, "days"))
                if days is not None:
                    self.assertEqual(age["source"]["normalization"],
                                     {"from": "weeks+days", "to": "days", "weeks": days // 7, "days": days % 7})
        ambiguous = fields_by_slot(draft_of(grid_page(one_visit(gestational_age_days="28.5"), ddr=None)))
        self.assertIn("GA_FORMAT_AMBIGUOUS", ambiguous["T1_V1"]["gestational_age_days"]["validation_flags"])

    def test_extracted_fields_are_never_verified_by_a_person(self):
        draft = pages_draft(grid_page(VISITS), grid_page(VISITS), cover_page())
        for _scope, _index, _slot, name, fv in schema.iter_fields(draft):
            with self.subTest(name):
                self.assertEqual((fv["verification"], fv["corrections"]),
                                 ({"state": "UNVERIFIED", "by": None, "at": None}, []))


class MultiPageTest(unittest.TestCase):
    def test_same_grid_on_two_pages_gives_each_visit_once_with_both_readings(self):
        draft = pages_draft(grid_page(VISITS), grid_page(VISITS))
        result = fields_by_slot(draft)
        self.assertEqual(list(result), ["T1_V1", "T2_V1", "M7"])
        weight = result["M7"]["weight_kg"]
        self.assertEqual((weight["value"], weight["field_status"]), (68.4, "KNOWN"))
        self.assertEqual([(r["page_ref"], r["value"]) for r in weight["source"]["readings"]],
                         [("whatsapp-media/0.jpg", 68.4), ("whatsapp-media/1.jpg", 68.4)])
        self.assertEqual(blocking_of(draft), set())

    def test_conflicting_values_across_pages_need_review_and_keep_every_reading(self):
        second = {slot: dict(cells) for slot, cells in VISITS.items()}
        second["M7"]["weight_kg"] = "69.4"
        second["T2_V1"]["visit_date"] = "06/07/2025"
        draft = pages_draft(grid_page(VISITS), grid_page(second))
        result = fields_by_slot(draft)
        for slot, name, values in (("M7", "weight_kg", [68.4, 69.4]),
                                   ("T2_V1", "visit_date", ["2025-07-05", "2025-07-06"])):
            with self.subTest(name):
                fv = result[slot][name]
                self.assertEqual((fv["value"], fv["field_status"], fv["validation_flags"]),
                                 (None, "NEEDS_REVIEW", ["CONFLICT_ACROSS_PAGES"]))
                self.assertEqual([r["value"] for r in fv["source"]["readings"]], values)
        self.assertEqual(result["M7"]["fundal_height_cm"]["field_status"], "KNOWN")
        self.assertIn(("M7", "weight_kg", "NEEDS_REVIEW"), blocking_of(draft))
        self.assertIn(("T2_V1", "visit_date", "REQUIRED_MISSING"), blocking_of(draft))

    def test_visit_on_one_photo_and_blank_column_on_another_is_a_conflict(self):
        draft = pages_draft(grid_page({**VISITS, "T1_V2": {"visit_date": "20/05/2025", "weight_kg": "61"}}),
                            grid_page(VISITS))
        fields = fields_by_slot(draft)["T1_V2"]
        for name in ("visit_date", "weight_kg"):
            with self.subTest(name):
                self.assertEqual((fields[name]["value"], fields[name]["validation_flags"]),
                                 (None, ["CONFLICT_ACROSS_PAGES"]))
                self.assertEqual([r["validation_flags"] for r in fields[name]["source"]["readings"]][1], ["CELL_BLANK"])

    def test_a_reading_beats_a_cell_another_photo_could_not_check(self):
        unchecked = PageScan(grid_page({slot: {k: v for k, v in cells.items() if k != "hiv_test"}
                                        for slot, cells in VISITS.items()}))
        hiv = fields_by_slot(pages_draft(grid_page(VISITS), unchecked))["T1_V1"]["hiv_test"]
        self.assertEqual((hiv["value"], hiv["field_status"]), ("NEGATIVE", "KNOWN"))
        self.assertEqual([r["validation_flags"] for r in hiv["source"]["readings"]], [[], ["OCR_NO_TEXT"]])

    def test_document_fields_from_two_pages_must_agree(self):
        cover = cover_page()
        other = [replace(l, text="N° de la fiche :_ 2026-999-002") if l.text.startswith("N°") else l for l in cover]
        docs = pages_draft(grid_page(VISITS), grid_page(VISITS, ddr="DDR : 13/04/2025"), cover, other)[
            "document_fields"]
        for name in ("registry_file_number", "last_menstrual_period"):
            with self.subTest(name):
                self.assertEqual((docs[name]["value"], docs[name]["validation_flags"]),
                                 (None, ["CONFLICT_ACROSS_PAGES"]))
        same = pages_draft(cover, cover)["document_fields"]["registry_file_number"]
        self.assertEqual((same["value"], same["validation_flags"]), ("2026999001", ["OCR_CONFIRM_REQUIRED"]))
        self.assertEqual(len(same["source"]["readings"]), 2)

    def test_history_delivery_and_cover_pages_give_no_antenatal_visit(self):
        delivery = [box("Déroulement de l'accouchement", 50, 50), box("Voie basse instrumentale", 50, 90)]
        draft = pages_draft(cover_page(), identification_page(), delivery)
        self.assertEqual(draft["encounters"], [])
        self.assertEqual([p["section"] for p in draft["pages"]], ["cover", "identification", "delivery"])
        self.assertFalse(any("grid" in p for p in draft["pages"]))

    def test_visit_table_on_a_postpartum_page_is_not_an_antenatal_visit(self):
        pregnancy_labels = {"Grossesse actuelle", "Age probable", "Syphilis (TPHA/VDRL)", "Sérologie VIH"}
        table = [l for l in grid_page(VISITS) if l.text not in pregnancy_labels]
        table += [box("État des lochies", 50, 600), box("État du périnée", 50, 630), box("Globe utérin", 50, 660)]
        self.assertEqual(live_ocr.detect_section(table), ("unknown", "low"))
        detected = pages_draft(table)
        self.assertEqual((detected["encounters"], detected["pages"][0]["review_reason"]), ([], "SECTION_UNCERTAIN"))
        self.assertEqual(pages_draft(table, hints=["postpartum"])["encounters"], [])
        chosen = pages_draft(table, hints=["current_pregnancy"])
        self.assertEqual([e["slot"] for e in chosen["encounters"]], ["T1_V1", "T2_V1", "M7"])


def cover_page() -> list[OcrLine]:
    return [box("Fiche de surveillance de la grossesse", 50, 50), box("Nom/Prénom : Inventée Exemple", 50, 100),
            box("N° de la fiche :_ 2026-999-001", 50, 150),
            box("Nom de l'établissement sanitaire :_CSC Exemple", 50, 200), box("Téléphone : 0600000000", 50, 250)]


class PagesAndPrivacyTest(unittest.TestCase):
    def cover(self) -> list[OcrLine]:
        return cover_page()

    def test_cover_values_are_always_confirmed_by_a_person(self):
        draft = draft_of(grid_page(VISITS), self.cover())
        docs = draft["document_fields"]
        self.assertEqual([p["section"] for p in draft["pages"]], ["current_pregnancy", "cover"])
        for name, value in (("registry_file_number", schema.FIELDS["registry_file_number"].parse("2026-999-001")),
                            ("facility_name", "CSC Exemple")):
            with self.subTest(name):
                self.assertEqual((docs[name]["field_status"], docs[name]["value"], docs[name]["validation_flags"]),
                                 ("NEEDS_REVIEW", value, ["OCR_CONFIRM_REQUIRED"]))
        # A label OCR did not find is unread, not absent from the paper.
        code = docs["midwife_patient_code"]
        self.assertEqual((code["field_status"], code["validation_flags"]), ("NEEDS_REVIEW", ["OCR_LABEL_NOT_FOUND"]))
        self.assertIn("bbox", docs["facility_name"]["source"])

    def test_names_and_phone_numbers_are_reported_as_categories_only(self):
        draft = draft_of(grid_page(VISITS), self.cover())
        self.assertEqual({(p["page_ref"], p["category"]) for p in draft["pii_detected"]},
                         {(PAGE, "STAFF_NAME"), (COVER, "PATIENT_NAME"), (COVER, "PHONE")})
        text = json.dumps(draft, ensure_ascii=False)
        for secret in ("Inventée", STAFF, "0600000000"):
            self.assertNotIn(secret, text)

    def test_grid_page_without_a_readable_table_asks_for_manual_entry(self):
        cases = [("one printed label", [box("Grossesse actuelle", 50, 80)], "SECTION_UNCERTAIN"),
                 ("two printed labels", [box("Grossesse actuelle", 50, 80), box("Examen clinique", 50, 120)],
                  "GRID_NOT_FOUND")]
        for label, lines, review in cases:
            with self.subTest(label):
                draft = draft_of(lines)
                [encounter] = draft["encounters"]
                page = draft["pages"][0]
                self.assertEqual((encounter["slot"], page["section"], page["review_reason"], page["grid"]),
                                 ("MANUAL", "current_pregnancy", review, {"found": False}))
                self.assertEqual({(f["field_status"], tuple(f["validation_flags"]))
                                  for f in encounter["fields"].values()}, {("NEEDS_REVIEW", ("OCR_GRID_NOT_FOUND",))})

    def test_unknown_page_and_empty_grid_give_no_visit(self):
        unknown = draft_of([box("Antécédents médicaux", 50, 50)])
        self.assertEqual((unknown["encounters"], unknown["pages"][0]["review_reason"]), ([], "UNKNOWN_LAYOUT"))
        empty = draft_of(grid_page({}))
        self.assertEqual(empty["encounters"], [])
        self.assertEqual((empty["pages"][0]["review_reason"], empty["pages"][0]["grid"]),
                         (None, {"found": True, "visit_columns": [], "unread_columns": [],
                                 "blank_columns": list(live_ocr.COLUMN_SLOTS)}))
        self.assertFalse([b for b in schema.blocking_fields(empty) if b["scope"] == "encounter"])

    def test_sections_not_photographed_are_unavailable_not_blank(self):
        docs = draft_of(grid_page(VISITS))["document_fields"]
        for name in ("registry_file_number", "facility_name", "midwife_patient_code"):
            with self.subTest(name):
                self.assertEqual((docs[name]["field_status"], docs[name]["validation_flags"]),
                                 ("NEEDS_REVIEW", ["SECTION_NOT_CAPTURED"]))


def identification_page() -> list[OcrLine]:
    return [box("IDENTIFICATION ET ANTÉCÉDENTS", 50, 50), box("Antécédents de la femme", 50, 100),
            box("Nom/Prénom : Inventée Exemple", 50, 150), box("Antécédents obstétricaux", 50, 200)]


class SectionTest(unittest.TestCase):
    def test_sections_come_from_printed_labels_with_a_confidence(self):
        cases = [
            ("identification", identification_page(), ("identification", "high")),
            ("delivery", [box("Déroulement de l'accouchement", 50, 50), box("Voie basse instrumentale", 50, 90)],
             ("delivery", "high")),
            ("one label", [box("Planification familiale", 50, 50)], ("postpartum", "low")),
            ("tie", [box("Planification familiale", 50, 50), box("État vaccinal", 50, 90)], ("unknown", "low")),
            ("nothing known", [box("Page 12", 50, 50), box("Observations", 50, 90)], ("unknown", "none")),
        ]
        for label, lines, expected in cases:
            with self.subTest(label):
                self.assertEqual(live_ocr.detect_section(lines), expected)

    def test_unknown_and_uncertain_pages_block_review_first(self):
        draft = draft_of(grid_page(VISITS), [box("Planification familiale", 50, 50)], [box("Observations", 50, 50)])
        blocking = schema.blocking_fields(draft)
        self.assertEqual([(b["scope"], b.get("page_index"), b["reason"]) for b in blocking[:2]],
                         [("page", 1, "SECTION_UNCERTAIN"), ("page", 2, "UNKNOWN_LAYOUT")])
        self.assertTrue(all(b["scope"] != "page" for b in blocking[2:]))

    def test_a_reviewer_section_replaces_detection(self):
        draft = draft_of(grid_page(VISITS), [box("Observations", 50, 50)],
                         section_hints=[None, "identification"])
        page = draft["pages"][1]
        self.assertEqual((page["section"], page["section_source"], page["section_confidence"], page["review_reason"]),
                         ("identification", "reviewer", None, None))
        self.assertEqual([b for b in schema.blocking_fields(draft) if b["scope"] == "page"], [])

    def test_a_confirmed_section_still_reports_what_could_not_be_read(self):
        lines = [box("Grossesse actuelle", 50, 80)]
        self.assertEqual(live_ocr.detect_section(lines), ("current_pregnancy", "low"))
        draft = draft_of(lines, section_hints=["current_pregnancy"])
        self.assertEqual(draft["pages"][0]["review_reason"], None)
        self.assertEqual(draft["encounters"][0]["fields"]["visit_date"]["validation_flags"], ["OCR_GRID_NOT_FOUND"])
        self.assertEqual(draft["encounters"][0]["fields"]["visit_date"]["field_status"], "NEEDS_REVIEW")

    def test_an_unreadable_grid_page_stays_unresolved_next_to_a_read_one(self):
        unreadable = [box("Grossesse actuelle", 50, 80), box("Examen clinique", 50, 120)]
        for hints in (None, [None, "current_pregnancy"], ["current_pregnancy", "current_pregnancy"]):
            with self.subTest(hints=hints):
                draft = draft_of(grid_page(VISITS), unreadable, section_hints=hints)
                slots = [e["slot"] for e in draft["encounters"]]
                self.assertEqual(slots, [*VISITS, "MANUAL"])
                manual = draft["encounters"][-1]["fields"]
                self.assertEqual({f["source"]["page_ref"] for f in manual.values()}, {COVER})
                self.assertEqual({(f["field_status"], tuple(f["validation_flags"])) for f in manual.values()},
                                 {("NEEDS_REVIEW", ("OCR_GRID_NOT_FOUND",))})
                self.assertEqual(draft["pages"][1]["grid"], {"found": False})
                self.assertEqual(draft["pages"][1]["review_reason"], None if hints else "GRID_NOT_FOUND")
                self.assertIn({"scope": "encounter", "encounter_index": len(VISITS), "slot": "MANUAL",
                               "field": "visit_date", "reason": "REQUIRED_MISSING"}, schema.blocking_fields(draft))

    def test_each_unreadable_grid_page_gets_its_own_manual_visit(self):
        unreadable = [box("Grossesse actuelle", 50, 80)]
        draft = draft_of(unreadable, unreadable, section_hints=["current_pregnancy", "current_pregnancy"])
        self.assertEqual([(e["slot"], e["fields"]["visit_date"]["source"]["page_ref"]) for e in draft["encounters"]],
                         [("MANUAL", PAGE), ("MANUAL", COVER)])


class UnreadPageTest(unittest.TestCase):
    def test_unusable_photo_is_a_page_to_review_and_the_others_are_read(self):
        draft = draft_of(grid_page(VISITS), PageUnread("PHOTO_UNUSABLE", "IMAGE_BLURRY"))
        page = draft["pages"][1]
        self.assertEqual((page["read"], page["review_reason"], page["retake_reason"]),
                         (False, "PHOTO_UNUSABLE", "IMAGE_BLURRY"))
        self.assertIn("floue", page["retake_message"])
        self.assertEqual(list(fields_by_slot(draft)), ["T1_V1", "T2_V1", "M7"])
        # The cover may be the page that was not read: unread, not absent.
        self.assertEqual(draft["document_fields"]["registry_file_number"]["validation_flags"], ["PAGE_NOT_READ"])

    def test_timed_out_page_needs_a_person(self):
        draft = draft_of(grid_page(VISITS), PageUnread("OCR_TIMEOUT"))
        self.assertEqual(schema.blocking_fields(draft)[0]["reason"], "OCR_TIMEOUT")

    def test_photo_read_despite_quality_problems_is_reviewed_field_by_field(self):
        draft = draft_of(page_scan(grid_page(VISITS), warnings=["IMAGE_BLURRY"]), section_hints=["current_pregnancy"])
        weight = fields_by_slot(draft)["M7"]["weight_kg"]
        self.assertEqual((weight["field_status"], weight["value"]), ("NEEDS_REVIEW", 68.4))
        self.assertIn("PHOTO_QUALITY_LOW", weight["validation_flags"])
        self.assertEqual(draft["pages"][0]["warnings"], ["IMAGE_BLURRY"])


class FakeEngine:
    name = "paddleocr"
    version = "paddleocr-test"
    known_confidence = 0.97


class ExtractDraftTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def reader(self, results):
        def read(path, *, force=False):
            self.calls.append((Path(path).name, force))
            result = results[len(self.calls) - 1]
            if isinstance(result, Exception):
                raise result
            return result
        return read

    def extract(self, results, **kwargs):
        pages = [(f"whatsapp-media/{i}.jpg", Path(f"{i}.jpg")) for i in range(len(results))]
        return live_ocr.extract_draft(FakeEngine(), pages, read_page=self.reader(results), **kwargs)

    def test_draft_records_the_engine_and_uses_its_threshold(self):
        draft = self.extract([page_scan(grid_page(VISITS, confidence=0.95))])
        self.assertEqual(draft["extraction"]["extractor"], "paddleocr")
        self.assertEqual(draft["extraction"]["extractor_version"], f"paddleocr-test+{live_ocr.PARSER_VERSION}")
        self.assertEqual(fields_by_slot(draft)["M7"]["weight_kg"]["validation_flags"], ["OCR_LOW_CONFIDENCE"])

    def test_one_bad_page_does_not_stop_the_others(self):
        timeout = ExtractionError("OCR_TIMEOUT", "trop long")
        draft = self.extract([page_scan(grid_page(VISITS)), PhotoRejected("IMAGE_TOO_DARK"), timeout])
        self.assertEqual([(p["read"], p["review_reason"]) for p in draft["pages"]],
                         [(True, None), (False, "PHOTO_UNUSABLE"), (False, "OCR_TIMEOUT")])

    def test_no_readable_page_fails_with_the_reason_of_each_page(self):
        with self.assertRaises(ExtractionError) as caught:
            self.extract([PhotoRejected("IMAGE_TOO_DARK"), PhotoRejected("IMAGE_BLURRY")])
        self.assertEqual(caught.exception.code, "RETAKE_REQUIRED")
        self.assertEqual(caught.exception.pages, [{"index": 0, "reason": "IMAGE_TOO_DARK"},
                                                  {"index": 1, "reason": "IMAGE_BLURRY"}])
        self.assertIn("page 2 : la photo est floue", caught.exception.message)
        self.calls.clear()
        with self.assertRaises(ExtractionError) as caught:
            self.extract([ExtractionError("OCR_TIMEOUT", "trop long")])
        self.assertEqual(caught.exception.code, "OCR_TIMEOUT")

    def test_engine_crash_is_logged_by_type_only(self):
        with self.assertLogs("dayone", "ERROR") as logs, self.assertRaises(ExtractionError) as caught:
            self.extract([RuntimeError("could not read 'Inventée Exemple'")])
        self.assertEqual(caught.exception.code, "OCR_FAILED")
        self.assertNotIn("Inventée", "\n".join(logs.output))

    def test_reviewer_sections_force_reading_and_stale_runs_stop(self):
        self.extract([page_scan(grid_page(VISITS)), page_scan([box("Observations", 50, 50)])],
                     section_hints=[None, "identification"])
        self.assertEqual([force for _name, force in self.calls], [False, True])
        self.calls.clear()
        answers = iter([True, False])
        with self.assertRaises(ExtractionCancelled):
            self.extract([page_scan(grid_page(VISITS)), page_scan(grid_page(VISITS))],
                         is_current=lambda: next(answers))
        self.assertEqual(len(self.calls), 1, "the second page is not read once the pages have changed")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_paddle_model_sets_and_missing_models(self):
        self.assertEqual(ocr_engines.create_engine("paddle", self.dir, environ={}).known_confidence, 0.97)
        mobile = ocr_engines.create_engine("paddle", self.dir, environ={"DAYONE_OCR_MODELS": "mobile"})
        self.assertEqual((mobile.detection_model, mobile.known_confidence), ("PP-OCRv5_mobile_det", 0.90))
        with self.assertRaises(ValueError):
            ocr_engines.create_engine("paddle", self.dir, environ={"DAYONE_OCR_MODELS": "large"})
        engine = ocr_engines.PaddleEngine(self.dir)
        self.assertIn("model PP-OCRv6_medium_det (python -m dayone.ocr_engines download)", engine.missing())
        self.assertIn(f"model {ocr_engines.ORIENTATION_MODEL} (python -m dayone.ocr_engines download)",
                      engine.missing())
        with self.assertRaises(ExtractionError) as caught:
            engine.read(self.dir / "p.png")
        self.assertEqual(caught.exception.code, "OCR_DEPENDENCY_MISSING")

    def test_engine_cpu_threads_are_capped(self):
        self.assertEqual(ocr_engines.create_engine("paddle", self.dir, environ={}).cpu_threads, 4)
        engine = ocr_engines.create_engine("paddle", self.dir, environ={"DAYONE_OCR_CPU_THREADS": "2"})
        self.assertEqual(engine._options()["cpu_threads"], 2)
        for value in ("1", "8", "all"):
            with self.subTest(value), self.assertRaises(ValueError):
                ocr_engines.create_engine("paddle", self.dir, environ={"DAYONE_OCR_CPU_THREADS": value})
        with mock.patch.dict(os.environ, {}, clear=False):
            engine._environment()
            self.assertEqual({os.environ[name] for name in ocr_engines.THREAD_VARIABLES}, {"2"})

    def test_missing_tesseract_program_is_reported(self):
        engine = ocr_engines.TesseractEngine(self.dir, command=str(self.dir / "no-tesseract.exe"))
        self.assertEqual(engine.missing(), ["tesseract program (docs/ocr.md)"])
        with self.assertRaises(ExtractionError) as caught:
            engine.read(self.dir / "p.png")
        self.assertEqual(caught.exception.code, "OCR_DEPENDENCY_MISSING")

    def test_tesseract_words_are_joined_into_cells(self):
        tsv = "\n".join([
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t10\t20\t40\t20\t96\tVenue",
            "5\t1\t1\t1\t1\t2\t55\t20\t20\t20\t90\tle",
            "5\t1\t1\t1\t1\t3\t300\t20\t80\t20\t80\t10/05/2025",
            "5\t1\t1\t1\t2\t1\t10\t60\t30\t20\t-1\t ",
        ])
        self.assertEqual(ocr_engines._tesseract_lines(tsv), [OcrLine("Venue le", 0.90, 10, 20, 75, 40),
                                                             OcrLine("10/05/2025", 0.80, 300, 20, 380, 40)])


if __name__ == "__main__":
    unittest.main()
