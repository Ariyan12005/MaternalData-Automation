import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from dayone import schema, specimen_ocr
from dayone.extraction import ExtractionError
from dayone.live_ocr import (LiveOcrExtractor, OcrLine, PaddleOcrReader, TesseractOcrReader, _section,
                             assess_photo, is_specimen)
from dayone.specimen_ocr import Reading, check_booklet, classify, combine, normalize

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECIMEN = "data/Paper Registry/dossiers_specimen_10_patientes-{:02d}.png"


def specimen_page(number: int) -> str:
    """Pages 07+ exist only with a download suffix (identical duplicates); take the first."""
    return sorted((REPO_ROOT / "data" / "Paper Registry").glob(f"dossiers_specimen_10_patientes-{number:02d}*.png"))[0] \
        .relative_to(REPO_ROOT).as_posix()


def agreed(value, votes=4):
    return Reading(value=value, raw_text=str(value), votes=votes)


class VotingTest(unittest.TestCase):
    def test_three_agreeing_readings_make_a_value_known(self):
        reading = combine("weight_kg", ["66.8", "66.8", "66.8", None])
        self.assertTrue(reading.agreed)
        self.assertEqual((reading.value, reading.confidence), (66.8, 0.75))

    def test_one_disagreeing_reading_blocks_known(self):
        reading = combine("bp", ["113/60", "143/60", "143/60", "143/60"])
        self.assertEqual(reading.value, (143, 60))
        self.assertFalse(reading.agreed)
        self.assertIn("OCR_READINGS_DISAGREE", reading.flags)

    def test_two_readings_are_a_candidate_only(self):
        reading = combine("fundal_height_cm", ["31", "31", None, None])
        self.assertEqual(reading.value, 31)
        self.assertFalse(reading.agreed)

    def test_tie_gives_no_candidate(self):
        reading = combine("fundal_height_cm", ["31", "37", None, None])
        self.assertIsNone(reading.value)

    def test_parsing_is_strict(self):
        self.assertEqual(normalize("gestational_age_days", "38 SA"), 266)
        self.assertEqual(normalize("visit_date", "18/01/2026"), "2026-01-18")
        self.assertEqual(normalize("bp", "110/60"), (110, 60))
        self.assertEqual(normalize("syphilis_test", "Neg"), "NEGATIVE")
        self.assertEqual(normalize("registry_file_number", ": 2026-823-001"), "2026823001")
        for name, text in [("weight_kg", "302"), ("visit_date", "18/01/20"), ("bp", "60/110"),
                           ("bp", "1100/60"), ("gestational_age_days", "3 SA 2"), ("visit_date", "18/01/2099")]:
            with self.subTest(name=name, text=text):
                self.assertIsNone(normalize(name, text))


