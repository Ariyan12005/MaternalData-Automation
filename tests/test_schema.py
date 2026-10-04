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
            [("M8", "fundal_height_cm", "ILLEGIBLE"), ("M9", "visit_date", "NEEDS_REVIEW")],
        )

    def test_success_fixture_has_no_blocking_field(self):
        self.assertEqual(schema.blocking_fields(load("success_extraction.json")), [])

    def test_field_catalog_has_twelve_fields(self):
        self.assertEqual(len(schema.FIELDS), 12)

    def test_validator_rejects_silent_known_below_threshold(self):
        draft = load("sample_extraction.json")
        draft["encounters"][5]["fields"]["visit_date"]["field_status"] = "KNOWN"
        errors = schema.validate_draft(draft)
        self.assertTrue(any("below" in e for e in errors), errors)

    def test_validator_rejects_value_on_missing_status(self):
        draft = load("success_extraction.json")
        draft["encounters"][0]["fields"]["fundal_height_cm"]["value"] = 12  # marked "–" on the page
        self.assertTrue(schema.validate_draft(draft))


class ParsingTest(unittest.TestCase):
    def test_reviewer_inputs_are_normalized(self):
        cases = [
            ("visit_date", "19/12/25", "2025-12-19"),
            ("visit_date", "2025-12-19", "2025-12-19"),
            ("gestational_age_days", "28SA+1j", 197),
            ("gestational_age_days", "35", 245),
            ("systolic_bp_mmhg", "12", 120),
            ("systolic_bp_mmhg", "125", 125),
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
            ("hiv_test", "peut-être"),
        ]
        for field, raw in cases:
            with self.subTest(field=field, raw=raw):
                with self.assertRaises(schema.InvalidValue):
                    schema.FIELDS[field].parse(raw)


if __name__ == "__main__":
    unittest.main()
