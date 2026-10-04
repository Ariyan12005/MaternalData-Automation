"""Local OCR behind the extraction contract (dayone/live_ocr_adapter.py), with a fake page reader.

No OCR package is needed and nothing leaves the machine: the fake reader stands in for the OCR process pool. It
returns synthetic page scans from tests/test_live_ocr.py and records the bytes of each page it was given. Each test
uses its own temporary database.
"""

import hashlib
import io
import os
from pathlib import Path
from unittest import mock

from dayone import live_ocr, server, whatsapp
from dayone.extraction import ExtractionError, FixtureExtractor, PhotoRejected
from dayone.extraction_pool import ExtractionPool
from dayone.live_ocr_adapter import LiveOcrAdapter
from dayone.server import build_service
from dayone.server import main as server_main
from dayone.service import INBOUND_MEDIA_PREFIX, Conflict, DayOneService
from dayone.store import Store

from tests.test_flow import REPO_ROOT, REVIEWER, SENDER, FlowTestCase
from tests.test_live_ocr import VISITS, box, cover_page, grid_page, identification_page, page_scan

SPECIMEN_COVER = "data/Paper Registry/dossiers_specimen_10_patientes-01.png"
SPECIMEN_PREGNANCY = "data/Paper Registry/dossiers_specimen_10_patientes-03.png"


class FakeEngine:
    name = "paddleocr"
    version = "paddleocr-fake"
    known_confidence = 0.97

    def missing(self):
        return []


class FakeReader:
    """Page reader: returns scripted results in order (default: the grid page), then the default."""

    def __init__(self):
        self.results = []
        self.default = page_scan(grid_page(VISITS))
        self.seen: list[bytes] = []
        self.forced: list[bool] = []
        self.before = None

    def read(self, path, *, force=False):
        self.seen.append(Path(path).read_bytes())
        self.forced.append(force)
        if self.before:
            before, self.before = self.before, None
            before()
        result = self.results.pop(0) if self.results else self.default
        if isinstance(result, Exception):
            raise result
        return result


