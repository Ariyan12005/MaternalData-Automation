import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dayone.crypto import Cipher
from dayone.extraction import ExtractionError, FixtureExtractor, HttpExtractor
from dayone.media import MediaStore
from dayone.service import DayOneService
from dayone.store import Store
from dayone.sync import LocalCentralRegistry
from tests.test_flow import REVIEWER, SAMPLE, FlowTestCase
from tests.test_media_retake import photo

REPO_ROOT = Path(__file__).resolve().parent.parent


class ExportAndRetentionTest(FlowTestCase):
    def make_service(self) -> DayOneService:
        return DayOneService(Store(self.db_path), FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                             grouping_window_seconds=8, clock=self.clock,
                             media=MediaStore(Path(self.tmp.name) / "media", Cipher(bytes(32))))

    def test_export_has_values_but_no_link_keys(self):
        rows = self.service.export_visits()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["patient_id"], "PAT-000001")
        self.assertEqual(rows[0]["weight_kg"], 58.8)
        text = json.dumps(rows)
        self.assertNotIn("2026823001", text)
        self.assertNotIn("raw_text", text)
        self.assertEqual(set(rows[0]), set(self.service.EXPORT_COLUMNS))

    def test_synced_camera_photos_are_purged_after_the_retention_period(self):
        self.service.central = LocalCentralRegistry(Path(self.tmp.name) / "central.log")
        fixture = self.service.extractor

        class CameraAsPageTwo:  # the camera photo stands for the identification page of the fixture
            name = "fixture"

            def extract(self, refs):
                return fixture.extract([SAMPLE[1] if ref.startswith("upload/") else ref for ref in refs])
        self.service.extractor = CameraAsPageTwo()
        ref = self.service.upload_photo(photo())["media_ref"]
        document_id = self.send(SAMPLE[0], ref, SAMPLE[2])
        self.wait_for_draft()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.service.confirm(document_id, reviewer=REVIEWER)
        self.service.tick()
        self.assertEqual(self.service.purge_media(30), 0, "too recent")
        self.clock.advance(31 * 24 * 3600)
        self.assertEqual(self.service.purge_media(30), 1)
        self.assertFalse(any((Path(self.tmp.name) / "media").iterdir()))


class FakeExtractorService(BaseHTTPRequestHandler):
    reply = (200, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeExtractorService.received = (self.headers["Authorization"], body)
        status, payload = FakeExtractorService.reply
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args):
        pass


class HttpExtractorTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeExtractorService)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.extractor = HttpExtractor(f"http://127.0.0.1:{self.server.server_port}/extract", "tok",
                                       media=lambda ref: (REPO_ROOT / ref).read_bytes())
        self.draft = FixtureExtractor(REPO_ROOT / "fixtures").extract(list(SAMPLE))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_valid_draft_is_accepted_and_images_are_sent(self):
        FakeExtractorService.reply = (200, self.draft)
        self.assertEqual(self.extractor.extract(list(SAMPLE))["encounters"][0]["slot"], "T1_V2")
        token, body = FakeExtractorService.received
        self.assertEqual(token, "Bearer tok")
        self.assertEqual([p["page_ref"] for p in body["pages"]], list(SAMPLE))

    def test_bad_answers_fail_explicitly_and_outages_are_retried(self):
        broken = json.loads(json.dumps(self.draft))
        broken["encounters"][0]["fields"]["weight_kg"]["value"] = 999
        for reply, code in (((200, broken), "INVALID_EXTRACTION"), ((422, {}), "EXTRACTOR_REFUSED"),
                            ((200, {**self.draft, "pages": self.draft["pages"][:1]}), "INVALID_EXTRACTION")):
            FakeExtractorService.reply = reply
            with self.subTest(code=code), self.assertRaises(ExtractionError) as caught:
                self.extractor.extract(list(SAMPLE))
            self.assertEqual(caught.exception.code, code)
        FakeExtractorService.reply = (503, {})
        with self.assertRaises(ConnectionError):
            self.extractor.extract(list(SAMPLE))


if __name__ == "__main__":
    unittest.main()
