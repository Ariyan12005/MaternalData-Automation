import tempfile
import unittest
from pathlib import Path

from dayone import schema
from dayone.extraction import ExtractionError
from dayone.live_ocr import LiveOcrExtractor, OcrLine


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

    def test_small_photo_requires_retake(self):
        extractor = LiveOcrExtractor(self.root, reader=FakeReader([]))
        with self.assertRaises(ExtractionError) as caught:
            extractor.extract(["data/Paper Registry/1-4.jpg"])
        self.assertEqual(caught.exception.code, "RETAKE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