class LiveOcrAdapterTestCase(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.engine = FakeEngine()
        self.reader = FakeReader()
        self.service.extractor = LiveOcrAdapter(self.service.extraction_media, self.engine, reader=self.reader)
        self.photo_number = 0

    @property
    def media_dir(self) -> Path:
        return (Path(self.tmp.name) / "media").resolve()

    def make_service(self) -> DayOneService:
        self.media_dir.mkdir(exist_ok=True)
        return DayOneService(Store(self.db_path), FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                             grouping_window_seconds=8, clock=self.clock, inbound_media_dir=self.media_dir)

    def whatsapp_photo(self) -> tuple[str, bytes]:
        """A synthetic downloaded WhatsApp photo, stored like the Cloud API adapter stores media."""
        self.photo_number += 1
        content = f"synthetic test photo {self.photo_number}".encode() * 1000
        name = f"{hashlib.sha256(content).hexdigest()}.jpg"
        (self.media_dir / name).write_bytes(content)
        return f"{INBOUND_MEDIA_PREFIX}{name}", content

    def status(self, document_id: str) -> tuple:
        document = self.service.get_document(document_id)["document"]
        return document["status"], document["failure_reason"]

    def pages(self, document_id: str) -> list[dict]:
        return self.service.get_document(document_id)["pages"]

    def extract_in_pool(self) -> None:
        """Extract the queued documents the way the server does: on the extraction pool's thread."""
        pool = ExtractionPool(self.service)
        try:
            pool.submit(self.service.queued_documents())
            self.assertTrue(pool.wait_idle(10))
        finally:
            pool.shutdown()

    def last_message(self) -> str:
        return self.service.thread(SENDER)[-1]["body"]

    def assertOnlyCoverMissing(self, document_id: str):
        """A grid page alone: the cover was not photographed, so its fields wait for a person (not 'not provided')."""
        detail = self.service.get_document(document_id)
        self.assertEqual((detail["document"]["status"], detail["document"]["failure_reason"]), ("NEEDS_REVIEW", None))
        self.assertEqual({(b["scope"], b["field"]) for b in detail["review"]["blocking"]},
                         {("document", "registry_file_number"), ("document", "facility_name"),
                          ("document", "midwife_patient_code")})
        fields = detail["draft"]["document_fields"]
        self.assertEqual({fields[name]["validation_flags"][0] for name in ("registry_file_number", "facility_name")},
                         {"SECTION_NOT_CAPTURED"})


class SuccessfulExtractionTest(LiveOcrAdapterTestCase):
    def test_specimen_pages_give_a_reviewable_draft_without_resetting_the_demo(self):
        visits = self.visit_count()
        document_id = self.send(SPECIMEN_PREGNANCY)
        self.wait_for_draft()

        draft = self.service.get_document(document_id)["draft"]
        self.assertOnlyCoverMissing(document_id)
        self.assertEqual(self.visit_count(), visits)
        self.assertEqual(draft["extraction"]["extractor"], "paddleocr")
        self.assertEqual([e["slot"] for e in draft["encounters"]], ["T1_V1", "T2_V1", "M7"])
        self.assertEqual(draft["encounters"][0]["fields"]["systolic_bp_mmhg"]["value"], 120)
        self.assertEqual(self.reader.seen, [(REPO_ROOT / SPECIMEN_PREGNANCY).read_bytes()])
        self.assertIn("PAT-000001", [p["patient_id"] for p in self.service.list_patients()])

    def test_whatsapp_photos_reach_the_reader_under_their_page_ref(self):
        ref, content = self.whatsapp_photo()
        document_id = self.send(ref)
        self.wait_for_draft()

        draft = self.service.get_document(document_id)["draft"]
        self.assertOnlyCoverMissing(document_id)
        self.assertEqual((draft["pages"][0]["page_ref"], draft["pages"][0]["section"]), (ref, "current_pregnancy"))
        self.assertEqual(draft["encounters"][0]["fields"]["weight_kg"]["source"]["page_ref"], ref)
        self.assertEqual(self.reader.seen, [content])

    def test_demo_seed_never_runs_ocr(self):
        self.service.reset_demo()
        self.service.ensure_seed()
        self.assertEqual(self.reader.seen, [])


class RetakeTest(LiveOcrAdapterTestCase):
    def test_unusable_photo_fails_with_its_reason_then_a_retake_is_extracted(self):
        self.reader.results = [PhotoRejected("IMAGE_TOO_DARK")]
        document_id = self.send(self.whatsapp_photo()[0])
        self.wait_for_draft()
        self.assertEqual(self.status(document_id), ("PROCESSING_FAILED", "RETAKE_REQUIRED"))
        document = self.service.get_document(document_id)["document"]
        page_id = self.pages(document_id)[0]["page_id"]
        self.assertIn("sombre", document["failure_message"])
        self.assertEqual(document["failure_pages"], [{"page_id": page_id, "position": 1, "reason": "IMAGE_TOO_DARK"}])

        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        self.assertEqual(self.last_message(), "Merci de reprendre la photo de la page 1 : "
                         + live_ocr.RETAKE_REASONS["IMAGE_TOO_DARK"])
        replacement, content = self.whatsapp_photo()
        self.service.ingest_photo(sender_id=SENDER, message_id="wamid.retake-1", media_ref=replacement,
                                  retake_request_id=request_id)
        self.assertEqual(self.status(document_id)[0], "PENDING_AI")

        self.assertEqual(self.service.tick(), [document_id])
        detail = self.service.get_document(document_id)
        self.assertOnlyCoverMissing(document_id)
        self.assertEqual(detail["draft"]["pages"][0]["page_ref"], replacement)
        self.assertEqual(self.reader.seen[-1], content)

    def test_one_unusable_page_of_two_is_reviewed_and_its_retake_message_names_the_problem(self):
        self.reader.results = [self.reader.default, PhotoRejected("IMAGE_BLURRY")]
        document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual([(p["read"], p["review_reason"]) for p in detail["draft"]["pages"]],
                         [(True, None), (False, "PHOTO_UNUSABLE")])
        self.service.request_retake(self.pages(document_id)[1]["page_id"], reviewer=REVIEWER)
        self.assertEqual(self.last_message(), "Merci de reprendre la photo de la page 2 : "
                         + live_ocr.RETAKE_REASONS["IMAGE_BLURRY"])

    def test_replacement_arriving_during_extraction_restarts_it_with_the_new_photo(self):
        first, first_content = self.whatsapp_photo()
        second, second_content = self.whatsapp_photo()
        document_id = self.send(first, second)
        self.service.set_ai_available(False)
        self.wait_for_draft()
        page_id = self.pages(document_id)[1]["page_id"]
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        self.service.set_ai_available(True)
        replacement, content = self.whatsapp_photo()
        # The replacement arrives while page 1 is being read.
        self.reader.before = lambda: self.service.ingest_photo(
            sender_id=SENDER, message_id="wamid.retake-1", media_ref=replacement, retake_request_id=request_id)

        self.extract_in_pool()
        self.assertEqual(self.reader.seen, [first_content, first_content, content],
                         "the replaced photo is never read, and the document is extracted again at once")
        draft = self.service.get_document(document_id)["draft"]
        self.assertEqual([p["page_ref"] for p in draft["pages"]], [first, replacement])
        self.assertNotIn(second_content, self.reader.seen)

    def test_result_for_pages_replaced_during_the_last_page_is_discarded(self):
        document_id = self.send(self.whatsapp_photo()[0])
        self.service.set_ai_available(False)
        self.wait_for_draft()
        page_id = self.pages(document_id)[0]["page_id"]
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        self.service.set_ai_available(True)
        replacement, content = self.whatsapp_photo()
        self.reader.before = lambda: self.service.ingest_photo(
            sender_id=SENDER, message_id="wamid.retake-1", media_ref=replacement, retake_request_id=request_id)

        self.assertEqual(self.service.tick(), [], "OCR of the replaced photo must not be saved")
        detail = self.service.get_document(document_id)
        self.assertEqual((detail["document"]["status"], detail["draft"]), ("PENDING_AI", None))

        self.assertEqual(self.service.tick(), [document_id])
        self.assertEqual(self.service.get_document(document_id)["draft"]["pages"][0]["page_ref"], replacement)
        self.assertEqual(self.reader.seen[-1], content)


class SectionTest(LiveOcrAdapterTestCase):
    def test_unknown_layout_waits_for_the_reviewer_then_is_read_as_chosen(self):
        unknown = page_scan([box("Observations", 50, 50), box("Page 12", 50, 100)])
        self.reader.results = [self.reader.default, unknown]
        document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual(detail["review"]["blocking"][0]["reason"], "UNKNOWN_LAYOUT")

        page_id = self.pages(document_id)[1]["page_id"]
        self.reader.results = [self.reader.default, unknown]
        detail = self.service.set_page_section(page_id, reviewer=REVIEWER, section="identification")
        self.assertEqual((detail["document"]["status"], detail["pages"][1]["section_hint"]),
                         ("PENDING_AI", "identification"))
        self.assertEqual(self.service.tick(), [document_id])
        draft = self.service.get_document(document_id)["draft"]
        self.assertEqual((draft["pages"][1]["section"], draft["pages"][1]["section_source"]),
                         ("identification", "reviewer"))
        self.assertEqual(self.reader.forced[-2:], [False, True], "a page whose section was chosen is always read")
        self.assertNotIn("page", {b["scope"] for b in self.service.get_document(document_id)["review"]["blocking"]})

    def test_section_changed_during_extraction_restarts_it(self):
        document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.service.set_ai_available(False)
        self.wait_for_draft()
        self.service.set_ai_available(True)
        page_id = self.pages(document_id)[1]["page_id"]
        self.reader.before = lambda: self.service.set_page_section(page_id, reviewer=REVIEWER, section="cover")
        self.extract_in_pool()
        self.assertEqual(len(self.reader.seen), 3)
        draft = self.service.get_document(document_id)["draft"]
        self.assertEqual(draft["pages"][1]["section_source"], "reviewer")

    def test_section_change_needs_the_revision_the_reviewer_saw(self):
        document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()
        seen = self.service.get_document(document_id)["document"]["revision"]
        page_id = self.pages(document_id)[1]["page_id"]
        detail = self.service.set_page_section(page_id, reviewer=REVIEWER, section="cover", expected_revision=seen)
        self.assertGreater(detail["document"]["revision"], seen)
        with self.assertRaises(Conflict) as caught:
            self.service.set_page_section(page_id, reviewer=REVIEWER, section="identification",
                                          expected_revision=seen)
        self.assertEqual(caught.exception.code, "STALE_REVISION")
        self.assertEqual(self.pages(document_id)[1]["section_hint"], "cover", "the stale choice changed nothing")

        current = detail["document"]["revision"]
        detail = self.service.set_page_section(page_id, reviewer=REVIEWER, section="identification",
                                               expected_revision=current)
        self.assertGreater(detail["document"]["revision"], current, "a queued document's revision moves too")
        status, body = server.Api(self.service).dispatch(
            "POST", f"/api/pages/{page_id}/section", {}, {"section": "cover", "expected_revision": current}, REVIEWER)
        self.assertEqual((status, body["error"]["code"]), (409, "STALE_REVISION"))

    def test_unknown_section_is_refused(self):
        document_id = self.send(self.whatsapp_photo()[0])
        with self.assertRaises(Exception) as caught:
            self.service.set_page_section(self.pages(document_id)[0]["page_id"], reviewer=REVIEWER, section="x")
        self.assertEqual(caught.exception.code, "UNKNOWN_SECTION")


class FailureTest(LiveOcrAdapterTestCase):
    def test_missing_ocr_install_keeps_the_document_queued(self):
        self.reader.results = [ExtractionError("OCR_DEPENDENCY_MISSING", "PaddleOCR incomplet : paddleocr")]
        document_id = self.send(SPECIMEN_PREGNANCY)
        with self.assertLogs("dayone.service", "WARNING") as logs:
            self.wait_for_draft()
        self.assertIn("OCR_DEPENDENCY_MISSING", logs.output[0])
        self.assertEqual(self.status(document_id), ("PENDING_AI", None))
        retry = self.service.get_document(document_id)["document"]["extraction_retry"]
        self.assertEqual((retry["attempts"], retry["reason"]), (1, "OCR_DEPENDENCY_MISSING"))
        listed = next(d for d in self.service.list_documents() if d["document_id"] == document_id)
        self.assertEqual((listed["retrying"], listed["extracting"]), (True, False))

        self.assertEqual(self.service.tick(), [document_id])
        self.assertOnlyCoverMissing(document_id)
        self.assertIsNone(self.service.extraction_retry(document_id), "a finished extraction clears the retry")

    def test_timed_out_page_is_reviewed_and_a_timed_out_document_fails(self):
        timeout = ExtractionError("OCR_TIMEOUT", "La lecture de la page a dépassé 120 s.")
        self.reader.results = [self.reader.default, timeout]
        two = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()
        self.assertEqual(self.service.get_document(two)["review"]["blocking"][0]["reason"], "OCR_TIMEOUT")

        self.clock.advance(60)
        self.reader.results = [timeout]
        one = self.send(self.whatsapp_photo()[0])
        self.wait_for_draft()
        self.assertEqual(self.status(one), ("PROCESSING_FAILED", "OCR_TIMEOUT"))

    def test_ocr_crash_goes_to_manual_entry_without_logging_page_text(self):
        self.reader.results = [RuntimeError("could not read 'Inventée Exemple'")]
        document_id = self.send(SPECIMEN_PREGNANCY)
        with self.assertLogs("dayone", "ERROR") as logs:
            self.wait_for_draft()
        self.assertNotIn("Inventée", "\n".join(logs.output))
        self.assertEqual(self.status(document_id), ("PROCESSING_FAILED", "OCR_FAILED"))
        self.service.tick()
        self.assertEqual(len(self.reader.seen), 1, "a failed page is not read again")
        manual = self.service.start_manual_entry(document_id, reviewer=REVIEWER)
        self.assertEqual(manual["draft"]["extraction"]["extractor"], "manual")

    def test_vanished_media_is_permanent(self):
        ref, _ = self.whatsapp_photo()
        document_id = self.send(ref)
        (self.media_dir / ref.rsplit("/", 1)[1]).unlink()
        self.wait_for_draft()
        self.assertEqual(self.status(document_id), ("PROCESSING_FAILED", "MEDIA_NOT_FOUND"))
        self.assertEqual(self.reader.seen, [])

    def test_drafts_outside_the_contract_are_rejected(self):
        build = live_ocr.extract_draft

        def extra_field(engine, pages, **kwargs):
            d = build(engine, pages, **kwargs)
            d["encounters"][0]["fields"]["hemoglobin_g_dl"] = dict(d["encounters"][0]["fields"]["weight_kg"])
            return d

        def other_pages(engine, pages, **kwargs):
            return build(engine, [(SPECIMEN_COVER, path) for _ref, path in pages], **kwargs)

        def human_claim(engine, pages, **kwargs):
            d = build(engine, pages, **kwargs)
            d["encounters"][0]["fields"]["weight_kg"]["verification"] = {"state": "CONFIRMED", "by": "ocr",
                                                                         "at": "2026-10-03T18:00:00+00:00"}
            return d

        def invalid(engine, pages, **kwargs):
            raise ExtractionError("INVALID_LIVE_OCR_DRAFT", "encounters: missing")

        cases = [("field outside v1.0", extra_field, "EXTRACTOR_SCHEMA_MISMATCH"),
                 ("other pages", other_pages, "EXTRACTOR_PAGE_MISMATCH"),
                 ("claims verification", human_claim, "EXTRACTOR_CLAIMS_VERIFICATION"),
                 ("draft fails the schema", invalid, "INVALID_LIVE_OCR_DRAFT")]
        for label, extract, code in cases:
            with self.subTest(label), mock.patch("dayone.live_ocr.extract_draft", side_effect=extract):
                document_id = self.send(SPECIMEN_PREGNANCY)
                self.wait_for_draft()
                self.assertEqual(self.status(document_id), ("PROCESSING_FAILED", code))


class MultiPageReviewTest(LiveOcrAdapterTestCase):
    def review(self, document_id: str, field: str, action: str, *, scope: str = "document",
               encounter_index: int | None = None, **kwargs) -> dict:
        return self.service.review_field(document_id, reviewer=REVIEWER, scope=scope, field=field, action=action,
                                         encounter_index=encounter_index, **kwargs)

    def test_two_photos_of_one_grid_register_each_visit_once_after_the_conflict_is_settled(self):
        second = {slot: dict(cells) for slot, cells in VISITS.items()}
        second["M7"]["weight_kg"] = "69.4"
        self.reader.results = [page_scan(grid_page(VISITS)), page_scan(grid_page(second))]
        document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()

        detail = self.service.get_document(document_id)
        self.assertEqual([e["slot"] for e in detail["draft"]["encounters"]], ["T1_V1", "T2_V1", "M7"])
        encounter_items = {(b["slot"], b["field"], b["reason"]) for b in detail["review"]["blocking"]
                           if b["scope"] == "encounter"}
        self.assertEqual(encounter_items, {("M7", "weight_kg", "NEEDS_REVIEW")})
        with self.assertRaises(Exception) as caught:
            self.review(document_id, "weight_kg", "CONFIRM", scope="encounter", encounter_index=2)
        self.assertEqual(caught.exception.code, "NOTHING_TO_CONFIRM", "no value was chosen between the two pages")

        self.review(document_id, "weight_kg", "CORRECT", scope="encounter", encounter_index=2, value="68.4")
        for name in ("registry_file_number", "midwife_patient_code", "facility_name"):
            self.review(document_id, name, "SET_STATUS", field_status="NOT_PROVIDED")
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="NEW")
        visits = self.visit_count()
        registration = self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual([v["slot"] for v in registration["visits"]], ["T1_V1", "T2_V1", "M7"])
        self.assertEqual(self.visit_count(), visits + 3)
        weight = self.service.get_document(document_id)["draft"]["encounters"][2]["fields"]["weight_kg"]
        self.assertEqual((weight["value"], weight["verification"]["state"]), (68.4, "CORRECTED"))
        self.assertEqual([r["value"] for r in weight["source"]["readings"]], [68.4, 69.4])

    def test_cover_and_history_pages_register_the_patient_without_inventing_a_visit(self):
        self.reader.results = [page_scan(cover_page()), page_scan(identification_page())]
        document_id = self.send(self.whatsapp_photo()[0], self.whatsapp_photo()[0])
        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["draft"]["encounters"], [])
        self.assertEqual({b["scope"] for b in detail["review"]["blocking"]}, {"document"})

        for name in ("registry_file_number", "facility_name"):
            self.review(document_id, name, "CONFIRM")
        for name in ("midwife_patient_code", "last_menstrual_period"):
            self.review(document_id, name, "SET_STATUS", field_status="NOT_PROVIDED")
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="NEW")
        visits = self.visit_count()
        registration = self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual((registration["visits"], self.visit_count()), ([], visits))


