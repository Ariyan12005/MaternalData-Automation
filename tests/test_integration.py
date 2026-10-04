import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dayone.server import build_service
from dayone.service import Conflict
from dayone.media import MongoMediaStore
from dayone.whatsapp import hold_outbound, outbound_hold_active
from dayone import whatsapp
from tests.test_flow import COVER, T1_GRID, REPO_ROOT
from tests import test_mongo_store as mongo_tests


class StartupTest(unittest.TestCase):
    def test_external_startup_is_non_destructive_and_restarts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DAYONE_STORAGE": "sqlite", "DAYONE_EXTRACTOR": "external"}, clear=True):
            path = Path(directory) / "test.sqlite3"
            service = build_service(path)
            try:
                service.ensure_seed()
                self.assertEqual(service.list_documents(), [])
                sender = service.list_senders()[0]["sender_id"]
                page = service.ingest_photo(sender_id=sender, message_id="persist", media_ref=COVER)
                hold_outbound(service.store)
            finally:
                service.store.close()
            service = build_service(path)
            try:
                service.ensure_seed()
                self.assertEqual(service.list_documents()[0]["document_id"], page["document_id"])
                self.assertTrue(outbound_hold_active(service.store))
                self.assertIsNone(service.extractor)
            finally:
                service.store.close()

    def test_unknown_extractor_fails_before_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            for mode in ("paddle", "typo", ""):
                with self.subTest(mode=mode), patch.dict(os.environ, {"DAYONE_EXTRACTOR": mode}, clear=True):
                    with self.assertRaisesRegex(ValueError, "DAYONE_EXTRACTOR"):
                        build_service(path)
                    self.assertFalse(path.exists())


class CombinedAtlasTest(unittest.TestCase):
    setUp = mongo_tests.MongoAdapterTest.setUp
    def test_real_outbound_trigger_and_hold_survive_hydration(self):
        with self.store.tx() as db:
            db.execute("UPDATE senders SET channel='WHATSAPP'")
        hold_outbound(self.store)
        self.service.ingest_photo(sender_id="private-sender", message_id="held", media_ref=COVER)
        for _ in range(3):
            with self.store.read() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM whatsapp_outbound").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT status FROM whatsapp_outbound").fetchone()[0], "PENDING")
            self.assertTrue(outbound_hold_active(self.store))

    def test_late_failure_cannot_retry_delivered_message(self):
        self.service.enroll_sender(facility_id="FAC", facility_name="Facility", sender_id="private-sender", label="sender", channel="WHATSAPP")
        self.service.ingest_photo(sender_id="private-sender", message_id="delivered", media_ref=COVER)
        adapter = object.__new__(whatsapp.WhatsAppCloud)
        with self.store.tx() as db:
            db.execute("UPDATE whatsapp_outbound SET status='READ', wa_message_id='mock-delivery'")
            update = whatsapp.StatusUpdate("mock-delivery", "FAILED", 131000)
            self.assertEqual(adapter._apply_status(db, update, self.service.clock()), 0)
            self.assertEqual(db.execute("SELECT status FROM whatsapp_outbound").fetchone()[0], "READ")

    def test_encrypted_retake_invalidates_lease_and_cleans_temporary_media(self):
        media = MongoMediaStore(self.store.db, self.store.cipher)
        self.service.media_store = media
        refs = [media.put((REPO_ROOT / ref).read_bytes(), ".jpg") for ref in (COVER, T1_GRID)]
        for index, ref in enumerate(refs):
            result = self.service.ingest_photo(sender_id="private-sender", message_id=f"secure-{index}", media_ref=ref)
        doc = result["document_id"]
        self.service.close_capture(doc)
        job = self.service.claim_job(doc)
        draft = mongo_tests._rewrite_pages(self.service.extractor.extract([COVER, T1_GRID]), refs)
        self.service.request_retake(result["page_id"], reviewer="tester")
        with self.store.read() as db:
            request_id = db.execute("SELECT request_id FROM retake_requests").fetchone()[0]
        replacement = self.service.ingest_photo(sender_id="private-sender", message_id="replacement", media_ref=refs[1], retake_request_id=request_id)
        with self.assertRaises(Conflict):
            self.service.complete_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], draft=draft)
        with self.assertRaises(Conflict):
            self.service.fail_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], code="EXTRACTION_FAILED")
        self.assertNotEqual(self.service.claim_job(doc)["job_id"], job["job_id"])
        self.assertEqual(media.get(refs[1]), (REPO_ROOT / T1_GRID).read_bytes())
        paths = []
        with self.assertRaisesRegex(RuntimeError, "OCR failure"):
            with self.service.extraction_media(refs) as materialized:
                paths = list(materialized.values())
                self.assertEqual(materialized[refs[0]].read_bytes(), media.get(refs[0]))
                raise RuntimeError("OCR failure")
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT replaced_by FROM pages WHERE page_id=?", (result["page_id"],)).fetchone()[0], replacement["page_id"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 0)