class ConsistencyTest(unittest.TestCase):
    def grid(self, **columns):
        return {slot: {row: agreed(value) if value is not None else Reading(ink="blank")
                       for row, value in rows.items()} for slot, rows in columns.items()}

    def test_age_that_contradicts_ddr_goes_to_review(self):
        lmp = agreed("2025-12-22")
        grid = self.grid(T1_V1={"visit_date": "2026-02-14", "gestational_age_days": 21},
                         T2_V2={"visit_date": "2026-05-25", "gestational_age_days": 154})
        check_booklet(grid, lmp)
        self.assertFalse(grid["T1_V1"]["gestational_age_days"].agreed)
        self.assertIn("AGE_INCONSISTENT_WITH_DATES", grid["T1_V1"]["gestational_age_days"].flags)
        self.assertTrue(grid["T2_V2"]["gestational_age_days"].agreed)

    def test_ddr_needs_confirmation_from_visits(self):
        lmp = Reading(value="2025-11-29", raw_text="29/11/2025", votes=2)
        grid = self.grid(T1_V1={"visit_date": "2026-01-28", "gestational_age_days": 70},
                         T1_V2={"visit_date": "2026-02-20", "gestational_age_days": 91})
        check_booklet(grid, lmp)
        self.assertFalse(lmp.agreed)
        self.assertIn("DDR_NOT_CONFIRMED_BY_VISITS", lmp.flags)

    def test_ddr_confirmed_by_two_visits_becomes_known(self):
        lmp = Reading(value="2025-11-21", raw_text="21/11/2025", votes=2)
        grid = self.grid(T1_V1={"visit_date": "2026-01-28", "gestational_age_days": 70},
                         T1_V2={"visit_date": "2026-02-20", "gestational_age_days": 91})
        check_booklet(grid, lmp)
        self.assertTrue(lmp.agreed)

    def test_dates_out_of_order_and_weight_drop_go_to_review(self):
        grid = self.grid(M7={"visit_date": "2026-07-28", "weight_kg": 81.2},
                         M9={"visit_date": "2026-04-17", "weight_kg": 30.2})
        check_booklet(grid, None)
        self.assertIn("VISIT_DATES_OUT_OF_ORDER", grid["M9"]["visit_date"].flags)
        self.assertFalse(grid["M9"]["weight_kg"].agreed)

    def test_fundal_height_needs_a_confirmed_age(self):
        grid = self.grid(T2_V1={"visit_date": "2026-05-02", "fundal_height_cm": 5})
        check_booklet(grid, None)
        self.assertIn("FUNDAL_HEIGHT_NOT_CONFIRMED", grid["T2_V1"]["fundal_height_cm"].flags)


class PageTest(unittest.TestCase):
    def test_pages_are_recognised_by_printed_title(self):
        words = [OcrLine("GROSSESSE", 0.9, 30, 20), OcrLine("ACTUELLE", 0.9, 200, 20), OcrLine("SPÉCIMEN", 0.9, 1100, 20)]
        self.assertEqual(classify(words), ("current_pregnancy", True))
        self.assertEqual(classify([OcrLine("TA", 0.9, 30, 700)]), (None, False))

    def test_file_name_fallback_covers_eight_page_booklets(self):
        self.assertEqual(_section(SPECIMEN.format(11)), "current_pregnancy")
        self.assertEqual(_section("data/Paper Registry/dossiers_specimen_10_patientes-25__1-CjNUosOk.png"), "cover")

    def test_only_specimen_pictures_are_read(self):
        self.assertTrue(is_specimen(SPECIMEN.format(1)))
        for ref in ("data/Paper Registry/1-4.jpg", "var/uploads/dossiers_specimen_x.png",
                    "data/Paper Registry/sub/dossiers_specimen_1.png"):
            self.assertFalse(is_specimen(ref), ref)
        with self.assertRaises(ExtractionError) as caught:
            LiveOcrExtractor(REPO_ROOT, reader=object()).extract([SPECIMEN.format(1), "data/Paper Registry/1-4.jpg"])
        self.assertEqual(caught.exception.code, "NOT_A_SPECIMEN_PAGE")

    def test_tiny_photo_asks_for_retake(self):
        with self.assertRaises(ExtractionError) as caught:
            assess_photo(b"\x89PNG small")
        self.assertEqual(caught.exception.code, "RETAKE_REQUIRED")


