"""Real local OCR on synthetic specimen pages, through the service and the server's OCR process pool, in a
temporary database.

Skipped unless DAYONE_REAL_OCR=1 and the OCR install is complete (docs/ocr.md):

    $env:DAYONE_REAL_OCR = "1"; .venv\\Scripts\\python -m unittest tests.test_real_ocr

Pages are checked against the manually checked ground truth (eval/specimen-ground-truth.json, docs/ocr-evaluation.md);
failures print clinical values only.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from dayone import ocr_engines, ocr_process
from dayone.extraction import ExtractionError
from dayone.live_ocr_adapter import LiveOcrAdapter

from tests.test_flow import REPO_ROOT, FlowTestCase

sys.path.insert(0, str(REPO_ROOT / "tools"))
PAGE = 3  # development split: patient 1's pregnancy grid
OCR_ENV = {"DAYONE_OCR_CPU_THREADS": "2"}  # the lowest allowed: the tests share the machine with running servers


def _skip_reason() -> str | None:
    if os.environ.get("DAYONE_REAL_OCR") != "1":
        return "set DAYONE_REAL_OCR=1 to run real OCR"
    missing = ocr_engines.create_engine("paddle").missing()
    if missing:
        return "OCR install incomplete: " + ", ".join(missing)
    return None


def _pool(**kwargs) -> ocr_process.OcrProcessPool:
    spec = ocr_process.EngineSpec("dayone.ocr_engines:create_engine", {"choice": "paddle", "environ": OCR_ENV})
    return ocr_process.OcrProcessPool(spec, workers=1, temp_root=Path(tempfile.gettempdir()) / "dayone-ocr-test-pool", **kwargs)


@unittest.skipIf(_skip_reason(), _skip_reason())
class RealOcrTest(FlowTestCase):
    def test_specimen_grid_is_read_without_confident_errors(self):
        import ocr_evaluate
        import ocr_specimen_eval

        truth = next(p for p in json.loads(ocr_evaluate.TRUTH.read_text(encoding="utf-8"))["pages"]
                     if p["page"] == PAGE)
        pool = _pool()
        self.addCleanup(pool.close)
        self.service.extractor = LiveOcrAdapter(self.service.resolve_media, ocr_engines.create_engine("paddle", environ=OCR_ENV),
                                                reader=pool, document_timeout=600)
        document_id = self.send(ocr_specimen_eval.page_png(PAGE).relative_to(REPO_ROOT).as_posix())
        self.wait_for_draft()
        draft = self.service.get_document(document_id)["draft"]
        self.assertIsNotNone(draft, self.service.get_document(document_id)["document"]["failure_reason"])
        self.assertEqual([e["slot"] for e in draft["encounters"]], truth["visit_slots"])

        rows, visits = [], []
        ocr_evaluate.score_page(truth, draft, {"page": PAGE}, rows, visits)
        wrong = [(r.get("slot"), r["field"], r["expected"], r["got"]) for r in rows
                 if r["outcome"] in ("wrong_known", "wrong_known_blank")]
        self.assertEqual(wrong, [], "KNOWN values that differ from the ground truth")
        self.assertEqual(visits[0]["shifted_values"], 0)
        known = sum(r["outcome"] == "correct_known" for r in rows if r["group"] == "grid")
        self.assertGreater(known, 30, "most clear printed-handwriting grid values should be KNOWN")

    def test_a_page_over_the_timeout_is_stopped_and_the_next_page_is_read(self):
        import ocr_specimen_eval

        pool = _pool(page_timeout=0.5)
        self.addCleanup(pool.close)
        pool.warm_up().join()
        with self.assertRaises(ExtractionError) as caught:
            pool.read(ocr_specimen_eval.page_png(PAGE))
        self.assertEqual(caught.exception.code, "OCR_TIMEOUT")
        pool.page_timeout = 120
        self.assertTrue(pool.read(ocr_specimen_eval.page_png(1)).lines)
        self.assertEqual(list(pool.work_root.glob(ocr_process.WORK_PREFIX + "*")), [])


if __name__ == "__main__":
    unittest.main()
