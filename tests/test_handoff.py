import io
import os
from contextlib import redirect_stdout
from unittest.mock import patch
from dayone.check import main as check_main
from dayone.service import Conflict, Invalid
from tests.test_flow import FlowTestCase, COVER, T1_GRID, REVIEWER


class HandoffTest(FlowTestCase):
    def pending(self):
        doc = self.send(COVER, T1_GRID)
        self.service.close_capture(doc)
        return doc

    def test_worker_failure_replay_and_manual_recovery(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        body = dict(token=job["token"], expected_revision=job["expected_revision"], code="ILLEGIBLE_IMAGE")
        self.assertFalse(self.service.fail_job(job["job_id"], **body)["replayed"])
        detail = self.service.get_document(doc)
        self.assertEqual(detail["document"]["status"], "PROCESSING_FAILED")
        self.assertTrue(self.service.fail_job(job["job_id"], **body)["replayed"])
        self.service.start_manual_entry(doc, reviewer=REVIEWER, expected_revision=detail["document"]["revision"])
        self.assertTrue(self.service.fail_job(job["job_id"], **body)["replayed"])
        self.assertEqual(self.service.get_document(doc)["document"]["status"], "NEEDS_REVIEW")

    def test_manual_entry_while_queued_rejects_late_extraction(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        detail = self.service.start_manual_entry(doc, reviewer=REVIEWER, expected_revision=job["expected_revision"])
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        with self.assertRaises(Conflict):
            self.service.complete_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], draft=self.service.extractor.extract(job["page_refs"]))
        with self.assertRaises(Conflict):
            self.service.fail_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], code="EXTRACTION_FAILED")

    def test_failure_after_lease_expiry_is_rejected(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        self.clock.advance(301)
        with self.assertRaises(Conflict):
            self.service.fail_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], code="EXTRACTION_FAILED")

    def test_arbitrary_error_text_not_persisted(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        with self.assertRaises(Invalid):
            self.service.fail_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], code="private patient text")
        self.assertEqual(self.service.get_document(doc)["document"]["status"], "PENDING_AI")

    def test_missing_config_check_does_not_print_secrets(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch("dayone.check.load_config"), redirect_stdout(output):
            self.assertEqual(check_main(), 1)
        self.assertIn("DAYONE_MONGODB_URI", output.getvalue())
