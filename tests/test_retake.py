from dayone.service import Conflict, Forbidden, NotFound

from tests.test_flow import COVER, REVIEWER, SENDER, T1_GRID, T2_T3_GRID, FlowTestCase

OTHER_FACILITY_SENDER = "whatsapp:+212600000099"
SAME_FACILITY_SENDER = "whatsapp:+212600000002"


class RetakeTestCase(FlowTestCase):
    def sample_document(self) -> tuple[str, str]:
        """Sample booklet (cover + T2/T3 grid) with a draft; returns (document_id, grid page_id)."""
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        return document_id, self.service.get_document(document_id)["pages"][1]["page_id"]

    def replace(self, request_id: str, media_ref: str, *, message_id: str = "wamid.retake-1",
                sender_id: str = SENDER) -> dict:
        return self.service.ingest_photo(sender_id=sender_id, message_id=message_id, media_ref=media_ref,
                                         retake_request_id=request_id)

    def counts(self) -> tuple[int, int, int]:
        with self.service.store.read() as db:
            return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("pages", "messages", "visits"))

    def retake_row(self, request_id: str):
        with self.service.store.read() as db:
            return dict(db.execute("SELECT * FROM retake_requests WHERE request_id = ?", (request_id,)).fetchone())


class RetakeRequestTest(RetakeTestCase):
    def test_repeated_request_is_persisted_once_with_one_message(self):
        document_id, page_id = self.sample_document()
        first = self.service.request_retake(page_id, reviewer=REVIEWER)
        second = self.service.request_retake(page_id, reviewer="autre.agent")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["request_id"], second["request_id"])
        row = self.retake_row(first["request_id"])
        self.assertEqual(
            (row["document_id"], row["page_id"], row["requested_by"], row["status"], row["facility_id"], row["sender_id"]),
            (document_id, page_id, REVIEWER, "PENDING", "FAC-SIDI-SMAIL", SENDER),
        )
        self.assertEqual(row["requested_at"], self.clock.now.isoformat())

        thread = self.service.thread(SENDER)
        asks = [m for m in thread if m["body"] == "Merci de reprendre la photo de la page 2."]
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0]["direction"], asks[0]["retake_request_id"], asks[0]["retake_status"]),
                         ("OUT", first["request_id"], "PENDING"))

        detail = self.service.get_document(document_id)
        self.assertEqual(detail["next_step"], "WAITING_RETAKE")
        self.assertEqual(detail["pages"][1]["pending_retake_id"], first["request_id"])
        self.assertEqual([e["type"] for e in detail["events"]].count("RETAKE_REQUESTED"), 1)
        queued = next(d for d in self.service.list_documents() if d["document_id"] == document_id)
        self.assertEqual(queued["pending_retakes"], 1)

    def test_confirmation_is_blocked_while_retake_is_pending(self):
        document_id, page_id = self.sample_document()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.service.request_retake(page_id, reviewer=REVIEWER)
        before = self.visit_count()
        with self.assertRaises(Conflict) as raised:
            self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "RETAKE_PENDING")
        self.assertEqual(self.visit_count(), before)

    def test_no_retake_on_registered_document(self):
        document_id = self.send(COVER, T1_GRID)
        self.wait_for_draft()
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.service.confirm(document_id, reviewer=REVIEWER)
        page_id = self.service.get_document(document_id)["pages"][0]["page_id"]
        messages = len(self.service.thread(SENDER))
        with self.assertRaises(Conflict) as raised:
            self.service.request_retake(page_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "ALREADY_REGISTERED")
        self.assertEqual(len(self.service.thread(SENDER)), messages)

    def test_cancelled_request_unblocks_and_rejects_late_photo(self):
        document_id, page_id = self.sample_document()
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        cancelled = self.service.cancel_retake(request_id, reviewer=REVIEWER)
        self.assertEqual((cancelled["status"], cancelled["closed_by"]), ("CANCELLED", REVIEWER))
        self.assertEqual(self.service.thread(SENDER)[-1]["body"],
                         "Demande annulée : inutile de reprendre la photo de la page 2.")
        self.assertEqual(self.service.get_document(document_id)["next_step"], "FIELD")

        messages = len(self.service.thread(SENDER))
        self.service.cancel_retake(request_id, reviewer=REVIEWER)
        self.assertEqual(len(self.service.thread(SENDER)), messages, "repeated cancel sends nothing")

        before = self.counts()
        with self.assertRaises(Conflict) as raised:
            self.replace(request_id, T2_T3_GRID)
        self.assertEqual(raised.exception.code, "RETAKE_NOT_PENDING")
        self.assertEqual(self.counts(), before)


