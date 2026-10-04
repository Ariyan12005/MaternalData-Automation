import tempfile
import unittest
from pathlib import Path

from dayone import schema
from dayone.extraction import ExtractionError
from dayone.live_ocr import LiveOcrExtractor, OcrLine, PaddleOcrReader, TesseractOcrReader, _section


class FakeReader:
    def __init__(self, lines):
        self.lines = lines

    def read(self, image_path):
        return self.lines


class LiveOcrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        directory = self.root / "data" / "Paper Registry"
        directory.mkdir(parents=True)
        # The quality check is replaced in tests; this file only gives the adapter a safe path.
        (directory / "1-4.jpg").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_unclear_clinical_values_go_to_review(self):
        extractor = LiveOcrExtractor(self.root, reader=FakeReader([OcrLine("TA", 0.99)]))
        from unittest.mock import patch
        with patch("dayone.live_ocr.assess_photo"):
            draft = extractor.extract(["data/Paper Registry/1-4.jpg"])
        self.assertEqual(schema.validate_draft(draft), [])
        self.assertEqual(draft["encounters"][0]["fields"]["visit_date"]["field_status"], "NEEDS_REVIEW")
        self.assertEqual(draft["encounters"][0]["fields"]["systolic_bp_mmhg"]["value"], None)

    def test_clear_bp_is_known_but_other_values_stay_reviewable(self):
        extractor = LiveOcrExtractor(self.root, reader=FakeReader([OcrLine("TA 12/7", 0.97)]))
        from unittest.mock import patch
        with patch("dayone.live_ocr.assess_photo"):
            draft = extractor.extract(["data/Paper Registry/1-4.jpg"])
        fields = draft["encounters"][0]["fields"]
        self.assertEqual(fields["systolic_bp_mmhg"]["value"], 120)
        self.assertEqual(fields["diastolic_bp_mmhg"]["value"], 70)
        self.assertEqual(fields["systolic_bp_mmhg"]["field_status"], "KNOWN")
        self.assertEqual(fields["hiv_test"]["field_status"], "NEEDS_REVIEW")

    def test_historical_date_is_never_mistaken_for_blood_pressure(self):
        extractor = LiveOcrExtractor(self.root, reader=FakeReader([OcrLine("12/11/2022", 0.99)]))
        from unittest.mock import patch
        with patch("dayone.live_ocr.assess_photo"):
            draft = extractor.extract(["data/Paper Registry/1-2.jpg"])
        fields = draft["encounters"][0]["fields"]
        self.assertIsNone(fields["systolic_bp_mmhg"]["value"])
        self.assertIsNone(fields["diastolic_bp_mmhg"]["value"])

    def test_small_photo_requires_retake(self):
        extractor = LiveOcrExtractor(self.root, reader=FakeReader([]))
        with self.assertRaises(ExtractionError) as caught:
            extractor.extract(["data/Paper Registry/1-4.jpg"])
        self.assertEqual(caught.exception.code, "RETAKE_REQUIRED")

    def test_specimen_page_layouts_are_recognized(self):
        self.assertEqual(_section("data/Paper Registry/dossiers_specimen_10_patientes-01.png"), "cover")
        self.assertEqual(_section("data/Paper Registry/dossiers_specimen_10_patientes-02.png"), "identification")
        self.assertEqual(_section("data/Paper Registry/dossiers_specimen_10_patientes-03.png"), "current_pregnancy")

    def test_specimen_latest_visit_uses_table_coordinates(self):
        lines = [
            OcrLine("18/01/2026", 0.95, 1428, 450), OcrLine("38", 0.95, 1427, 553), OcrLine("SA", 0.95, 1453, 548),
            OcrLine("66.8", 0.95, 1426, 652), OcrLine("110/60", 0.95, 1427, 698), OcrLine("34", 0.95, 1427, 997),
        ]
        extractor = LiveOcrExtractor(self.root, reader=FakeReader(lines))
        from unittest.mock import patch
        with patch("dayone.live_ocr.assess_photo"):
            draft = extractor.extract(["data/Paper Registry/dossiers_specimen_10_patientes-03.png"])
        fields = draft["encounters"][0]["fields"]
        self.assertEqual(draft["encounters"][0]["slot"], "M9")
        self.assertEqual(fields["visit_date"]["value"], "2026-01-18")
        self.assertEqual(fields["gestational_age_days"]["value"], 266)
        self.assertEqual(fields["weight_kg"]["value"], 66.8)
        self.assertEqual(fields["systolic_bp_mmhg"]["value"], 110)

    def test_malformed_cells_never_supply_partial_values(self):
        from unittest.mock import patch
        ref = "data/Paper Registry/dossiers_specimen_10_patientes-03.png"
        for text, top, name in [
            ("14/09/2024/7", 450, "visit_date"),
            ("72.4?", 650, "weight_kg"),
            ("120/80/2024", 700, "systolic_bp_mmhg"),
            ("31 SA + 9 j", 550, "gestational_age_days"),
            ("29 30", 997, "fundal_height_cm"),
        ]:
            with self.subTest(text=text), patch("dayone.live_ocr.assess_photo"):
                draft = LiveOcrExtractor(self.root, FakeReader([OcrLine(text, 0.99, 1420, top)])).extract([ref])
                field = draft["encounters"][0]["fields"][name]
                self.assertIsNone(field["value"])
                self.assertEqual(field["field_status"], "NEEDS_REVIEW")

    def test_low_confidence_token_blocks_entire_cell(self):
        from unittest.mock import patch
        ref = "data/Paper Registry/dossiers_specimen_10_patientes-03.png"
        lines = [OcrLine("31", 0.99, 1420, 550), OcrLine("SA", 0.40, 1460, 550)]
        with patch("dayone.live_ocr.assess_photo"):
            draft = LiveOcrExtractor(self.root, FakeReader(lines)).extract([ref])
        self.assertIsNone(draft["encounters"][0]["fields"]["gestational_age_days"]["value"])

    def test_unlabelled_number_pair_is_not_blood_pressure(self):
        from unittest.mock import patch
        with patch("dayone.live_ocr.assess_photo"):
            draft = LiveOcrExtractor(self.root, FakeReader([OcrLine("12/8", 0.99)])).extract(["data/Paper Registry/1-4.jpg"])
        self.assertIsNone(draft["encounters"][0]["fields"]["systolic_bp_mmhg"]["value"])

    def test_cover_candidate_from_other_page_is_rejected(self):
        from unittest.mock import patch
        class Reader:
            def read(self, path):
                return [] if path.name.endswith("01.png") else [OcrLine("N° fiche 987654", 0.99)]
        refs = [f"data/Paper Registry/dossiers_specimen_10_patientes-{n:02}.png" for n in (1, 2)]
        with patch("dayone.live_ocr.assess_photo"):
            draft = LiveOcrExtractor(self.root, Reader()).extract(refs)
        self.assertIsNone(draft["document_fields"]["registry_file_number"]["value"])

    def test_postpartum_bp_does_not_become_antenatal_measurement(self):
        from unittest.mock import patch
        ref = "data/Paper Registry/dossiers_specimen_10_patientes-05.png"
        with patch("dayone.live_ocr.assess_photo"):
            draft = LiveOcrExtractor(self.root, FakeReader([OcrLine("TA 13/8", 0.99)])).extract([ref])
        self.assertIsNone(draft["encounters"][0]["fields"]["systolic_bp_mmhg"]["value"])

    def test_paddle_adapter_preserves_text_score_and_coordinates(self):
        class Engine:
            def predict(self, path):
                return [{"rec_texts": ["31 SA", "invalid"],
                         "rec_scores": [0.96, float("nan")],
                         "rec_polys": [[[1420, 548], [1490, 548], [1490, 570], [1420, 570]],
                                       [[0, 0], [1, 0], [1, 1], [0, 1]]]}]
        reader = PaddleOcrReader(Engine())
        self.assertEqual(reader.read(Path("unused.png")), [OcrLine("31 SA", 0.96, 1420, 548)])

    def test_paddle_inference_failure_has_explicit_error(self):
        class Engine:
            def predict(self, path):
                raise RuntimeError("failure")
        with self.assertRaises(ExtractionError) as caught:
            PaddleOcrReader(Engine()).read(Path("unused.png"))
        self.assertEqual(caught.exception.code, "OCR_FAILED")

    def test_paddle_mismatched_results_are_rejected(self):
        class Engine:
            def predict(self, path):
                return [{"rec_texts": ["31 SA"], "rec_scores": [], "rec_polys": []}]
        with self.assertRaises(ExtractionError) as caught:
            PaddleOcrReader(Engine()).read(Path("unused.png"))
        self.assertEqual(caught.exception.code, "OCR_INVALID_RESULT")

    def test_hyphenated_registry_number_is_kept_whole(self):
        from unittest.mock import patch
        ref = "data/Paper Registry/dossiers_specimen_10_patientes-01.png"
        cases = [
            ("N° de la fiche : 2026-823-001", 0.95, "2026823001"),
            ("N° de la fiche ：2026-823-001", 0.95, "2026823001"),
            ("N° de la fiche : 2026-823-001", 0.60, None),
            ("N° de la fiche : 2026-823-001 bis", 0.95, None),
        ]
        for text, confidence, expected in cases:
            with self.subTest(text=text, confidence=confidence), patch("dayone.live_ocr.assess_photo"):
                draft = LiveOcrExtractor(self.root, FakeReader([OcrLine(text, confidence)])).extract([ref])
                field = draft["document_fields"]["registry_file_number"]
                self.assertEqual(field["value"], expected)
                self.assertEqual(field["field_status"], "KNOWN" if expected else "NEEDS_REVIEW")

    def test_pages_read_in_parallel_keep_their_page_refs(self):
        from unittest.mock import patch
        class Reader:
            def read(self, path):
                return [OcrLine(path.name, 0.99)]
        refs = [f"data/Paper Registry/dossiers_specimen_10_patientes-{n:02}.png" for n in (1, 2, 3)]
        extractor = LiveOcrExtractor(self.root, Reader())
        from dayone import live_ocr
        captured, parse = [], live_ocr._parse_encounter
        with patch("dayone.live_ocr.assess_photo"), \
                patch("dayone.live_ocr._parse_encounter", side_effect=lambda lines, refs: captured.extend(lines) or parse(lines, refs)):
            extractor.extract(refs)
        self.assertEqual([(line.text, line.page_ref) for line in captured],
                         [(Path(ref).name, ref) for ref in refs])

    def test_tesseract_adapter_reads_words_and_coordinates(self):
        from unittest.mock import patch
        import subprocess
        tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
               "5\t1\t1\t1\t1\t1\t1427\t698\t60\t20\t95.5\t110/60\n"
               "5\t1\t1\t1\t1\t2\t10\t10\t5\t5\t-1\t \n")
        completed = subprocess.CompletedProcess([], 0, stdout=tsv, stderr="")
        with patch("dayone.live_ocr.subprocess.run", return_value=completed) as run:
            lines = TesseractOcrReader().read(Path("page.png"))
        self.assertEqual(lines, [OcrLine("110/60", 0.955, 1427, 698)])
        self.assertEqual(run.call_args.kwargs["env"]["OMP_THREAD_LIMIT"], "1")

    def test_missing_tesseract_has_explicit_error(self):
        from unittest.mock import patch
        with patch("dayone.live_ocr.subprocess.run", side_effect=FileNotFoundError("tesseract")):
            with self.assertRaises(ExtractionError) as caught:
                TesseractOcrReader().read(Path("page.png"))
        self.assertEqual(caught.exception.code, "OCR_DEPENDENCY_MISSING")


if __name__ == "__main__":
    unittest.main()
