import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dayone.extraction import FixtureExtractor
from dayone.service import DEMO_SENDER, Conflict, DayOneService, Forbidden, Invalid
from dayone.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
COVER = "data/Paper Registry/1-1.jpg"
T1_GRID = "data/Paper Registry/1-4.jpg"
T2_T3_GRID = "data/Paper Registry/1-5.jpg"
SENDER = DEMO_SENDER["sender_id"]
REVIEWER = "agent.test"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        self.clock = Clock()
        self.service = self.make_service()
        self.service.reset_demo()  # seeds PAT-000001 with the three first-trimester visits
        self.message_number = 0

    def tearDown(self):
        self.service.store.close()
        self.tmp.cleanup()

    def make_service(self) -> DayOneService:
        return DayOneService(Store(self.db_path), FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                             grouping_window_seconds=8, clock=self.clock)

    def send(self, *refs: str) -> str:
        document_id = None
        for ref in refs:
            self.message_number += 1
            result = self.service.ingest_photo(sender_id=SENDER, message_id=f"wamid.test-{self.message_number}",
                                               media_ref=ref)
            document_id = result["document_id"]
            self.clock.advance(2)
        return document_id

    def wait_for_draft(self) -> None:
        self.clock.advance(10)
        self.service.tick()

    def review_sample(self, document_id: str) -> dict:
        self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=0,
                                  field="fundal_height_cm", action="CONFIRM")
        return self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=2,
                                         field="visit_date", action="CORRECT", value="19/12/2025")

    def visit_count(self) -> int:
        with self.service.store.read() as db:
            return db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]


