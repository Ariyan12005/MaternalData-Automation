"""Verified values must reach the CSV while unreviewed suggestions stay blank."""

import csv
import io
import unittest

from dayone import export_columns


def field(value, state="CONFIRMED", status="KNOWN"):
    return {"value": value, "field_status": status, "verification": {"state": state}}


class ExportValuesTest(unittest.TestCase):
    def row(self, extended=None, visits=None):
        return export_columns.compute_export_row({"patient_id": "PAT-TEST"}, visits or [], extended)

    def test_confirmed_values_reach_the_csv(self):
        extended = {
            "pregnancy": {"maternal_age_years": field(29), "gravidity": field(2, "CORRECTED"),
                          "parity": field(0)},
            "delivery": {"delivery_mode": field("CESAREAN_PLANNED"),
                         "delivery_gestational_age_days": field(266)},
            "newborns": [{"index": 1, "fields": {"newborn_sex": field("MALE"),
                          "birth_weight_g": field(3200), "birth_head_circumference_cm": field(34)}}],
        }
        visits = [{"visit_date": "2026-01-04", "slot": "T1_V1", "fields": {
            "systolic_bp_mmhg": field(120), "diastolic_bp_mmhg": field(80),
            "hiv_test": field("NEGATIVE"), "gestational_age_days": field(84)}}]
        row = self.row(extended, visits)
        expected = {"age (years)": 29, "gravidity (number)": 2, "parity (number)": 0,
                    "mean systolic bp (mmhg)": 120, "mean diastolic bp (mmhg)": 80,
                    "hiv test result": 0, "gestational age at enrollment (weeks)": 12,
                    "gestational age at birth (weeks)": 38,
                    "type of delivery (0=vaginal,1=cesarean)": 1,
                    "newborn sex (0=female,1=male)": 1, "child birth weight (g)": 3200,
                    "head circumference (cm)": 34}
        csv_row = next(csv.DictReader(io.StringIO(export_columns.export_to_csv([row]))))
        for column, value in expected.items():
            with self.subTest(column=column):
                self.assertEqual(row[column], value)
                self.assertEqual(float(csv_row[column]), value)

    def test_unverified_or_missing_verification_values_are_excluded(self):
        for verification in ({"state": "UNVERIFIED"}, None, "CONFIRMED", {}):
            with self.subTest(verification=verification):
                unreviewed = field(29)
                if verification is None:
                    unreviewed.pop("verification")
                else:
                    unreviewed["verification"] = verification
                row = self.row({"pregnancy": {"maternal_age_years": unreviewed}})
                self.assertIsNone(row["age (years)"])
        row = self.row({"pregnancy": {"maternal_age_years": field(29, status="NEEDS_REVIEW")}})
        self.assertIsNone(row["age (years)"])

    def test_newborn_sex_preserves_canonical_and_zero_values(self):
        for sex, encoded in (("FEMALE", 0), ("MALE", 1), (0, 0), (1, 1)):
            with self.subTest(sex=sex):
                row = self.row({"newborns": [{"index": 1, "fields": {"newborn_sex": field(sex)}}]})
                self.assertEqual(row["newborn sex (0=female,1=male)"], encoded)

    def test_hemoglobin_uses_verified_extended_labs_in_visit_date_order(self):
        extended = {"visit_labs": [
            {"slot": "T1_V1", "fields": {"hemoglobin_g_dl": field(12.5)}},
            {"slot": "M9", "fields": {"hemoglobin_g_dl": field(10.5, "UNVERIFIED")}},
            {"slot": "T2_V1", "fields": {"hemoglobin_g_dl": field(11.5, "CORRECTED")}},
        ]}
        visits = [
            {"visit_date": "2026-05-04", "slot": "T1_V1", "fields": {}},
            {"visit_date": "2026-01-04", "slot": "M9", "fields": {}},
            {"visit_date": "2026-02-04", "slot": "T2_V1", "fields": {}},
        ]
        self.assertEqual(self.row(extended, visits)["hemoglobin (g/dl)"], 11.5)

    def test_hemoglobin_without_visit_dates_uses_catalog_slot_order(self):
        extended = {"visit_labs": [
            {"slot": "M9", "fields": {"hemoglobin_g_dl": field(12.5)}},
            {"slot": "T1_V1", "fields": {"hemoglobin_g_dl": field(11.5)}},
        ]}
        self.assertEqual(self.row(extended)["hemoglobin (g/dl)"], 11.5)
        extended["visit_labs"][1]["fields"]["hemoglobin_g_dl"].pop("verification")
        extended["visit_labs"][0]["fields"]["hemoglobin_g_dl"] = field(12.5, "UNVERIFIED")
        self.assertIsNone(self.row(extended)["hemoglobin (g/dl)"])


if __name__ == "__main__":
    unittest.main()
