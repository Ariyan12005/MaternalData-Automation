import os
import tempfile
import unittest
from pathlib import Path
from cryptography.fernet import Fernet

from dayone import export_columns
from dayone.offline import OfflineQueue
from dayone.security import Cipher
from dayone.service import Conflict, Invalid
from tests.test_flow import FlowTestCase, COVER, T1_GRID, REVIEWER


class FinalBackendTest(FlowTestCase):
    def test_offline_queue_explicit_states_and_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            cipher = Cipher(Fernet.generate_key())
            path = Path(directory) / "offline.sqlite3"
            queue = OfflineQueue(path, cipher)

            key1 = queue.capture("sender-1", "msg-1", b"img-data-1", group_id="grp-1")
            status1 = queue.status("msg-1")
            self.assertIsNotNone(status1)
            self.assertEqual(status1["state"], "PENDING")
            self.assertEqual(status1["attempts"], 0)
            self.assertIsNone(status1["last_error"])

            # Failure during flush marks state FAILED with attempts and error detail
            with self.assertRaises(ConnectionError):
                queue.flush(lambda body: (_ for _ in ()).throw(ConnectionError("Simulated network outage")))

            status_fail = queue.status("msg-1")
            self.assertEqual(status_fail["state"], "FAILED")
            self.assertEqual(status_fail["attempts"], 1)
            self.assertIn("ConnectionError", status_fail["last_error"])
            self.assertEqual(len(queue.list_items("FAILED")), 1)
            self.assertEqual(len(queue.list_items("PENDING")), 0)

            # Reconnection: retry succeeds and updates state to SYNCED
            delivered = queue.flush(lambda body: {"page_id": "PAGE-10", "document_id": "DOC-10"})
            self.assertEqual(delivered, 1)

            status_ok = queue.status("msg-1")
            self.assertEqual(status_ok["state"], "SYNCED")
            self.assertEqual(status_ok["page_id"], "PAGE-10")
            self.assertEqual(status_ok["document_id"], "DOC-10")
            self.assertIsNone(status_ok["last_error"])
            self.assertEqual(len(queue.list_items("SYNCED")), 1)
            queue.close()

    def test_document_sync_lifecycle_and_idempotency(self):
        doc_id = self.send(COVER, T1_GRID)
        self.wait_for_draft()
        doc = self.service.get_document(doc_id)

        # Cannot sync an unready or non-registered document
        with self.assertRaises(Conflict) as ctx:
            self.service.sync_document(doc_id, reviewer=REVIEWER)
        self.assertEqual(ctx.exception.code, "NOT_REGISTERED")

        # Resolve fields and confirm
        for f in doc["review"]["blocking"]:
            self.service.review_field(
                doc_id, reviewer=REVIEWER, scope=f["scope"], field=f["field"],
                encounter_index=f.get("encounter_index"), section=f.get("section"),
                item_index=f.get("item_index"), action="CONFIRM" if f.get("value") else "SET_STATUS",
                field_status="NOT_PROVIDED" if not f.get("value") else None,
                expected_revision=self.service.get_document(doc_id)["document"]["revision"]
            )
        self.service.select_patient(doc_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001",
                                   expected_revision=self.service.get_document(doc_id)["document"]["revision"])
        self.service.confirm(doc_id, reviewer=REVIEWER,
                             expected_revision=self.service.get_document(doc_id)["document"]["revision"])
        self.assertEqual(self.service.get_document(doc_id)["document"]["status"], "REGISTERED")

        # Synchronize with simulated central sink
        synced_payloads = []
        result = self.service.sync_document(doc_id, reviewer=REVIEWER, central_sink=lambda p: synced_payloads.append(p))
        self.assertEqual(result["status"], "SYNCED")
        self.assertFalse(result["replayed"])
        self.assertEqual(len(synced_payloads), 1)
        self.assertEqual(self.service.get_document(doc_id)["document"]["status"], "SYNCED")

        # Idempotent replay on SYNCED
        replay = self.service.sync_document(doc_id, reviewer=REVIEWER)
        self.assertEqual(replay["status"], "SYNCED")
        self.assertTrue(replay["replayed"])

    def test_conversational_review_workflow(self):
        # Use T2_T3_GRID which has 2 uncertain fields in the fixture
        from tests.test_flow import T2_T3_GRID
        doc_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()

        # Step 1: Query prompt when uncertain fields exist
        prompt = self.service.conversational_prompt(doc_id)
        self.assertEqual(prompt["step"], "FIELD")
        self.assertIn("actions", prompt)
        self.assertIn("CONFIRM", prompt["actions"])

        # Answer blocking fields conversationally
        while prompt["step"] == "FIELD":
            field_name = prompt["field"]["field"]
            if field_name == "visit_date":
                prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="CORRECT", value="19/12/2025")
            elif field_name == "fundal_height_cm":
                prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="CORRECT", value=25)
            elif prompt["field"].get("value") is not None:
                prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="CONFIRM")
            else:
                prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="NOT_PROVIDED")

        # Step 2: Query prompt advances to PATIENT
        self.assertEqual(prompt["step"], "PATIENT")
        self.assertIn("CHOOSE", prompt["actions"])
        prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="CHOOSE", patient_id="PAT-000001")

        # Step 3: Query prompt advances to CONFIRM
        self.assertEqual(prompt["step"], "CONFIRM")
        self.assertIn("CONFIRM", prompt["actions"])
        prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="CONFIRM")

        # Step 4: Step is DONE
        self.assertEqual(prompt["step"], "DONE")
        self.assertIn("SYNC", prompt["actions"])

        # Step 5: Conversational SYNC
        prompt = self.service.conversational_reply(doc_id, reviewer="midwife", action="SYNC")
        self.assertEqual(prompt["step"], "DONE")
        self.assertEqual(self.service.get_document(doc_id)["document"]["status"], "SYNCED")

    def test_export_row_generation_and_csv_formatting(self):
        # Register a document to have patient & visits
        doc_id = self.send(COVER, T1_GRID)
        self.wait_for_draft()
        doc = self.service.get_document(doc_id)
        for f in doc["review"]["blocking"]:
            self.service.review_field(
                doc_id, reviewer=REVIEWER, scope=f["scope"], field=f["field"],
                encounter_index=f.get("encounter_index"), section=f.get("section"),
                item_index=f.get("item_index"), action="CONFIRM" if f.get("value") else "SET_STATUS",
                field_status="NOT_PROVIDED" if not f.get("value") else None,
                expected_revision=self.service.get_document(doc_id)["document"]["revision"]
            )
        self.service.select_patient(doc_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001",
                                   expected_revision=self.service.get_document(doc_id)["document"]["revision"])
        reg = self.service.confirm(doc_id, reviewer=REVIEWER,
                                   expected_revision=self.service.get_document(doc_id)["document"]["revision"])
        patient_id = reg["patient_id"]

        row = self.service.export_patient(patient_id)
        # Exact 31 columns in header order
        headers = [c.header for c in export_columns.COLUMNS]
        self.assertEqual(len(headers), 31)
        self.assertEqual(list(row.keys()), headers)

        # Unverified / unavailable fields remain None (not fabricated)
        self.assertIsNone(row["education level (0=none/primary,1=secondary,2=higher)"])
        self.assertIsNone(row["hypertension history"])
        self.assertIsNone(row["diabetes mellitus"])
        self.assertIsNone(row["first fasting glucose (mg/dl)"])
        self.assertIsNone(row["preterm birth"])
        self.assertIsNone(row["referral to higher care"])

        # Mean systolic and diastolic BP computed deterministically if available
        all_rows = self.service.export_all_patients()
        self.assertGreaterEqual(len(all_rows), 1)

        csv_text = self.service.export_csv()
        lines = csv_text.strip().split("\n")
        self.assertGreaterEqual(len(lines), 2)
        # Header line must exactly match the 31 headers
        self.assertEqual(lines[0], ",".join(f'"{h}"' if "," in h else h for h in headers))

    def test_api_endpoints_sync_conversational_export(self):
        from dayone.server import Api
        api = Api(self.service)

        doc_id = self.send(COVER, T1_GRID)
        self.wait_for_draft()

        # GET conversational prompt
        status, body = api.dispatch("GET", f"/api/documents/{doc_id}/conversational", {}, {}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["step"], "PATIENT")

        # POST conversational reply
        status, body = api.dispatch("POST", f"/api/documents/{doc_id}/conversational", {},
                                    {"action": "CHOOSE", "patient_id": "PAT-000001"}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["step"], "CONFIRM")

        # POST conversational confirm
        status, body = api.dispatch("POST", f"/api/documents/{doc_id}/conversational", {},
                                    {"action": "CONFIRM"}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["step"], "DONE")

        # POST sync
        status, body = api.dispatch("POST", f"/api/documents/{doc_id}/sync", {}, {}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "SYNCED")

        # GET patient export
        status, body = api.dispatch("GET", "/api/patients/PAT-000001/export", {}, {}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 31)

        # GET all export
        status, body = api.dispatch("GET", "/api/export", {}, {}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertIn("rows", body)
        self.assertGreaterEqual(len(body["rows"]), 1)

        # GET export csv
        status, body = api.dispatch("GET", "/api/export/csv", {}, {}, REVIEWER)
        self.assertEqual(status, 200)
        self.assertIn("csv", body)
        self.assertIn("age (years)", body["csv"])


if __name__ == "__main__":
    unittest.main()