class AdapterTest(unittest.TestCase):
    TSV = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t1427\t698\t60\t20\t95.5\t110/60\n"
           "5\t1\t1\t1\t1\t2\t10\t10\t5\t5\t-1\t \n")

    def test_tesseract_reads_bytes_from_stdin_with_one_thread(self):
        completed = subprocess.CompletedProcess([], 0, stdout=self.TSV.encode(), stderr=b"")
        with patch("dayone.live_ocr.subprocess.run", return_value=completed) as run:
            lines = TesseractOcrReader().read_line(b"png-bytes", "0123456789/")
        self.assertEqual(lines, [OcrLine("110/60", 0.955, 1427, 698, width=60, height=20)])
        self.assertEqual(run.call_args.args[0][1], "stdin")
        self.assertEqual(run.call_args.kwargs["input"], b"png-bytes")
        self.assertEqual(run.call_args.kwargs["env"]["OMP_THREAD_LIMIT"], "1")
        self.assertIn("tessedit_char_whitelist=0123456789/", run.call_args.args[0])

    def test_missing_tesseract_has_explicit_error(self):
        with patch("dayone.live_ocr.subprocess.run", side_effect=FileNotFoundError("tesseract")):
            with self.assertRaises(ExtractionError) as caught:
                TesseractOcrReader().read(Path("page.png"))
        self.assertEqual(caught.exception.code, "OCR_DEPENDENCY_MISSING")

    def test_paddle_adapter_preserves_text_score_and_box(self):
        class Engine:
            def predict(self, path):
                return [{"rec_texts": ["31 SA", "invalid"],
                         "rec_scores": [0.96, float("nan")],
                         "rec_polys": [[[1420, 548], [1490, 548], [1490, 570], [1420, 570]],
                                       [[0, 0], [1, 0], [1, 1], [0, 1]]]}]
        self.assertEqual(PaddleOcrReader(Engine()).read(Path("unused.png")),
                         [OcrLine("31 SA", 0.96, 1420, 548, width=70, height=22)])

    def test_paddle_failures_have_explicit_errors(self):
        class Failing:
            def predict(self, path):
                raise RuntimeError("failure")

        class Mismatched:
            def predict(self, path):
                return [{"rec_texts": ["31 SA"], "rec_scores": [], "rec_polys": []}]
        for engine, code in ((Failing(), "OCR_FAILED"), (Mismatched(), "OCR_INVALID_RESULT")):
            with self.assertRaises(ExtractionError) as caught:
                PaddleOcrReader(engine).read(Path("unused.png"))
            self.assertEqual(caught.exception.code, code)


@unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
class RealSpecimenTest(unittest.TestCase):
    """Patient 1, real Tesseract: values that come out KNOWN must match the page."""

    TRUTH = {
        "T1_V2": {"visit_date": "2025-07-20", "weight_kg": 58.8, "systolic_bp_mmhg": 104, "syphilis_test": "NEGATIVE"},
        "M9": {"visit_date": "2026-01-18", "weight_kg": 66.8, "systolic_bp_mmhg": 110, "fundal_height_cm": 34},
    }

    def test_patient_one_has_no_false_known(self):
        draft = LiveOcrExtractor(REPO_ROOT).extract([specimen_page(n) for n in range(1, 9)])
        self.assertEqual(schema.validate_draft(draft), [])
        self.assertEqual([e["slot"] for e in draft["encounters"]], ["T1_V2", "T2_V2", "T2_V3", "M7", "M8", "M9"])
        self.assertEqual(draft["pages"][2]["section"], "current_pregnancy")
        self.assertIn({"category": "cin", "action": "NOT_EXTRACTED"}, draft["pii_detected"])
        encounters = {e["slot"]: e["fields"] for e in draft["encounters"]}
        known = 0
        for slot, expected in self.TRUTH.items():
            for name, value in expected.items():
                field = encounters[slot][name]
                if field["field_status"] == "KNOWN":
                    known += 1
                    self.assertEqual(field["value"], value, f"{slot} {name}")
        self.assertGreaterEqual(known, 4)
        self.assertEqual(encounters["T1_V2"]["fundal_height_cm"]["field_status"], "NOT_PROVIDED")
        self.assertEqual(encounters["M9"]["syphilis_test"]["validation_flags"], ["BLANK_ON_PAGE"])
        self.assertEqual(draft["document_fields"]["midwife_patient_code"]["field_status"], "NOT_PROVIDED")

    def test_two_booklets_in_one_document_fail_clearly(self):
        with self.assertRaises(ExtractionError) as caught:
            LiveOcrExtractor(REPO_ROOT).extract([SPECIMEN.format(1), SPECIMEN.format(3), SPECIMEN.format(9)])
        self.assertEqual(caught.exception.code, "SEVERAL_BOOKLETS")


if __name__ == "__main__":
    unittest.main()
