"""Adapter tests use an explicit in-memory Mongo test double, not a live Atlas."""
import base64
import copy
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from cryptography.fernet import Fernet
from dayone.mongo_store import MongoStore
from dayone.media import MongoMediaStore
from dayone.security import Cipher
from dayone.service import DayOneService, Conflict
from dayone.extraction import FixtureExtractor
from tests.test_flow import REPO_ROOT, COVER, T1_GRID

class Collection:
    def __init__(self):
        self.items = {}
        self.conflict = False
    def with_options(self, **kwargs):
        return self
    def create_index(self, *args, **kwargs):
        pass
    def find(self, query, **kwargs):
        return list(copy.deepcopy(self.items).values())
    def find_one(self, query, **kwargs):
        return copy.deepcopy(self.items.get(query["_id"]))
    def update_one(self, query, update, upsert=False, **kwargs):
        item = self.items.get(query["_id"])
        if self.conflict or (item and "version" in query and query["version"] != item["version"]):
            return SimpleNamespace(modified_count=0)
        if item is None:
            item = {"_id": query["_id"], **update.get("$setOnInsert", {})}
            self.items[query["_id"]] = item
        for field, amount in update.get("$inc", {}).items():
            item[field] += amount
        return SimpleNamespace(modified_count=1)
    def replace_one(self, query, item, **kwargs):
        self.items[query["_id"]] = copy.deepcopy(item)
    def delete_one(self, query, **kwargs):
        self.items.pop(query["_id"], None)

class Database:
    def __init__(self):
        self.collections = {}
    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())
    @property
    def control(self):
        return self["control"]

class Transaction:
    def __init__(self, database):
        self.database = database
    def __enter__(self):
        self.before = copy.deepcopy(self.database.collections)
    def __exit__(self, kind, exc, trace):
        if kind:
            self.database.collections = self.before

class Session:
    def __init__(self, database):
        self.database = database
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def start_transaction(self, **kwargs):
        return Transaction(self.database)

class Client:
    def __init__(self):
        self.database = Database()
        self.admin = self
    def __getitem__(self, name):
        return self.database
    def command(self, command):
        return {"ok": 1}
    def start_session(self):
        return Session(self.database)
    def close(self):
        pass

class MongoAdapterTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.store = MongoStore("mongodb+srv://example.invalid", cipher=Cipher(Fernet.generate_key()), client=self.client)
        self.service = DayOneService(self.store, FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT)
        self.service.enroll_sender(facility_id="FAC", facility_name="Facility", sender_id="private-sender", label="sender")

    def test_registration_restart_dedup_and_encrypted_rows(self):
        for i, ref in enumerate((COVER, T1_GRID)):
            response = self.service.ingest_photo(sender_id="private-sender", message_id=f"message-{i}", media_ref=ref, group_id="batch")
        doc = response["document_id"]
        self.service.close_capture(doc)
        self.service.process_document(doc)
        self.service.select_patient(doc, reviewer="agent", choice="NEW")
        saved = self.service.confirm(doc, reviewer="agent")
        # New adapter simulates a fresh process sharing the central authority.
        other = MongoStore("mongodb+srv://example.invalid", cipher=self.store.cipher, client=self.client)
        service = DayOneService(other, self.service.extractor, REPO_ROOT)
        self.assertTrue(service.confirm(doc, reviewer="agent")["replayed"])
        self.assertEqual(len(service.patient_timeline(saved["patient_id"])["visits"]), 3)
        self.assertTrue(service.ingest_photo(sender_id="private-sender", message_id="message-1", media_ref=T1_GRID)["duplicate"])
        for table in ("patients", "documents", "senders"):
            for item in self.client.database[table].items.values():
                self.assertIsInstance(item["payload"], bytes)
                self.assertNotIn(b"private-sender", item["payload"])
                self.assertNotIn("private-sender", item["_id"])
        patient = next(iter(self.client.database["patients"].items.values()))
        self.assertEqual(len(patient["registry_file_number"]), 64)

    def test_rollback_and_global_conflict(self):
        with self.assertRaises(RuntimeError):
            with self.store.tx() as db:
                db.execute("INSERT INTO facilities VALUES ('BAD','bad')")
                raise RuntimeError("fail before commit")
        with self.store.read() as db:
            self.assertIsNone(db.execute("SELECT * FROM facilities WHERE facility_id='BAD'").fetchone())
        self.client.database.control.conflict = True
        with self.assertRaises(Conflict):
            with self.store.tx() as db:
                db.execute("INSERT INTO facilities VALUES ('BAD','bad')")

    def test_composite_group_keys_and_encrypted_media(self):
        self.service.enroll_sender(facility_id="FAC", facility_name="Facility", sender_id="sender-2", label="sender")
        first = self.service.ingest_photo(sender_id="private-sender", message_id="first", media_ref=COVER, group_id="shared")
        second = self.service.ingest_photo(sender_id="sender-2", message_id="second", media_ref=COVER, group_id="shared")
        self.assertNotEqual(first["document_id"], second["document_id"])
        self.assertEqual(len(self.client.database["capture_groups"].items), 2)
        media = MongoMediaStore(self.store.db, self.store.cipher)
        raw = (REPO_ROOT / COVER).read_bytes()
        ref = media.put(raw, ".jpg")
        self.assertEqual(media.put(raw, ".jpg"), ref)
        self.assertEqual(media.get(ref), raw)
        self.assertNotIn(raw[:30], self.client.database["media_blobs"].items[ref]["payload"])

    def test_demo_reset_is_conflict(self):
        from dayone.server import Api
        status, body = Api(self.service).dispatch("POST", "/api/demo/reset", {}, {}, "agent.test")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RESET_DISABLED")

    def test_hydration_does_not_replay_outbound_triggers(self):
        from dayone.store import SCHEMA_SQL
        schema_with_trigger = SCHEMA_SQL + """
        CREATE TRIGGER queue_receipt AFTER INSERT ON messages
        WHEN NEW.direction = 'OUT'
        BEGIN
            INSERT INTO settings(key, value) VALUES ('receipt:' || NEW.id, 'PENDING');
        END;
        """
        with patch("dayone.mongo_store.SCHEMA_SQL", schema_with_trigger):
            with self.store.tx() as db:
                db.execute("INSERT INTO messages(sender_id,direction,created_at) VALUES ('private-sender','OUT','now')")
                db.execute("UPDATE settings SET value='SENT' WHERE key='receipt:1'")
            # Restoring the message must neither duplicate nor reset its job.
            with self.store.read() as db:
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='receipt:1'").fetchone()[0], "SENT")
            # Triggers are enabled again for new domain writes after hydration.
            with self.store.tx() as db:
                db.execute("INSERT INTO messages(sender_id,direction,created_at) VALUES ('private-sender','OUT','later')")
            with self.store.read() as db:
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='receipt:2'").fetchone()[0], "PENDING")

    def test_additive_schema_columns_preserve_encrypted_records(self):
        from dayone.store import SCHEMA_SQL
        migrated_schema = SCHEMA_SQL
        # Simulate payloads written before the transport added sender channels.
        for item in self.client.database["senders"].items.values():
            row = self.store.cipher.open(item["payload"])
            row.pop("channel")
            item["payload"] = self.store.cipher.seal(row)
        encrypted_before = copy.deepcopy(self.client.database["senders"].items)
        with patch("dayone.mongo_store.SCHEMA_SQL", migrated_schema):
            with self.store.read() as db:
                row = db.execute("SELECT * FROM senders WHERE sender_id='private-sender'").fetchone()
                self.assertEqual(row["channel"], "SIMULATOR")
                self.assertEqual(row["facility_id"], "FAC")
            self.assertEqual(self.client.database["senders"].items, encrypted_before)
            with self.store.tx():
                pass
            with self.store.read() as db:
                self.assertEqual(db.execute("SELECT channel FROM senders").fetchone()[0], "SIMULATOR")

    def test_selected_field_update_and_stale_visit_on_mongo(self):
        media = MongoMediaStore(self.store.db, self.store.cipher)
        self.service.media_store = media
        cover = (REPO_ROOT / COVER).read_bytes()
        grid = (REPO_ROOT / T1_GRID).read_bytes()
        first = self.service.ingest_upload(sender_id="private-sender", message_id="mongo-msg-1",
                                           image_base64=base64.b64encode(cover).decode(), group_id="mongo-grp-1")
        second = self.service.ingest_upload(sender_id="private-sender", message_id="mongo-msg-2",
                                            image_base64=base64.b64encode(grid).decode(), group_id="mongo-grp-1")
        self.assertEqual(first["document_id"], second["document_id"])
        self.service.close_capture(first["document_id"])
        job = self.service.claim_job(first["document_id"])
        draft = _rewrite_pages(self.service.extractor.extract([COVER, T1_GRID]), job["page_refs"])
        self.service.complete_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], draft=draft)
        self.service.select_patient(first["document_id"], reviewer="agent.test", choice="NEW")
        saved = self.service.confirm(first["document_id"], reviewer="agent.test")
        self.assertEqual(len(self.service.patient_timeline(saved["patient_id"])["visits"]), 3)

        # Re-digitization with different weight
        redo = self.service.ingest_upload(sender_id="private-sender", message_id="mongo-msg-3",
                                          image_base64=base64.b64encode(cover).decode(), group_id="mongo-grp-2")
        self.service.ingest_upload(sender_id="private-sender", message_id="mongo-msg-4",
                                   image_base64=base64.b64encode(grid).decode(), group_id="mongo-grp-2")
        self.service.close_capture(redo["document_id"])
        job2 = self.service.claim_job(redo["document_id"])
        draft2 = _rewrite_pages(self.service.extractor.extract([COVER, T1_GRID]), job2["page_refs"])
        draft2["encounters"][0]["fields"]["weight_kg"]["value"] = 85
        draft2["encounters"][0]["fields"]["weight_kg"]["field_status"] = "KNOWN"
        draft2["encounters"][0]["fields"]["weight_kg"]["confidence"] = 0.95
        self.service.complete_job(job2["job_id"], token=job2["token"], expected_revision=job2["expected_revision"], draft=draft2)

        # Both keys match -> candidate is suggested
        self.assertEqual(self.service.get_document(redo["document_id"])["review"]["suggested_patient_id"], saved["patient_id"])
        self.service.select_patient(redo["document_id"], reviewer="agent.test", choice="EXISTING", patient_id=saved["patient_id"])

        match = self.service.get_document(redo["document_id"])["review"]["encounter_matches"][0]
        self.assertEqual(match["outcome"], "EXISTS_DIFFERENT")
        self.assertEqual(len(match["diffs"]), 1)
        self.assertEqual(match["diffs"][0]["field"], "weight_kg")

        # Stale update is rejected
        with self.assertRaises(Conflict) as caught:
            self.service.confirm(redo["document_id"], reviewer="agent.test", existing_visit_decisions={
                "0": {"action": "UPDATE", "fields": ["weight_kg"], "expected_version": "stale-version"}})
        self.assertEqual(caught.exception.code, "STALE_VISIT")

        # Correct update with versioned selected field
        self.service.confirm(redo["document_id"], reviewer="agent.test", existing_visit_decisions={
            "0": {"action": "UPDATE", "fields": ["weight_kg"], "expected_version": match["existing_version"]}})

        visit = self.service.patient_timeline(saved["patient_id"])["visits"][0]
        self.assertEqual(visit["fields"]["weight_kg"]["value"], 85)
        self.assertEqual(visit["history"][-1]["selected_fields"], ["weight_kg"])

    def test_offline_queue_flush_to_mongo_service(self):
        import tempfile
        from pathlib import Path
        from dayone.offline import OfflineQueue
        media = MongoMediaStore(self.store.db, self.store.cipher)
        self.service.media_store = media
        with tempfile.TemporaryDirectory() as tmp_dir:
            queue_db = Path(tmp_dir) / "edge_outbox.sqlite3"
            queue = OfflineQueue(queue_db, self.store.cipher)
            try:
                cover = (REPO_ROOT / COVER).read_bytes()
                grid = (REPO_ROOT / T1_GRID).read_bytes()
                queue.capture("private-sender", "edge-msg-1", cover, group_id="edge-grp")
                queue.capture("private-sender", "edge-msg-2", grid, group_id="edge-grp")
                delivered = queue.flush(lambda body: self.service.ingest_upload(**body), self.service.close_capture)
                self.assertEqual(delivered, 2)
            finally:
                queue.close()

            docs = self.service.list_documents()
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["status"], "PENDING_AI")
            self.assertEqual(docs[0]["page_count"], 2)


