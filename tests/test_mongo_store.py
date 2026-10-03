"""Adapter tests use an explicit in-memory Mongo test double, not a live Atlas."""
import copy
import json
import os
import unittest
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

@unittest.skipUnless(os.environ.get("DAYONE_TEST_MONGODB_URI"), "No dedicated Atlas test URI configured")
class LiveAtlasTest(unittest.TestCase):
    def test_snapshot_transaction_roundtrip(self):
        import uuid
        from pymongo import MongoClient
        uri = os.environ["DAYONE_TEST_MONGODB_URI"]
        database = "dayone_test_" + uuid.uuid4().hex
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        store = None
        try:
            store = MongoStore(uri, database, cipher=Cipher(Fernet.generate_key()), client=client)
            with store.tx() as db:
                db.execute("INSERT INTO facilities VALUES ('TEST','synthetic')")
            with store.read() as db:
                self.assertEqual(db.execute("SELECT name FROM facilities").fetchone()[0], "synthetic")
        finally:
            client.drop_database(database)
            client.close()