class ReplacementTest(RetakeTestCase):
    def test_replacement_preserves_original_and_requeues_extraction(self):
        document_id, page_id = self.sample_document()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        visits_before = self.visit_count()

        result = self.replace(request_id, T2_T3_GRID)
        self.assertEqual((result["position"], result["replaced_page_id"], result["duplicate"]), (2, page_id, False))
        self.assertEqual(self.service.thread(SENDER)[-1]["body"],
                         "Reçu : nouvelle photo de la page 2. Merci, le traitement est en cours.")

        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "PENDING_AI")
        self.assertIsNone(detail["draft"], "stale draft must be discarded")
        self.assertIsNone(detail["review"], "stale patient selection must be discarded")
        self.assertEqual([p["page_id"] for p in detail["pages"]][1], result["page_id"])
        self.assertEqual(len(detail["pages"]), 2)
        self.assertEqual(detail["retakes"][0]["status"], "FULFILLED")
        self.assertEqual(detail["retakes"][0]["replacement_page_id"], result["page_id"])
        replaced = next(e for e in detail["events"] if e["type"] == "PAGE_REPLACED")["detail"]
        self.assertEqual((replaced["discarded_reviews"], replaced["selection_discarded"]), (2, True))
        self.assertIn("RETAKE_REQUESTED", [e["type"] for e in detail["events"]])
        with self.service.store.read() as db:
            original = db.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
        self.assertEqual((original["replaced_by"], original["media_ref"]), (result["page_id"], T2_T3_GRID))

        self.wait_for_draft()
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual(len(detail["review"]["blocking"]), 2, "re-extracted fields need review again")
        self.assertEqual(self.visit_count(), visits_before)

        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        result = self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual([v["outcome"] for v in result["visits"]], ["CREATED"] * 3)
        self.assertTrue(self.service.confirm(document_id, reviewer=REVIEWER)["replayed"])
        self.assertEqual(self.visit_count(), visits_before + 3)

    def test_duplicate_replacement_webhook_is_ignored(self):
        _document_id, page_id = self.sample_document()
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        first = self.replace(request_id, T2_T3_GRID)
        before = self.counts()

        replay = self.replace(request_id, T2_T3_GRID)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["page_id"], first["page_id"])
        self.assertEqual(self.counts(), before)

        with self.assertRaises(Conflict) as raised:
            self.replace(request_id, T2_T3_GRID, message_id="wamid.retake-2")
        self.assertEqual(raised.exception.code, "RETAKE_NOT_PENDING")
        with self.assertRaises(Conflict) as raised:
            self.service.request_retake(page_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "PAGE_REPLACED")
        self.assertEqual(self.counts(), before)

    def test_other_sender_cannot_fulfil_request(self):
        _document_id, page_id = self.sample_document()
        with self.service.store.tx() as db:
            db.execute("INSERT INTO facilities(facility_id, name) VALUES ('FAC-OTHER', 'Autre centre')")
            db.execute("INSERT INTO senders(sender_id, facility_id, label) VALUES (?, 'FAC-OTHER', 'Autre')",
                       (OTHER_FACILITY_SENDER,))
            db.execute("INSERT INTO senders(sender_id, facility_id, label) VALUES (?, 'FAC-SIDI-SMAIL', 'Collègue')",
                       (SAME_FACILITY_SENDER,))
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        before = self.counts()

        with self.assertRaises(Forbidden) as raised:
            self.replace(request_id, T2_T3_GRID, sender_id=OTHER_FACILITY_SENDER)
        self.assertEqual(raised.exception.code, "OTHER_FACILITY")
        with self.assertRaises(Forbidden) as raised:
            self.replace(request_id, T2_T3_GRID, sender_id=SAME_FACILITY_SENDER, message_id="wamid.retake-2")
        self.assertEqual(raised.exception.code, "NOT_REQUEST_RECIPIENT")
        with self.assertRaises(NotFound):
            self.replace("RTK-999999", T2_T3_GRID, message_id="wamid.retake-3")

        self.assertEqual(self.counts(), before)
        self.assertEqual(self.retake_row(request_id)["status"], "PENDING")
        self.assertEqual(self.service.thread(OTHER_FACILITY_SENDER), [])

    def test_extraction_started_before_replacement_is_discarded(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.service.set_ai_available(False)
        self.wait_for_draft()
        page_id = self.service.get_document(document_id)["pages"][1]["page_id"]
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        self.service.set_ai_available(True)

        fixtures, calls = self.service.extractor, []

        class ReplacedMidExtraction:
            name = "fixture"

            def extract(inner, refs):
                calls.append(list(refs))
                if len(calls) == 1:
                    self.replace(request_id, T2_T3_GRID)
                return fixtures.extract(refs)

        self.service.extractor = ReplacedMidExtraction()
        self.assertEqual(self.service.tick(), [], "result for the old page set must not be saved")
        detail = self.service.get_document(document_id)
        self.assertEqual((detail["document"]["status"], detail["draft"]), ("PENDING_AI", None))

        self.assertEqual(self.service.tick(), [document_id])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "NEEDS_REVIEW")

    def test_pages_with_pending_retake_cannot_move_and_history_follows_moves(self):
        document_id, page_id = self.sample_document()
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        with self.assertRaises(Conflict) as raised:
            self.service.move_page(page_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "RETAKE_PENDING")

        replacement = self.replace(request_id, T2_T3_GRID)["page_id"]
        with self.assertRaises(Conflict):
            self.service.move_page(page_id, reviewer=REVIEWER)
        moved = self.service.move_page(replacement, reviewer=REVIEWER)

        split_off = self.service.get_document(moved["to_document_id"])
        self.assertEqual([p["page_id"] for p in split_off["pages"]], [replacement])
        self.assertEqual(split_off["retakes"][0]["request_id"], request_id)
        self.assertEqual(self.service.get_document(document_id)["retakes"], [])
        with self.service.store.read() as db:
            original = db.execute("SELECT document_id, position FROM pages WHERE page_id = ?", (page_id,)).fetchone()
        self.assertEqual(tuple(original), (moved["to_document_id"], 1))