def _rewrite_pages(draft, page_refs):
    draft = copy.deepcopy(draft)
    mapping = {old["page_ref"]: new for old, new in zip(draft["pages"], page_refs)}
    for page in draft["pages"]:
        page["page_ref"] = mapping[page["page_ref"]]
    for item in draft.get("pii_detected", []):
        if item.get("page_ref") in mapping:
            item["page_ref"] = mapping[item["page_ref"]]
    for field in draft["document_fields"].values():
        ref = (field.get("source") or {}).get("page_ref")
        if ref in mapping:
            field["source"]["page_ref"] = mapping[ref]
    for encounter in draft["encounters"]:
        for field in encounter["fields"].values():
            ref = (field.get("source") or {}).get("page_ref")
            if ref in mapping:
                field["source"]["page_ref"] = mapping[ref]
    return draft


@unittest.skipUnless(os.environ.get("DAYONE_TEST_MONGODB_URI"), "No dedicated Atlas test URI configured")
class LiveAtlasTest(unittest.TestCase):
    def test_encrypted_pipeline_on_dedicated_database(self):
        import uuid
        from pymongo import MongoClient
        uri = os.environ["DAYONE_TEST_MONGODB_URI"].strip()
        database = "dayone_test_" + uuid.uuid4().hex
        client = MongoClient(uri, serverSelectionTimeoutMS=8000, tls=True)
        cipher = Cipher(Fernet.generate_key())
        try:
            store = MongoStore(uri, database, cipher=cipher, client=client)
            media = MongoMediaStore(store.db, cipher)
            service = DayOneService(store, FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT, media_store=media)
            service.enroll_sender(facility_id="FAC-TEST", facility_name="Test facility",
                                  sender_id="whatsapp:+212600000009", label="test-sender")
            cover = (REPO_ROOT / COVER).read_bytes()
            grid = (REPO_ROOT / T1_GRID).read_bytes()
            first = service.ingest_upload(sender_id="whatsapp:+212600000009", message_id="wamid.live-1",
                                          image_base64=base64.b64encode(cover).decode(), group_id="live-group")
            second = service.ingest_upload(sender_id="whatsapp:+212600000009", message_id="wamid.live-2",
                                           image_base64=base64.b64encode(grid).decode(), group_id="live-group")
            self.assertEqual(first["document_id"], second["document_id"])
            self.assertTrue(service.ingest_upload(sender_id="whatsapp:+212600000009", message_id="wamid.live-2",
                                                  image_base64=base64.b64encode(grid).decode(),
                                                  group_id="live-group")["duplicate"])
            service.close_capture(first["document_id"])
            job = service.claim_job(first["document_id"])
            draft = _rewrite_pages(service.extractor.extract([COVER, T1_GRID]), job["page_refs"])
            service.complete_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], draft=draft)
            self.assertIsNone(service.get_document(first["document_id"])["review"]["suggested_patient_id"])
            service.select_patient(first["document_id"], reviewer="agent.test", choice="NEW")
            saved = service.confirm(first["document_id"], reviewer="agent.test")
            self.assertTrue(service.confirm(first["document_id"], reviewer="agent.test")["replayed"])
            self.assertEqual(len(service.patient_timeline(saved["patient_id"])["visits"]), 3)
            raw_ref = service.get_document(first["document_id"])["pages"][0]["media_ref"]
            self.assertEqual(media.get(raw_ref), cover)
            blob = store.db["media_blobs"].find_one({"_id": raw_ref})
            self.assertNotIn(cover[:24], bytes(blob["payload"]))
            patient = cipher.open(bytes(next(store.db["patients"].find())["payload"]))
            self.assertEqual(patient["registry_file_number"], "164125")
            document = cipher.open(bytes(next(store.db["documents"].find({"_id": first["document_id"]}))["payload"]))
            self.assertEqual(document["sender_id"], "whatsapp:+212600000009")
            visit = cipher.open(bytes(next(store.db["visits"].find())["payload"]))
            self.assertEqual(visit["patient_id"], saved["patient_id"])
            self.assertTrue(any(cipher.open(bytes(item["payload"]))["type"] == "REGISTERED"
                                for item in store.db["events"].find()))
            for table, needle in (("patients", b"164125"), ("documents", b"whatsapp:+212600000009"),
                                  ("visits", saved["patient_id"].encode())):
                payload = bytes(next(store.db[table].find())["payload"])
                self.assertNotIn(needle, payload)
            redo = service.ingest_upload(sender_id="whatsapp:+212600000009", message_id="wamid.live-3",
                                         image_base64=base64.b64encode(cover).decode(), group_id="live-group-2")
            service.ingest_upload(sender_id="whatsapp:+212600000009", message_id="wamid.live-4",
                                  image_base64=base64.b64encode(grid).decode(), group_id="live-group-2")
            service.close_capture(redo["document_id"])
            job = service.claim_job(redo["document_id"])
            draft = _rewrite_pages(service.extractor.extract([COVER, T1_GRID]), job["page_refs"])
            draft["encounters"][0]["fields"]["weight_kg"]["value"] = 88
            service.complete_job(job["job_id"], token=job["token"], expected_revision=job["expected_revision"], draft=draft)
            self.assertEqual(service.get_document(redo["document_id"])["review"]["suggested_patient_id"], saved["patient_id"])
            service.select_patient(redo["document_id"], reviewer="agent.test", choice="EXISTING",
                                   patient_id=saved["patient_id"])
            match = service.get_document(redo["document_id"])["review"]["encounter_matches"][0]
            with self.assertRaises(Conflict) as caught:
                service.confirm(redo["document_id"], reviewer="agent.test", existing_visit_decisions={
                    "0": {"action": "UPDATE", "fields": ["weight_kg"], "expected_version": "stale-version"}})
            self.assertEqual(caught.exception.code, "STALE_VISIT")
            service.confirm(redo["document_id"], reviewer="agent.test", existing_visit_decisions={
                "0": {"action": "UPDATE", "fields": ["weight_kg"], "expected_version": match["existing_version"]}})
            visit = service.patient_timeline(saved["patient_id"])["visits"][0]
            self.assertEqual(visit["fields"]["weight_kg"]["value"], 88)
            self.assertEqual(visit["history"][-1]["selected_fields"], ["weight_kg"])
        finally:
            client.drop_database(database)
            client.close()
