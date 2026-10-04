import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from dayone.crypto import Cipher, DecryptionError
from dayone.extraction import ExtractionError, FixtureExtractor
from dayone.media import MediaError, MediaStore
from dayone.service import DEMO_SENDER, Conflict, DayOneService, Invalid
from dayone.store import Store
from tests.test_flow import COVER, GRID, REVIEWER, SAMPLE, FlowTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
SENDER = DEMO_SENDER["sender_id"]
KEY = bytes(range(32))


def photo(colour=(200, 200, 200), size=(900, 1200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


class EncryptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_media_is_encrypted_and_bound_to_its_reference(self):
        store = MediaStore(self.root, Cipher(KEY))
        data = photo()
        ref = store.put(data)
        self.assertTrue(ref.startswith("upload/") and ref.endswith(".jpg"))
        stored = next(self.root.iterdir()).read_bytes()
        self.assertNotIn(data[:64], stored)
        self.assertEqual(store.get(ref), data)
        with self.assertRaises(DecryptionError):
            MediaStore(self.root, Cipher(bytes(32))).get(ref)

    def test_only_jpeg_and_png_are_accepted(self):
        with self.assertRaises(MediaError) as caught:
            MediaStore(self.root).put(b"GIF89a....")
        self.assertEqual(caught.exception.code, "MEDIA_TYPE")
        with self.assertRaises(MediaError):
            MediaStore(self.root).get("../etc/passwd")

    def test_database_file_is_unreadable_without_the_key(self):
        path = self.root / "db.sqlite3"
        store = Store(path, Cipher(KEY))
        with store.tx() as db:
            db.execute("INSERT INTO facilities(facility_id, name) VALUES ('F1', 'C/S secret name')")
        store.close()
        raw = path.read_bytes()
        self.assertNotIn(b"secret name", raw)
        self.assertNotIn(b"SQLite format 3", raw)
        reopened = Store(path, Cipher(KEY))
        with reopened.read() as db:
            self.assertEqual(db.execute("SELECT name FROM facilities").fetchone()[0], "C/S secret name")
        reopened.close()
        with self.assertRaises(DecryptionError):
            Store(path, Cipher(bytes(32)))

    def test_plain_database_is_converted_on_first_encrypted_open(self):
        path = self.root / "old.sqlite3"
        Store(path).close()
        plain = sqlite3.connect(path)
        plain.execute("INSERT INTO facilities(facility_id, name) VALUES ('F1', 'kept')")
        plain.commit()
        plain.close()
        for _ in range(3):  # the converted file must reopen (it used to keep the WAL flag and fail)
            store = Store(path, Cipher(KEY))
            with store.read() as db:
                self.assertEqual(db.execute("SELECT name FROM facilities").fetchone()[0], "kept")
            store.close()
        self.assertFalse(path.read_bytes().startswith(b"SQLite format 3"))


class RetakeTest(FlowTestCase):
    def make_service(self) -> DayOneService:
        self.media_root = Path(self.tmp.name) / "media"
        return DayOneService(Store(self.db_path), FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                             grouping_window_seconds=8, clock=self.clock,
                             media=MediaStore(self.media_root, Cipher(KEY)))

    def test_camera_photo_is_ingested_like_any_page(self):
        ref = self.service.upload_photo(photo())["media_ref"]
        result = self.service.ingest_photo(sender_id=SENDER, message_id="wamid.cam-1", media_ref=ref)
        self.assertEqual(self.service.read_media(ref)[:3], b"\xff\xd8\xff")
        self.wait_for_draft()
        detail = self.service.get_document(result["document_id"])
        self.assertEqual(detail["document"]["status"], "PROCESSING_FAILED")  # fixture mode has no draft for it
        with self.assertRaises(Invalid):
            self.service.upload_photo(b"not an image")

    def test_retake_request_then_new_photo_replaces_the_page(self):
        document_id = self.send(*SAMPLE)
        self.wait_for_draft()
        page = self.service.get_document(document_id)["pages"][2]
        detail = self.service.request_retake(page["page_id"], reviewer=REVIEWER, reason="photo floue")
        self.assertEqual(detail["document"]["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(detail["next_step"], "WAITING_RETAKE")
        self.assertEqual(self.service.thread(SENDER)[-1]["body"], "Merci de reprendre la photo de la page 3 : photo floue")
        with self.assertRaises(Conflict):
            self.service.confirm(document_id, reviewer=REVIEWER)

        result = self.service.ingest_photo(sender_id=SENDER, message_id="wamid.retake", media_ref=GRID)
        self.assertEqual((result["document_id"], result["position"]), (document_id, 3))
        self.assertEqual(result["replaced_page_id"], page["page_id"])
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "PENDING_AI")
        self.assertEqual(len(detail["pages"]), 3)
        self.assertNotIn(page["page_id"], [p["page_id"] for p in detail["pages"]])
        self.service.tick()
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "NEEDS_REVIEW")

    def test_unusable_photo_triggers_an_automatic_retake_message(self):
        class Blurry:
            name = "blurry"

            def extract(self, refs):
                raise ExtractionError("RETAKE_REQUIRED", "Photo trop sombre.", page_ref=refs[1])
        self.service.extractor = Blurry()
        document_id = self.send(COVER, GRID)
        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual([p["position"] for p in detail["pages"] if p["retake_requested_at"]], [2])
        self.assertEqual(self.service.thread(SENDER)[-1]["body"], "Merci de reprendre la photo de la page 2 : Photo trop sombre.")


if __name__ == "__main__":
    unittest.main()
