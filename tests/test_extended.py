"""Extended paper fields (dayone/extended.py, dayone/ocr_extended.py) and the export column catalog.

Synthetic page scans only: OCR boxes are written by hand and checkboxes are drawn as dark pixels on white paper.
Each service test uses its own temporary database.
"""

import copy
import csv
import json
import unittest

from dayone import export_columns, extended, live_ocr, schema, server
from dayone.extraction import ExtractionError, check_live_draft
from dayone.service import Invalid

from tests.test_flow import REPO_ROOT, REVIEWER
from tests.test_live_ocr import VISITS, box, cover_page, grid_page, page_scan
from tests.test_live_ocr_adapter import LiveOcrAdapterTestCase

BOX_SIDE = 13


def checkbox_ink(label: str, x: float, y: float, fill: int = 0) -> tuple[list, list]:
    """A printed square just left of a label at (x, y), with a fill x fill block of pen ink inside it."""
    x0, y0 = x - 20, y - 7
    s = BOX_SIDE
    ink = [(x0, y0, x0 + s + 1, y0 + 1), (x0, y0 + s, x0 + s + 1, y0 + s + 1),
           (x0, y0, x0 + 1, y0 + s + 1), (x0 + s, y0, x0 + s + 1, y0 + s + 1)]
    if fill:
        cx, cy = x0 + s // 2, y0 + s // 2
        ink.append((cx - fill // 2, cy - fill // 2, cx - fill // 2 + fill, cy - fill // 2 + fill))
    return [box(label, x, y)], ink


TICKED, FAINT, EMPTY = 6, 3, 0


def identification(*, age="27 ans", consanguinity=TICKED, desired=EMPTY, abortions="1", deliveries=True,
                   family_diabetes=True) -> tuple[list, list]:
    lines = [box("IDENTIFICATION ET ANTÉCÉDENTS", 50, 50), box("Antécédents de la femme", 50, 100),
             box(f"Age : {age}", 50, 150), box("Niveau d'instruction :", 50, 190, width=220),
             box("Secondaire", 300, 190)]
    ink = []
    for label, y, fill in (("Consanguinité", 250, consanguinity), ("Grossesse désirée", 290, desired)):
        if fill is not None:
            more_lines, more_ink = checkbox_ink(label, 300, y, fill)
            lines += more_lines
            ink += more_ink
    if family_diabetes:
        # The family's history, ticked: never the woman's own history.
        more_lines, more_ink = checkbox_ink("Diabète", 600, 340, TICKED)
        lines += [box("Antécédents héréditaires", 50, 340, width=220)] + more_lines
        ink += more_ink
    lines += [box("Antécédents obstétricaux", 50, 400), box("Gestation : 3", 50, 440), box("Parité : 2", 50, 480),
              box("Nombre d'enfants vivants : 2", 50, 520)]
    lines += [box("Nombre", 300, 580, width=60), box("Date", 400, 580, width=40),
              box("Avortement", 50, 620, width=200), box("Mort-né", 50, 660, width=200)]
    if abortions:
        lines.append(box(abortions, 320, 620, width=10))
    if deliveries:
        lines += [box("Déroulement des accouchements antérieurs", 50, 720)]
        lines += [box(f"Accouch. {n}", 300 + 100 * (n - 1), 760, width=70) for n in range(1, 6)]
        lines += [box("Date", 50, 800, width=100), box("Modalité d'extraction", 50, 840, width=200),
                  box("Complications", 50, 880, width=200)]
        lines += [box("12/03/2019", 305, 800, width=80), box("Voie basse", 305, 840, width=80),
                  box("05/06/2021", 405, 800, width=80), box("Césarienne", 405, 840, width=80)]
    return lines, ink


LAB_Y = {"hemoglobin_g_dl": 580, "blood_glucose_g_l": 620, "albuminuria": 660}


def pregnancy_with_labs(labs: dict) -> list:
    """The grid page (three visits) with its Examen biologique rows; labs: {slot: {field: text}}."""
    lines = grid_page(VISITS) + [box("Taille : 165 cm", 700, 140)]
    lines += [box(label, 40, LAB_Y[name], width=200) for name, (_p, label) in live_ocr.LAB_ROWS.items()]
    for slot, cells in labs.items():
        column = live_ocr.COLUMN_SLOTS.index(slot)
        lines += [box(text, 305 + 100 * column, LAB_Y[name], width=60) for name, text in cells.items()]
    return lines


def delivery(boxes=(TICKED, EMPTY, EMPTY, EMPTY)) -> tuple[list, list]:
    lines = [box("Déroulement de l'accouchement", 50, 50), box("Au moment de l'accouchement", 50, 90),
             box("Date de l'accouchement : 14/10/2025", 50, 150), box("Âge gestationnel : 39 SA + 2 j", 50, 190)]
    ink = []
    for (label, x, y), fill in zip((("Voie basse non instrumentale", 300, 250), ("Voie basse instrumentale", 300, 290),
                                    ("Césarienne : Programmée", 300, 330), ("Urgence", 600, 330)), boxes):
        more_lines, more_ink = checkbox_ink(label, x, y, fill)
        lines += more_lines
        ink += more_ink
    lines += [box("État du nouveau-né", 50, 400), box("Sexe : Féminin", 50, 440),
              box("Poids à la naissance : 3200 g", 50, 480), box("Périmètre crânien à la naissance : 34.5 cm", 50, 520)]
    return lines, ink


def newborn(title="Post-partum précoce", feeding=(EMPTY, EMPTY, TICKED)) -> tuple[list, list]:
    lines = [box("État vaccinal", 50, 50), box("Allaitement maternel", 50, 90),
             box("Date de la consultation : 20/10/2025", 50, 190)]
    if title:
        lines.append(box(title, 50, 140))
    ink = []
    for (label, x), fill in zip((("exclusivement au sein", 300), ("Artificiel", 600), ("mixte", 800)), feeding):
        more_lines, more_ink = checkbox_ink(label, x, 250, fill)
        lines += more_lines
        ink += more_ink
    return lines, ink


def scan(page) -> live_ocr.PageScan:
    lines, ink = page if isinstance(page, tuple) else (page, ())
    return page_scan(lines, width=1300, height=1000, ink=ink)


def build(*pages, hints=None) -> dict:
    scans = [(f"whatsapp-media/{i}.jpg", scan(p)) for i, p in enumerate(pages)]
    draft = live_ocr.build_draft(scans, extractor="paddleocr", extractor_version="test", min_confidence=0.97,
                                 section_hints=hints)
    assert schema.validate_draft(draft) == [], schema.validate_draft(draft)
    return draft


def state(fv: dict) -> tuple:
    return fv["field_status"], fv["value"], fv["validation_flags"]


def items(draft: dict, section: str) -> dict:
    key = extended.SECTIONS[section].item_key
    return {item[key]: item["fields"] for item in draft["extended"][section]}


class SchemaTest(unittest.TestCase):
    def test_drafts_without_extended_fields_stay_valid(self):
        for path in sorted((REPO_ROOT / "fixtures").glob("*_extraction.json")):
            with self.subTest(path.name):
                draft = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("extended", draft)
                self.assertEqual(schema.validate_draft(draft), [])

    def test_choice_values_accept_codes_and_french_spellings_only(self):
        mode = extended.FIELDS["delivery_mode"]
        self.assertEqual(mode.parse("Césarienne en urgence"), "CESAREAN_EMERGENCY")
        self.assertEqual(mode.parse("VAGINAL_INSTRUMENTAL"), "VAGINAL_INSTRUMENTAL")
        with self.assertRaises(schema.InvalidValue):
            mode.parse("césarienne")
        self.assertIsNotNone(mode.type_error("MALE"))
        self.assertIsNone(mode.type_error("CESAREAN_PLANNED"))
        mark = extended.FIELDS["consanguinity_mark"]
        self.assertEqual((mark.parse("case cochée"), mark.parse("vide")), ("MARKED", "UNMARKED"))
        with self.assertRaises(schema.InvalidValue):
            mark.parse("non")  # an empty box is not "no" (D5); only the box state can be entered

    def test_glucose_keeps_two_decimals_and_units_are_accepted(self):
        self.assertEqual(extended.FIELDS["blood_glucose_g_l"].parse("0.857"), 0.86)
        self.assertEqual(extended.FIELDS["hemoglobin_g_dl"].parse("11.46"), 11.5)
        self.assertEqual(extended.FIELDS["maternal_age_years"].parse("27 ans"), 27)
        self.assertEqual(extended.FIELDS["birth_weight_g"].parse("3200 gr"), 3200)

    def test_extended_object_is_validated(self):
        draft = build(identification())
        self.assertEqual(schema.validate_draft(draft), [])
        cases = {
            "unknown section": lambda e: e.update(fasting={}),
            "unknown field": lambda e: e["pregnancy"].update(bmi_pregestational={}),
            "duplicate item": lambda e: e["previous_deliveries"].append(copy.deepcopy(e["previous_deliveries"][0])),
            "bad item key": lambda e: e["previous_deliveries"][0].update(column=9),
            "wrong version": lambda e: e.update(catalog_version="x"),
            "bad value": lambda e: e["pregnancy"]["consanguinity_mark"].update(value="YES"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                broken = copy.deepcopy(draft)
                change(broken["extended"])
                self.assertTrue(schema.validate_draft(broken))

    def test_catalog_lists_extended_fields_with_their_source(self):
        catalog = schema.catalog()["extended"]
        self.assertEqual(catalog["version"], extended.CATALOG_VERSION)
        self.assertEqual(set(catalog["fields"]), set(extended.FIELDS))
        self.assertIn("à jeun", catalog["fields"]["blood_glucose_g_l"]["source"]["note"])
        self.assertTrue(all(not f["required"] for f in catalog["fields"].values()))

    def test_no_field_stands_for_an_unverified_meaning(self):
        names = " ".join(extended.FIELDS)
        for word in ("fasting", "bmi", "prepregnancy", "initiated", "family", "hypertension", "diabetes",
                     "proteinuria", "preterm", "referral", "cin", "patient_id"):
            with self.subTest(word):
                self.assertNotIn(word, names)


class CheckboxTest(unittest.TestCase):
    def test_box_states_come_from_the_ink_inside_the_printed_square(self):
        for fill, expected in ((TICKED, ("KNOWN", "MARKED", [])), (EMPTY, ("KNOWN", "UNMARKED", [])),
                               (FAINT, ("NEEDS_REVIEW", None, ["CHECKBOX_UNCLEAR"]))):
            with self.subTest(fill=fill):
                fv = build(identification(consanguinity=fill))["extended"]["pregnancy"]["consanguinity_mark"]
                self.assertEqual(state(fv), expected)
                self.assertEqual(fv["source"]["checkboxes"][0]["label"], "Consanguinité")
                self.assertIsNone(fv["confidence"])

    def test_a_label_without_a_printed_square_is_not_guessed(self):
        lines, ink = identification(desired=None)
        lines.append(box("Grossesse désirée", 300, 290))
        fv = build((lines, ink))["extended"]["pregnancy"]["pregnancy_desired_mark"]
        self.assertEqual(state(fv), ("NEEDS_REVIEW", None, ["CHECKBOX_NOT_FOUND"]))

    def test_choice_needs_exactly_one_marked_box_and_the_others_clearly_empty(self):
        cases = {
            "one": ((EMPTY, EMPTY, EMPTY, TICKED), ("KNOWN", "CESAREAN_EMERGENCY", [])),
            "two": ((TICKED, EMPTY, TICKED, EMPTY), ("NEEDS_REVIEW", None, ["CHECKBOX_MULTIPLE_MARKED"])),
            "none": ((EMPTY,) * 4, ("NOT_PROVIDED", None, ["CHECKBOX_NONE_MARKED"])),
            "one and a doubt": ((TICKED, FAINT, EMPTY, EMPTY),
                                ("NEEDS_REVIEW", "VAGINAL_NON_INSTRUMENTAL", ["CHECKBOX_UNCLEAR"])),
        }
        for label, (boxes, expected) in cases.items():
            with self.subTest(label):
                fv = build(delivery(boxes))["extended"]["delivery"]["delivery_mode"]
                self.assertEqual(state(fv), expected)
                self.assertEqual(len(fv["source"]["checkboxes"]), 4)


class ExtendedReadingTest(unittest.TestCase):
    def test_identification_page_values_keep_their_scope(self):
        ext = build(identification())["extended"]
        self.assertEqual(set(ext), {"catalog_version", "patient", "pregnancy", "previous_deliveries"})
        self.assertEqual(state(ext["patient"]["education_level_text"]),
                         ("NEEDS_REVIEW", "Secondaire", ["OCR_CONFIRM_REQUIRED"]))
        pregnancy = ext["pregnancy"]
        self.assertEqual(state(pregnancy["maternal_age_years"]), ("KNOWN", 27, []))
        # Handwritten counts are always confirmed by a person.
        for name, value in (("gravidity", 3), ("parity", 2), ("living_children_count", 2), ("abortions_count", 1)):
            with self.subTest(name):
                self.assertEqual(state(pregnancy[name]), ("NEEDS_REVIEW", value, ["OCR_CONFIRM_REQUIRED"]))
        self.assertNotIn("height_cm", pregnancy, "height is on the pregnancy page, which was not photographed")

    def test_family_history_is_never_read_as_the_womans_history(self):
        ext = build(identification(family_diabetes=True))["extended"]
        read = {name for section in ("patient", "pregnancy") for name in ext[section]}
        self.assertEqual(read, {"education_level_text", "maternal_age_years", "gravidity", "parity",
                                "living_children_count", "abortions_count", "consanguinity_mark",
                                "pregnancy_desired_mark"})

    def test_nothing_read_after_a_label_is_unread_not_blank(self):
        lines, ink = identification()
        lines = [l for l in lines if not l.text.startswith("Nombre d'enfants")]
        # OCR stretched the label box over a handwritten "1" it did not recognise; the paper to its right is blank.
        lines.append(box("Nombre d'enfants vivants :", 50, 520, width=290))
        ink.append((326, 512, 329, 528))
        fv = build((lines, ink))["extended"]["pregnancy"]["living_children_count"]
        self.assertEqual(state(fv), ("NEEDS_REVIEW", None, ["OCR_NO_TEXT"]))

    def test_an_empty_abortions_cell_is_not_provided_never_zero(self):
        fv = build(identification(abortions=None))["extended"]["pregnancy"]["abortions_count"]
        self.assertEqual((fv["field_status"], fv["value"]), ("NOT_PROVIDED", None))

    def test_previous_deliveries_are_numbered_by_column_and_kept_as_written(self):
        deliveries = items(build(identification()), "previous_deliveries")
        self.assertEqual(list(deliveries), [1, 2])
        self.assertEqual(deliveries[1]["previous_delivery_date"]["value"], "2019-03-12")
        self.assertEqual(deliveries[2]["previous_delivery_mode_text"]["value"], "Césarienne")
        self.assertIn("OCR_CONFIRM_REQUIRED", deliveries[2]["previous_delivery_mode_text"]["validation_flags"])
        self.assertNotIn("previous_deliveries", build(identification(deliveries=False))["extended"])

    def test_labs_are_read_per_visit_without_making_a_visit(self):
        draft = build(pregnancy_with_labs({"T1_V1": {"hemoglobin_g_dl": "11.5 g/dl", "blood_glucose_g_l": "0.85 g/l",
                                                     "albuminuria": "Neg"},
                                           "T1_V2": {"hemoglobin_g_dl": "10.9"}}))
        self.assertEqual([e["slot"] for e in draft["encounters"]], ["T1_V1", "T2_V1", "M7"])
        self.assertFalse(set(draft["encounters"][0]["fields"]) & set(live_ocr.LAB_ROWS))
        labs = items(draft, "visit_labs")
        self.assertEqual(list(labs), ["T1_V1", "T1_V2", "T2_V1", "M7"])
        self.assertEqual(state(labs["T1_V1"]["hemoglobin_g_dl"]), ("KNOWN", 11.5, []))
        # Not called fasting: the form does not say.
        self.assertEqual(state(labs["T1_V1"]["blood_glucose_g_l"]), ("KNOWN", 0.85, []))
        self.assertEqual(labs["T1_V1"]["albuminuria"]["value"], "NEGATIVE")
        self.assertEqual(state(labs["T1_V2"]["hemoglobin_g_dl"]), ("NEEDS_REVIEW", 10.9, ["LAB_WITHOUT_VISIT"]))
        self.assertEqual(labs["T2_V1"]["hemoglobin_g_dl"]["field_status"], "NOT_PROVIDED")
        self.assertEqual(state(draft["extended"]["pregnancy"]["height_cm"]), ("KNOWN", 165, []))

    def test_delivery_page_gives_one_delivery_and_newborn_one(self):
        ext = build(delivery())["extended"]
        self.assertEqual(ext["delivery"]["delivery_date"]["value"], "2025-10-14")
        self.assertEqual(ext["delivery"]["delivery_gestational_age_days"]["value"], 39 * 7 + 2)
        [baby] = ext["newborns"]
        self.assertEqual(baby["index"], 1)
        self.assertEqual({name: fv["value"] for name, fv in baby["fields"].items()},
                         {"newborn_sex": "FEMALE", "birth_weight_g": 3200, "birth_head_circumference_cm": 34.5})

    def test_feeding_is_kept_per_consultation_period_only(self):
        consultations = items(build(newborn()), "newborn_consultations")
        self.assertEqual(list(consultations), ["EARLY"])
        self.assertEqual(state(consultations["EARLY"]["feeding_mode"]), ("KNOWN", "MIXED", []))
        self.assertEqual(consultations["EARLY"]["consultation_date"]["value"], "2025-10-20")
        self.assertNotIn("extended", build(newborn(title=None)), "no period title: the consultation is not kept")
        self.assertNotIn("newborns", build(newborn())["extended"], "a consultation is not a birth record")

    def test_two_photos_that_disagree_go_to_a_person(self):
        fv = build(identification(age="27 ans"), identification(age="29 ans"))["extended"]["pregnancy"][
            "maternal_age_years"]
        self.assertEqual((fv["field_status"], fv["value"]), ("NEEDS_REVIEW", None))
        self.assertIn("CONFLICT_ACROSS_PAGES", fv["validation_flags"])
        self.assertEqual([r["value"] for r in fv["source"]["readings"]], [27, 29])

    def test_sections_without_their_page_are_absent(self):
        draft = build(pregnancy_with_labs({}))
        self.assertEqual(set(draft["extended"]), {"catalog_version", "pregnancy", "visit_labs"})
        self.assertNotIn("extended", build([box("Observations", 50, 50)]))

    def test_extended_values_never_block_registration(self):
        draft = build(identification(consanguinity=FAINT), delivery((TICKED, TICKED, EMPTY, EMPTY)))
        self.assertFalse([b for b in schema.blocking_fields(draft) if b.get("scope") == "extended"])

    def test_no_identifier_reaches_the_extended_object(self):
        lines, ink = identification()
        lines += [box("CIN : ZZ999999", 50, 960), box("Nom/Prénom : Inventée Exemple", 600, 150)]
        text = str(build((lines, ink))["extended"])
        for secret in ("ZZ999999", "Inventée"):
            self.assertNotIn(secret, text)


class ExportColumnsTest(unittest.TestCase):
    def test_headers_match_the_provided_csv_exactly(self):
        with open(REPO_ROOT / "data" / "maternal_registry_synthetic.csv", encoding="utf-8-sig", newline="") as f:
            headers = next(csv.reader(f))
        self.assertEqual([c.header for c in export_columns.COLUMNS], headers)
        self.assertEqual([c.number for c in export_columns.COLUMNS], list(range(1, 32)))

    def test_inputs_are_real_fields_and_categories_are_known(self):
        known = export_columns.known_inputs()
        for column in export_columns.COLUMNS:
            with self.subTest(column.header):
                self.assertTrue(set(column.inputs) <= known, set(column.inputs) - known)
                self.assertIn(column.category, export_columns.CATEGORIES)
                if column.category == export_columns.REQUIRES_AGGREGATION:
                    self.assertTrue(column.inputs and column.decisions)

    def test_not_every_column_is_claimed(self):
        counts = export_columns.coverage_counts()
        self.assertEqual(sum(counts.values()), 31)
        self.assertLess(counts[export_columns.IMPLEMENTED_EVALUATED], 31)
        by_number = {c.number: c for c in export_columns.COLUMNS}
        for number in (1, 6, 7, 13, 17, 18, 23, 30):
            self.assertEqual(by_number[number].category, export_columns.UNAVAILABLE, number)

    def test_mapping_document_carries_the_generated_table(self):
        text = (REPO_ROOT / "docs" / "excel-field-mapping.md").read_text(encoding="utf-8")
        self.assertTrue(export_columns.coverage_markdown() in text,
                        "regenerate the coverage table with dayone.export_columns.coverage_markdown()")


class ExtendedReviewTest(LiveOcrAdapterTestCase):
    def setUp(self):
        super().setUp()
        self.reader.results = [scan(cover_page()), scan(identification(consanguinity=FAINT)), scan(delivery())]
        self.document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()

    def review(self, field, action, section, item_index=None, **kwargs):
        return self.service.review_field(self.document_id, reviewer=REVIEWER, scope="extended", field=field,
                                         action=action, section=section, item_index=item_index, **kwargs)

    def ext(self) -> dict:
        return self.service.get_document(self.document_id)["draft"]["extended"]

    def test_reviews_are_recorded_and_unreviewed_values_stay_unverified_after_registration(self):
        self.review("consanguinity_mark", "CORRECT", "pregnancy", value="case cochée")
        self.review("newborn_sex", "CONFIRM", "newborns", 0)
        self.review("abortions_count", "SET_STATUS", "pregnancy", field_status="ILLEGIBLE")
        ext = self.ext()
        self.assertEqual((ext["pregnancy"]["consanguinity_mark"]["value"],
                          ext["pregnancy"]["consanguinity_mark"]["verification"]["state"]), ("MARKED", "CORRECTED"))
        self.assertEqual(ext["newborns"][0]["fields"]["newborn_sex"]["verification"]["state"], "CONFIRMED")
        self.assertEqual(ext["pregnancy"]["abortions_count"]["field_status"], "ILLEGIBLE")
        events = [e for e in self.service.get_document(self.document_id)["events"] if e["type"] == "FIELD_REVIEWED"]
        self.assertEqual([(e["detail"]["section"], e["detail"]["item_index"]) for e in events],
                         [("pregnancy", None), ("newborns", 0), ("pregnancy", None)])

        for name in ("registry_file_number", "facility_name"):
            self.service.review_field(self.document_id, reviewer=REVIEWER, scope="document", field=name,
                                      action="CONFIRM")
        for name in ("midwife_patient_code", "last_menstrual_period"):
            self.service.review_field(self.document_id, reviewer=REVIEWER, scope="document", field=name,
                                      action="SET_STATUS", field_status="NOT_PROVIDED")
        self.service.select_patient(self.document_id, reviewer=REVIEWER, choice="NEW")
        self.service.confirm(self.document_id, reviewer=REVIEWER)

        ext = self.ext()
        self.assertEqual(ext["pregnancy"]["maternal_age_years"]["verification"]["state"], "UNVERIFIED")
        self.assertEqual(ext["delivery"]["delivery_mode"]["verification"]["state"], "UNVERIFIED")
        self.assertEqual(ext["pregnancy"]["consanguinity_mark"]["verification"]["state"], "CORRECTED")

    def test_the_fields_route_passes_the_section_and_item(self):
        api = server.Api(self.service)
        status, body = api.dispatch("POST", f"/api/documents/{self.document_id}/fields", {}, {
            "scope": "extended", "section": "newborns", "item_index": 0, "field": "birth_weight_g",
            "action": "CORRECT", "value": "3250"}, REVIEWER)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.ext()["newborns"][0]["fields"]["birth_weight_g"]["value"], 3250)
        status, body = api.dispatch("GET", "/api/system", {}, {}, None)
        self.assertIn("birth_weight_g", body["catalog"]["extended"]["fields"])

    def test_unknown_targets_are_refused(self):
        with self.assertRaises(Invalid):
            self.review("newborn_sex", "CONFIRM", "pregnancy")
        with self.assertRaises(Invalid):
            self.review("newborn_sex", "CONFIRM", "newborns", 4)
        with self.assertRaises(Invalid):
            self.review("height_cm", "CONFIRM", "pregnancy")  # the pregnancy page was not photographed

    def test_a_live_extractor_cannot_claim_an_extended_value_was_verified(self):
        draft = self.service.get_document(self.document_id)["draft"]
        claimed = copy.deepcopy(draft)
        claimed["extended"]["delivery"]["delivery_mode"]["verification"] = {
            "state": "CONFIRMED", "by": "extractor", "at": "2026-10-03T18:00:00Z"}
        refs = [p["page_ref"] for p in draft["pages"]]
        with self.assertRaises(ExtractionError) as caught:
            check_live_draft(claimed, refs)
        self.assertEqual(caught.exception.code, "EXTRACTOR_CLAIMS_VERIFICATION")


if __name__ == "__main__":
    unittest.main()