class EndToEndTest(FlowTestCase):
    def test_photo_to_timeline(self):
        document_id = self.send(COVER, T2_T3_GRID)
        thread = self.service.thread(SENDER)
        self.assertEqual(thread[-1]["body"], "Reçu : page 2. Merci, le traitement est en cours.")
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "CAPTURED")

        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual(detail["next_step"], "FIELD")
        self.assertEqual(len(detail["review"]["blocking"]), 2)
        self.assertEqual(detail["review"]["suggested_patient_id"], "PAT-000001")
        self.assertIsNone(detail["review"]["selection"], "auto-link must only suggest")

        detail = self.review_sample(document_id)
        self.assertEqual(detail["document"]["status"], "VALIDATED")
        self.assertEqual(detail["next_step"], "PATIENT")
        corrected = detail["draft"]["encounters"][2]["fields"]["visit_date"]
        self.assertEqual(corrected["value"], "2025-12-19")
        self.assertEqual(corrected["verification"]["state"], "CORRECTED")
        self.assertEqual(corrected["corrections"][0]["previous"]["value"], "2025-12-12")

        with self.assertRaises(Conflict):
            self.service.confirm(document_id, reviewer=REVIEWER)

        detail = self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.assertEqual(detail["document"]["status"], "PATIENT_MATCHED")
        self.assertEqual(detail["next_step"], "CONFIRM")

        result = self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual(result["patient_id"], "PAT-000001")
        self.assertEqual([v["outcome"] for v in result["visits"]], ["CREATED"] * 3)

        timeline = self.service.patient_timeline("PAT-000001")
        self.assertEqual(
            [v["visit_date"] for v in timeline["visits"]],
            ["2025-07-04", "2025-08-01", "2025-09-12", "2025-09-26", "2025-11-14", "2025-12-19"],
        )
        registered = self.service.get_document(document_id)["draft"]
        states = {fv["verification"]["state"] for enc in registered["encounters"] for fv in enc["fields"].values()}
        self.assertNotIn("UNVERIFIED", states)

    def test_confirm_retry_saves_visits_once(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        before = self.visit_count()

        first = self.service.confirm(document_id, reviewer=REVIEWER)
        second = self.service.confirm(document_id, reviewer=REVIEWER)

        self.assertEqual(self.visit_count(), before + 3)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual([v["visit_id"] for v in first["visits"]], [v["visit_id"] for v in second["visits"]])

    def test_illegible_field_needs_explicit_answer(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        detail = self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=2,
                                           field="visit_date", action="CONFIRM")
        self.assertEqual([b["field"] for b in detail["review"]["blocking"]], ["fundal_height_cm"])

    def test_required_visit_date_cannot_be_marked_missing(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        with self.assertRaises(Invalid):
            self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=2,
                                      field="visit_date", action="SET_STATUS", field_status="ILLEGIBLE")

    def test_invalid_correction_is_rejected_without_change(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        with self.assertRaises(Invalid):
            self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=2,
                                      field="visit_date", action="CORRECT", value="32/12/2025")
        fv = self.service.get_document(document_id)["draft"]["encounters"][2]["fields"]["visit_date"]
        self.assertEqual((fv["value"], fv["corrections"]), ("2025-12-12", []))

    def test_stale_revision_is_rejected(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        revision = self.service.get_document(document_id)["document"]["revision"]
        self.review_sample(document_id)
        with self.assertRaises(Conflict):
            self.service.select_patient(document_id, reviewer=REVIEWER, choice="NEW", expected_revision=revision)


class DuplicateTest(FlowTestCase):
    def test_webhook_replay_is_ignored(self):
        first = self.service.ingest_photo(sender_id=SENDER, message_id="wamid.replay", media_ref=COVER)
        messages = len(self.service.thread(SENDER))
        replay = self.service.ingest_photo(sender_id=SENDER, message_id="wamid.replay", media_ref=COVER)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["page_id"], first["page_id"])
        self.assertEqual(len(self.service.thread(SENDER)), messages, "no second acknowledgment")

    def test_rephotographed_booklet_creates_no_new_visit(self):
        document_id = self.send(COVER, T1_GRID)
        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "VALIDATED")
        self.assertEqual(detail["review"]["suggested_patient_id"], "PAT-000001")

        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        before = self.visit_count()
        result = self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual([v["outcome"] for v in result["visits"]], ["UNCHANGED"] * 3)
        self.assertEqual(self.visit_count(), before)

    def test_changed_value_on_existing_visit_requires_decision(self):
        document_id = self.send(COVER, T1_GRID)
        self.wait_for_draft()
        self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=1,
                                  field="weight_kg", action="CORRECT", value="59")
        detail = self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.assertEqual(detail["next_step"], "EXISTING_VISITS")
        with self.assertRaises(Conflict) as raised:
            self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "DECISION_REQUIRED")

        result = self.service.confirm(document_id, reviewer=REVIEWER, existing_visit_decisions={"1": "UPDATE"})
        self.assertEqual([v["outcome"] for v in result["visits"]], ["UNCHANGED", "UPDATED", "UNCHANGED"])
        visit = self.service.patient_timeline("PAT-000001")["visits"][1]
        self.assertEqual((visit["fields"]["weight_kg"]["value"], visit["revisions"]), (59, 1))

    def test_new_patient_with_registered_key_is_rejected(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="NEW")
        with self.assertRaises(Conflict) as raised:
            self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "KEY_ALREADY_REGISTERED")
        self.assertEqual(len(self.service.list_patients()), 1)

    def test_unsure_parks_document(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        self.review_sample(document_id)
        detail = self.service.select_patient(document_id, reviewer=REVIEWER, choice="UNSURE")
        self.assertEqual(detail["document"]["status"], "DUPLICATE_SUSPECTED")
        with self.assertRaises(Conflict):
            self.service.confirm(document_id, reviewer=REVIEWER)

    def test_unknown_sender_is_refused(self):
        with self.assertRaises(Forbidden):
            self.service.ingest_photo(sender_id="whatsapp:+0000", message_id="wamid.x", media_ref=COVER)


class QueueAndGroupingTest(FlowTestCase):
    def test_queue_waits_while_ai_unavailable_and_survives_restart(self):
        self.service.set_ai_available(False)
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "PENDING_AI")

        self.service.store.close()
        self.service = self.make_service()
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "PENDING_AI")

        self.service.set_ai_available(True)
        self.assertEqual(self.service.tick(), [document_id])
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "NEEDS_REVIEW")

    def test_unexpected_extractor_error_keeps_the_document_queued_and_logs_its_type_only(self):
        fixtures = self.service.extractor

        class Broken:
            name = "fixture"

            def extract(self, refs):
                raise RuntimeError("Nom/Prénom : Inventée Exemple 0600000000")

        self.service.extractor = Broken()
        document_id = self.send(COVER, T2_T3_GRID)
        self.clock.advance(10)
        with self.assertLogs("dayone", "ERROR") as logs:
            self.assertEqual(self.service.tick(), [])
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("Inventée", text)
        self.assertTrue(all(record.exc_info is None for record in logs.records), "no traceback is logged")
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "PENDING_AI")
        self.assertEqual(self.service.extraction_retry(document_id)["reason"], "EXTRACTION_ERROR")

        self.service.extractor = fixtures
        self.assertEqual(self.service.tick(), [document_id])

    def test_pages_after_window_start_a_new_document(self):
        first = self.send(COVER)
        self.clock.advance(30)
        second = self.send(T2_T3_GRID)
        self.assertNotEqual(first, second)

    def test_split_and_regroup_pages(self):
        mixed = self.send(COVER, T2_T3_GRID, T1_GRID)
        self.wait_for_draft()
        self.assertEqual(self.service.get_document(mixed)["document"]["status"], "PROCESSING_FAILED")

        t1_page = self.service.get_document(mixed)["pages"][2]["page_id"]
        moved = self.service.move_page(t1_page, reviewer=REVIEWER)
        split_off = moved["to_document_id"]
        self.service.tick()

        remaining = self.service.get_document(mixed)
        self.assertEqual(remaining["document"]["grouping_status"], "CONFIRMED")
        self.assertEqual(remaining["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual(self.service.get_document(split_off)["document"]["status"], "PROCESSING_FAILED")

        self.service.move_page(t1_page, reviewer=REVIEWER, target_document_id=mixed)
        self.assertEqual(self.service.get_document(mixed)["document"]["status"], "PENDING_AI")
        with self.assertRaises(Exception):
            self.service.get_document(split_off)

    def test_manual_entry_after_failed_extraction(self):
        document_id = self.send(T1_GRID)
        self.wait_for_draft()
        detail = self.service.start_manual_entry(document_id, reviewer=REVIEWER)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual(len(detail["review"]["blocking"]), 12)


if __name__ == "__main__":
    unittest.main()