class ServerTest(LiveOcrAdapterTestCase):
    def test_fixture_stays_the_default(self):
        default = build_service(Path(self.tmp.name) / "default.sqlite3")
        try:
            self.assertIsInstance(default.extractor, FixtureExtractor)
        finally:
            default.store.close()
        local = build_service(Path(self.tmp.name) / "local.sqlite3", ocr_engine=self.engine, ocr_reader=self.reader)
        try:
            self.assertIsInstance(local.extractor, LiveOcrAdapter)
        finally:
            local.store.close()

    def test_one_ocr_process_gets_only_the_ocr_settings(self):
        environ = {"DAYONE_OCR_MODELS": "mobile", "WHATSAPP_ACCESS_TOKEN": "secret-token",
                   "DAYONE_OCR_CPU_THREADS": "2", "DAYONE_OCR_TMP_DIR": str(Path(self.tmp.name) / "ocr")}
        with mock.patch.dict(os.environ, environ, clear=True):
            settings = server.ocr_process.settings_from_env(os.environ)
            reader = server.ocr_reader_for("paddle", settings)
        try:
            self.assertEqual(reader.spec.kwargs, {"choice": "paddle", "environ": {
                "DAYONE_OCR_MODELS": "mobile", "DAYONE_OCR_CPU_THREADS": "2"}})
            self.assertEqual((len(reader._workers), reader.temp_root), (1, Path(self.tmp.name) / "ocr"))
        finally:
            reader.close()

    def test_document_reading_is_bounded_and_reports_progress(self):
        clock = [0.0]

        def slow_read(path, *, force=False, timeout=None):
            self.reader.seen.append(timeout)
            clock[0] += 50
            return self.reader.default

        self.reader.read = slow_read
        self.service.extractor = LiveOcrAdapter(self.service.extraction_media, self.engine, reader=self.reader,
                                                document_timeout=120, monotonic=lambda: clock[0])
        refs = [self.whatsapp_photo()[0] for _ in range(4)]
        document_id = self.send(*refs)
        with self.assertLogs("dayone", "INFO") as logs:
            self.wait_for_draft()
        self.assertEqual(self.reader.seen, [120, 70, 20], "each page gets what is left of the document's time")
        draft = self.service.get_document(document_id)["draft"]
        self.assertEqual([p["review_reason"] for p in draft["pages"]], [None, None, None, "OCR_TIMEOUT"])
        self.assertIn(f"extraction of {document_id}: page 4/4 OCR_TIMEOUT", "\n".join(logs.output))
        self.assertEqual(self.service.extraction_progress(document_id), None, "no progress once done")

    def test_progress_is_visible_while_a_page_is_read(self):
        snapshots = []
        refs = [self.whatsapp_photo()[0] for _ in range(2)]
        document_id = self.send(*refs)
        self.reader.results = [self.reader.default, self.reader.default]
        reads = iter([lambda: snapshots.append(self.service.get_document(document_id)["document"]),
                      lambda: snapshots.append(self.service.system_info()["extracting"])])
        original = self.reader.read

        def read(path, **options):
            next(reads)()
            return original(path, **options)

        self.reader.read = read
        self.service.extractor = LiveOcrAdapter(self.service.extraction_media, self.engine, reader=self.reader)
        self.wait_for_draft()
        self.assertEqual({k: snapshots[0]["extraction_progress"][k] for k in ("pages_done", "pages")},
                         {"pages_done": 0, "pages": 2})
        self.assertEqual([(p["document_id"], p["pages_done"], p["last_page"]) for p in snapshots[1]],
                         [(document_id, 1, "read")])
        self.assertIsNone(self.service.get_document(document_id)["document"]["extraction_progress"])

    def test_ocr_extractors_are_refused_until_installed(self):
        missing = mock.Mock(missing=mock.Mock(return_value=["paddleocr", "model PP-OCRv6_medium_det"]))
        for argv, environ in ((["--extractor", "paddle"], {}), ([], {"DAYONE_EXTRACTOR": "paddle"}),
                              (["--extractor", "tesseract"], {})):
            with self.subTest(argv or environ), mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch("dayone.ocr_engines.create_engine", return_value=missing), \
                    mock.patch("sys.stderr", io.StringIO()) as stderr, self.assertRaises(SystemExit):
                server_main(["--db", str(self.db_path), *argv])
            self.assertIn("missing paddleocr, model PP-OCRv6_medium_det", stderr.getvalue())

    def test_invalid_settings_are_refused(self):
        for environ, message in (({"DAYONE_OCR_MODELS": "large"}, "DAYONE_OCR_MODELS must be one of medium, mobile"),
                                 ({"DAYONE_OCR_TIMEOUT_SECONDS": "1"}, "DAYONE_OCR_TIMEOUT_SECONDS must be between"),
                                 ({"DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS": "x"},
                                  "DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS must be a number"),
                                 ({"DAYONE_OCR_CPU_THREADS": "16"}, "DAYONE_OCR_CPU_THREADS must be between 2 and 4")):
            with self.subTest(environ), mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch("sys.stderr", io.StringIO()) as stderr, self.assertRaises(SystemExit):
                server_main(["--db", str(self.db_path), "--extractor", "paddle"])
            self.assertIn(message, stderr.getvalue())

    def test_ocr_startup_keeps_the_demo_data_and_the_outbound_hold(self):
        whatsapp.hold_outbound(self.service.store)
        visits = self.visit_count()
        self.service.store.close()

        class StoppedServer:
            def __init__(self, *args):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        work = Path(self.tmp.name) / "ocr-work"
        with mock.patch.dict(os.environ, {"DAYONE_OCR_TMP_DIR": str(work)}, clear=True), \
                mock.patch("dayone.ocr_engines.create_engine", return_value=self.engine), \
                mock.patch("dayone.server.ThreadingHTTPServer", StoppedServer), \
                mock.patch("dayone.ocr_process.OcrProcessPool.warm_up") as warm_up, \
                mock.patch.object(DayOneService, "reset_demo", side_effect=AssertionError("demo reset")), \
                mock.patch("sys.stdout", io.StringIO()):
            server_main(["--db", str(self.db_path), "--extractor", "paddle"])
        warm_up.assert_called_once_with()

        self.service = self.make_service()
        self.assertEqual(self.visit_count(), visits)
        self.assertTrue(whatsapp.outbound_hold_active(self.service.store))
        self.assertEqual(list(work.iterdir()), [])
