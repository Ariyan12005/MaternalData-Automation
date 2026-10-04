import json
import unittest
from pathlib import Path

from dayone import schema

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureContractTest(unittest.TestCase):
    def test_fixtures_follow_schema(self):
        for name in ("sample_extraction.json", "success_extraction.json"):
            with self.subTest(fixture=name):
                self.assertEqual(schema.validate_draft(load(name)), [])

    def test_sample_has_exactly_two_uncertain_fields(self):
        blocking = schema.blocking_fields(load("sample_extraction.json"))
        self.assertEqual(
            [(b["slot"], b["field"], b["reason"]) for b in blocking],
            [("T2_V1", "fundal_height_cm", "ILLEGIBLE"), ("M9", "visit_date", "NEEDS_REVIEW")],
        )

    def test_success_fixture_has_no_blocking_field(self):
        self.assertEqual(schema.blocking_fields(load("success_extraction.json")), [])

    def test_field_catalog_has_twelve_fields(self):
        self.assertEqual(len(schema.FIELDS), 12)

    def test_validator_rejects_silent_known_below_threshold(self):
        draft = load("sample_extraction.json")
        draft["encounters"][2]["fields"]["visit_date"]["field_status"] = "KNOWN"
        errors = schema.validate_draft(draft)
        self.assertTrue(any("below" in e for e in errors), errors)

    def test_validator_rejects_value_on_missing_status(self):
        draft = load("success_extraction.json")
        draft["encounters"][0]["fields"]["weight_kg"]["value"] = 60
        self.assertTrue(schema.validate_draft(draft))

    def test_page_readings_and_unit_normalization_are_optional_and_checked(self):
        draft = load("sample_extraction.json")
        fields = draft["encounters"][0]["fields"]
        page = fields["weight_kg"]["source"]["page_ref"]
        reading = {"page_ref": page, "raw_text": "62kg", "value": 62, "field_status": "KNOWN", "confidence": 0.84,
                   "validation_flags": []}
        fields["weight_kg"]["source"]["readings"] = [reading, dict(reading, page_ref="whatsapp-media/b.jpg")]
        fields["systolic_bp_mmhg"]["source"]["normalization"] = {"from": "cmHg", "to": "mmHg", "factor": 10}
        self.assertEqual(schema.validate_draft(draft), [])

        fields["weight_kg"]["source"]["readings"] = [reading]
        fields["systolic_bp_mmhg"]["source"]["normalization"] = "cmHg"
        errors = schema.validate_draft(draft)
        self.assertTrue(any("readings" in e for e in errors) and any("normalization" in e for e in errors), errors)

    def test_conflict_across_pages_cannot_be_known_before_review(self):
        draft = load("sample_extraction.json")
        draft["encounters"][0]["fields"]["weight_kg"]["validation_flags"] = ["CONFLICT_ACROSS_PAGES"]
        self.assertTrue(any("conflict" in e for e in schema.validate_draft(draft)))
        draft["encounters"][0]["fields"]["weight_kg"]["verification"] = {"state": "CORRECTED", "by": "agent", "at": "x"}
        self.assertEqual(schema.validate_draft(draft), [])

    def test_draft_without_antenatal_visit_is_valid(self):
        draft = load("success_extraction.json")
        draft["encounters"] = []
        self.assertEqual(schema.validate_draft(draft), [])
        self.assertTrue(all(b["scope"] == "document" for b in schema.blocking_fields(draft)))


class ParsingTest(unittest.TestCase):
    def test_reviewer_inputs_are_normalized(self):
        cases = [
            ("visit_date", "19/12/25", "2025-12-19"),
            ("visit_date", "2025-12-19", "2025-12-19"),
            ("gestational_age_days", "28SA+1j", 197),
            ("gestational_age_days", "35", 245),
            ("gestational_age_days", "28 SA 3 j", 199),
            ("gestational_age_days", "28 SA et 3 jours", 199),
            ("gestational_age_days", "28 sem + 3", 199),
            ("systolic_bp_mmhg", "12", 120),
            ("systolic_bp_mmhg", "125", 125),
            ("diastolic_bp_mmhg", "7,5", 75),
            ("weight_kg", "63,5 kg", 63.5),
            ("syphilis_test", "nég", "NEGATIVE"),
            ("midwife_patient_code", "CM: 164125", "164125"),
            ("registry_file_number", "164 125", "164125"),
        ]
        for field, raw, expected in cases:
            with self.subTest(field=field, raw=raw):
                self.assertEqual(schema.FIELDS[field].parse(raw), expected)

    def test_invalid_inputs_are_rejected(self):
        cases = [
            ("visit_date", "31/02/25"),
            ("visit_date", "hier"),
            ("fundal_height_cm", "80"),
            ("gestational_age_days", "28+9"),
            ("gestational_age_days", "28,5"),  # decimal weeks or 28+5: never guessed
            ("gestational_age_days", "283"),
            ("gestational_age_days", "28 3"),
            ("systolic_bp_mmhg", "120,5"),
            ("hiv_test", "peut-être"),
        ]
        for field, raw in cases:
            with self.subTest(field=field, raw=raw):
                with self.assertRaises(schema.InvalidValue):
                    schema.FIELDS[field].parse(raw)


if __name__ == "__main__":
    unittest.main()
