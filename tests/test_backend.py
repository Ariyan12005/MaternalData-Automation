import base64
import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote
from http.server import ThreadingHTTPServer
from cryptography.fernet import Fernet, InvalidToken
from dayone.security import Cipher
from dayone.media import MediaStore
from dayone.offline import OfflineQueue
from dayone.server import Api, make_handler
from dayone.service import Conflict, Invalid, Forbidden
from dayone import linking
from tests.test_flow import FlowTestCase, COVER, T1_GRID, SENDER, REVIEWER

class BackendFlowTest(FlowTestCase):
    def test_both_keys_required_for_suggestion(self):
        draft = self.service.extractor.extract([COVER, T1_GRID])
        with self.service.store.read() as db:
            candidates = linking.find_candidates(db, "FAC-SIDI-SMAIL", draft)
        self.assertEqual(linking.suggested_patient(candidates, draft), "PAT-000001")
        draft["document_fields"]["midwife_patient_code"]["value"] = None
        self.assertIsNone(linking.suggested_patient(candidates, draft))

    def test_low_confidence_keys_not_suggested(self):
        draft = self.service.extractor.extract([COVER, T1_GRID])
        with self.service.store.read() as db:
            candidates = linking.find_candidates(db, "FAC-SIDI-SMAIL", draft)
        draft["document_fields"]["midwife_patient_code"]["confidence"] = 0.2
        self.assertIsNone(linking.suggested_patient(candidates, draft))

    def pending(self):
        doc = self.send(COVER, T1_GRID)
        self.service.close_capture(doc)
        return doc

    def test_job_exclusive_retry_and_restart(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        self.assertIsNone(self.service.claim_job(doc))
        self.service.store.close()
        self.service = self.make_service()
        self.assertIsNone(self.service.claim_job(doc))
        self.clock.advance(301)
        retry = self.service.claim_job(doc)
        self.assertEqual(job["job_id"], retry["job_id"])
        draft = self.service.extractor.extract(retry["page_refs"])
        with self.assertRaises(Forbidden):
            self.service.complete_job(job["job_id"], token=job["token"], expected_revision=0, draft=draft)
        self.assertFalse(self.service.complete_job(retry["job_id"], token=retry["token"], expected_revision=0, draft=draft)["replayed"])
        self.assertTrue(self.service.complete_job(retry["job_id"], token=retry["token"], expected_revision=0, draft=draft)["replayed"])

    def test_late_extraction_cannot_overwrite_regroup(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        page = self.service.get_document(doc)["pages"][0]["page_id"]
        self.service.move_page(page, reviewer=REVIEWER)
        with self.assertRaises(Conflict):
            self.service.complete_job(job["job_id"], token=job["token"], expected_revision=0, draft=self.service.extractor.extract(job["page_refs"]))

    def test_reject_extractor_claiming_human_review(self):
        doc = self.pending()
        job = self.service.claim_job(doc)
        draft = self.service.extractor.extract(job["page_refs"])
        draft["document_fields"]["registry_file_number"]["verification"] = {"state": "CONFIRMED", "by": "model", "at": "2026-10-03"}
        with self.assertRaises(Invalid):
            self.service.complete_job(job["job_id"], token=job["token"], expected_revision=0, draft=draft)

    def test_encrypted_upload_group_and_duplicate(self):
        self.service.media_store = MediaStore(Path(self.tmp.name) / "images", Cipher(Fernet.generate_key()))
        raw = (self.service.repo_root / COVER).read_bytes()
        body = dict(sender_id=SENDER, message_id="upload-1", image_base64=base64.b64encode(raw).decode(), group_id="batch")
        first = self.service.ingest_upload(**body)
        self.clock.advance(100)
        self.service.tick()
        body["message_id"] = "upload-2"
        second = self.service.ingest_upload(**body)
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertTrue(self.service.ingest_upload(**body)["duplicate"])
        ref = self.service.get_document(first["document_id"])["pages"][0]["media_ref"]
        self.assertEqual(self.service.read_media(ref), raw)
        encrypted = next((Path(self.tmp.name) / "images").glob("*.enc")).read_bytes()
        self.assertNotIn(raw[:30], encrypted)
        self.service.close_capture(first["document_id"])
        body["message_id"] = "upload-3"
        with self.assertRaises(Conflict):
            self.service.ingest_upload(**body)

    def modified_document(self):
        doc = self.pending()
        self.service.process_document(doc)
        for field, value in (("weight_kg", 80), ("fundal_height_cm", 30)):
            self.service.review_field(doc, reviewer=REVIEWER, scope="encounter", encounter_index=0, field=field, action="CORRECT", value=value)
        self.service.select_patient(doc, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        return doc

    def test_selected_field_update_preserves_others_and_history(self):
        doc = self.modified_document()
        before = self.service.patient_timeline("PAT-000001")["visits"][0]
        match = self.service.get_document(doc)["review"]["encounter_matches"][0]
        decision = {"action": "UPDATE", "fields": ["weight_kg"], "expected_version": match["existing_version"]}
        self.service.confirm(doc, reviewer=REVIEWER, existing_visit_decisions={"0": decision})
        after = self.service.patient_timeline("PAT-000001")["visits"][0]
        self.assertEqual(after["fields"]["weight_kg"]["value"], 80)
        self.assertEqual(after["fields"]["fundal_height_cm"], before["fields"]["fundal_height_cm"])
        self.assertEqual(after["history"][-1]["selected_fields"], ["weight_kg"])
        self.assertIn(doc, after["source_documents"])

    def test_stale_existing_visit_rejected(self):
        doc = self.modified_document()
        match = self.service.get_document(doc)["review"]["encounter_matches"][0]
        with self.service.store.tx() as db:
            db.execute("UPDATE visits SET history_json='[{\"by\":\"another-staff\"}]' WHERE visit_id=?", (match["existing_visit_id"],))
        with self.assertRaises(Conflict) as caught:
            self.service.confirm(doc, reviewer=REVIEWER, existing_visit_decisions={"0": {"action": "UPDATE", "fields": ["weight_kg"], "expected_version": match["existing_version"]}})
        self.assertEqual(caught.exception.code, "STALE_VISIT")

    def test_key_correction_requires_new_patient_selection(self):
        doc = self.modified_document()
        self.service.review_field(doc, reviewer=REVIEWER, scope="document", field="midwife_patient_code", action="CORRECT", value="NEW-CODE")
        self.assertIsNone(self.service.get_document(doc)["review"]["selection"])
        with self.assertRaises(Conflict):
            self.service.confirm(doc, reviewer=REVIEWER)

    def test_stale_regroup_is_rejected(self):
        doc = self.pending()
        detail = self.service.get_document(doc)
        self.service.process_document(doc)
        with self.assertRaises(Conflict):
            self.service.move_page(detail["pages"][0]["page_id"], reviewer=REVIEWER, expected_revision=detail["document"]["revision"])

    def test_http_revision_required(self):
        doc = self.modified_document()
        status, body = Api(self.service).dispatch("POST", f"/api/documents/{doc}/patient", {}, {"choice": "NEW"}, REVIEWER)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "REVISION_REQUIRED")

    def test_http_auth_protects_api_media_and_ui(self):
        with patch.dict(os.environ, {"DAYONE_API_TOKEN": "test-token"}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(self.service)))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                for path in ("/", "/api/documents", "/media/" + quote(COVER)):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(url + path)
                    self.assertEqual(caught.exception.code, 401)
                    caught.exception.close()
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url + "/api/system", headers={"Authorization": "Bearer x"}))
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()
                for authorization in ("Bearer test-token", "Basic " + base64.b64encode(b"dayone:test-token").decode()):
                    with urlopen(Request(url + "/api/system", headers={"Authorization": authorization})) as response:
                        self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                worker.join()
                server.server_close()

    def test_http_bridge_ignores_unknown_fields(self):
        self.service.media_store = MediaStore(Path(self.tmp.name) / "images", Cipher(Fernet.generate_key()))
        raw = (self.service.repo_root / COVER).read_bytes()
        status, body = Api(self.service).dispatch("POST", "/api/whatsapp/uploads", {}, {
            "sender_id": SENDER, "message_id": "bridge-extra", "image_base64": base64.b64encode(raw).decode(),
            "suffix": ".jpg", "group_id": "scan-extra", "timestamp": 1, "from": "meta-extra",
        }, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["position"], 1)
        status, claimed = Api(self.service).dispatch("POST", "/api/extraction/jobs/claim", {}, {
            "noise": True, "document_id": body["document_id"],
        }, REVIEWER)
        self.assertEqual(status, 200)
        self.assertIsNone(claimed)

    def test_http_bridge_handles_whitespace_and_uppercase_suffix(self):
        self.service.media_store = MediaStore(Path(self.tmp.name) / "images", Cipher(Fernet.generate_key()))
        raw = (self.service.repo_root / COVER).read_bytes()
        wrapped_b64 = "  " + base64.b64encode(raw).decode()[:50] + "\r\n  " + base64.b64encode(raw).decode()[50:] + "\n"
        status, body = Api(self.service).dispatch("POST", "/api/whatsapp/uploads", {}, {
            "sender_id": SENDER, "message_id": "bridge-wrapped", "image_base64": wrapped_b64,
            "suffix": ".JPG", "group_id": "scan-wrapped",
        }, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["position"], 1)

    def test_confirm_handles_null_decisions(self):
        doc = self.pending()
        self.service.process_document(doc)
        self.service.select_patient(doc, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        status, body = Api(self.service).dispatch("POST", f"/api/documents/{doc}/confirm", {}, {
            "expected_revision": 2, "existing_visit_decisions": None,
        }, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["patient_id"], "PAT-000001")

    def test_system_info_reports_external_extractor(self):
        saved = self.service.extractor
        try:
            self.service.extractor = None
            self.assertEqual(self.service.system_info()["extractor"], "external")
        finally:
            self.service.extractor = saved

class EncryptionTest(unittest.TestCase):
    def test_offline_restart_response_loss_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            cipher = Cipher(Fernet.generate_key())
            queue = OfflineQueue(path, cipher)
            queue.capture("private-sender", "stable-message", b"private-image", group_id="group")
            queue.capture("private-sender", "stable-message", b"private-image", group_id="group")
            with self.assertRaises(OSError):
                queue.flush(lambda body: (_ for _ in ()).throw(OSError("offline")))
            queue.close()
            self.assertNotIn(b"private-sender", path.read_bytes())
            self.assertNotIn(b"private-image", path.read_bytes())
            queue = OfflineQueue(path, cipher)
            seen = []
            self.assertEqual(queue.flush(lambda body: seen.append(body) or {"page_id": "PAGE-1", "duplicate": True}), 1)
            self.assertEqual(seen[0]["message_id"], "stable-message")
            self.assertEqual(queue.flush(lambda body: None), 0)
            queue.close()

    def test_offline_group_closure_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            cipher = Cipher(Fernet.generate_key())
            queue = OfflineQueue(path, cipher)
            queue.capture("sender", "message", b"image", group_id="batch")
            with self.assertRaises(OSError):
                queue.flush(lambda body: {"page_id": "PAGE-1", "document_id": "DOC-1"}, lambda doc: (_ for _ in ()).throw(OSError("lost closure response")))
            queue.close()
            queue = OfflineQueue(path, cipher)
            closed = []
            self.assertEqual(queue.flush(lambda body: self.fail("upload must not repeat"), lambda doc: closed.append(doc) or {"ok": True}), 0)
            self.assertEqual(closed, ["DOC-1"])
            queue.close()

    def test_cipher_rejects_wrong_key_and_tampering(self):
        cipher = Cipher(Fernet.generate_key())
        sealed = cipher.seal({"sensitive": "value"})
        with self.assertRaises(InvalidToken):
            Cipher(Fernet.generate_key()).open(sealed)
        with self.assertRaises(InvalidToken):
            cipher.open(sealed[:-5] + b"xxxxx")
        key = Fernet.generate_key().decode()
        self.assertEqual(Cipher(key + "\r\n").index("sender"), Cipher(key).index("sender"))

    def test_media_rejects_path_escape_and_fake_image(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaStore(directory, Cipher(Fernet.generate_key()))
            with self.assertRaises(ValueError):
                media.put(b"not-a-photo", ".jpg")
            with self.assertRaises(ValueError):
                media.get("secure/../../secret.jpg")

if __name__ == "__main__":
    unittest.main()