class RegisteredVisitTest(RetakeTestCase):
    def test_retake_never_silently_changes_registered_visits(self):
        document_id = self.send(COVER, T1_GRID)  # re-photo of the booklet already registered for PAT-000001
        self.wait_for_draft()
        grid_page = self.service.get_document(document_id)["pages"][1]["page_id"]
        request_id = self.service.request_retake(grid_page, reviewer=REVIEWER)["request_id"]
        self.replace(request_id, T1_GRID)
        self.wait_for_draft()

        self.service.review_field(document_id, reviewer=REVIEWER, scope="encounter", encounter_index=1,
                                  field="weight_kg", action="CORRECT", value="59")
        detail = self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.assertEqual(detail["next_step"], "EXISTING_VISITS")
        with self.assertRaises(Conflict) as raised:
            self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual(raised.exception.code, "DECISION_REQUIRED")

        before = self.service.patient_timeline("PAT-000001")["visits"]
        result = self.service.confirm(document_id, reviewer=REVIEWER, existing_visit_decisions={"1": "KEEP"})
        self.assertEqual([v["outcome"] for v in result["visits"]], ["UNCHANGED", "KEPT_EXISTING", "UNCHANGED"])
        after = self.service.patient_timeline("PAT-000001")["visits"]
        self.assertEqual([v["fields"] for v in after], [v["fields"] for v in before])
