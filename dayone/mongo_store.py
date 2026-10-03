"""Atlas adapter preserving existing SQL contracts. SQL is reconstructed in RAM.
Encrypted rows are durable in Mongo collections. Snapshot transactions and global
version CAS serialize writes across processes. This MVP adapter reads all rows per
request; native Mongo queries will be needed for larger deployments.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from .security import Cipher
from .store import SCHEMA_SQL, TABLES

class MongoStore:
    def __init__(self, uri, database="dayone", *, cipher=None, client=None):
        uri = (uri or "").strip()
        database = (database or "dayone").strip() or "dayone"
        if not uri.startswith("mongodb+srv://"):
            raise ValueError("Configure DAYONE_MONGODB_URI with an Atlas mongodb+srv URI")
        if client is None:
            from pymongo import MongoClient
            client = MongoClient(uri, serverSelectionTimeoutMS=5000, tls=True)
        self.client = client
        self.db = self.client[database]
        self.cipher = cipher or Cipher()
        self._lock = threading.RLock()
        self.client.admin.command("ping")
        self.db.control.update_one({"_id": "generation"}, {"$setOnInsert": {"version": 0}}, upsert=True)
        for table, names in {"pages": ("source_message_id",), "patients": ("registry_file_number", "midwife_patient_code"), "visits": ("identity",)}.items():
            for name in names:
                self.db[table].create_index(name, unique=True, sparse=True)

    def _load(self, session):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        before = {}
        try:
            for table in reversed(TABLES):
                rows = {}
                for item in self.db[table].find({}, session=session):
                    row = self.cipher.open(item["payload"])
                    columns = list(row)
                    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row.values()))
                    rows[item["_id"]] = row
                before[table] = rows
            conn.execute("PRAGMA foreign_keys=ON")
            return conn, before
        except BaseException:
            conn.close()
            raise

    def _persist(self, conn, before, session):
        for table in TABLES:
            pk = next(row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[5])
            # capture_groups has a composite PK.
            after = {self._row_key(table, dict(row), pk): dict(row) for row in conn.execute(f"SELECT * FROM {table}")}
            for key in before[table].keys() - after.keys():
                self.db[table].delete_one({"_id": key}, session=session)
            for key, row in after.items():
                if before[table].get(key) == row:
                    continue
                item = {"_id": key, "payload": self.cipher.seal(row)}
                if table == "pages":
                    item["source_message_id"] = self.cipher.index(row["source_message_id"])
                elif table == "patients":
                    for name in ("registry_file_number", "midwife_patient_code"):
                        if row[name] is not None:
                            item[name] = self.cipher.index(json.dumps([row["facility_id"], row[name]]))
                elif table == "visits":
                    item["identity"] = self.cipher.index(json.dumps([row["patient_id"], row["encounter_type"], row["visit_date"]]))
                self.db[table].replace_one({"_id": key}, item, upsert=True, session=session)

    def _row_key(self, table, row, pk):
        if table == "capture_groups":
            return self.cipher.index(json.dumps([row["group_id"], row["sender_id"]]))
        if table == "senders":
            return self.cipher.index(row[pk])
        return str(row[pk])

    @contextmanager
    def _unit(self, write):
        from pymongo.read_concern import ReadConcern
        from pymongo.write_concern import WriteConcern
        from pymongo.errors import OperationFailure, PyMongoError
        from .service import Conflict
        with self._lock, self.client.start_session() as session:
            conn = None
            try:
                with session.start_transaction(read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority")):
                    control_doc = self.db.control.find_one({"_id": "generation"}, session=session)
                    generation = control_doc["version"] if control_doc else 0
                    conn, before = self._load(session)
                    conn.execute("BEGIN IMMEDIATE")
                    yield conn
                    conn.execute("COMMIT")
                    if write:
                        if control_doc is None:
                            self.db.control.update_one({"_id": "generation"}, {"$setOnInsert": {"version": 1}}, upsert=True, session=session)
                        else:
                            changed = self.db.control.update_one({"_id": "generation", "version": generation}, {"$inc": {"version": 1}}, session=session)
                            if changed.modified_count != 1:
                                raise Conflict("STALE_STORAGE", "Central records changed; reload and retry")
                        self._persist(conn, before, session)
            except (OperationFailure, PyMongoError) as exc:
                if hasattr(exc, "has_error_label") and exc.has_error_label("TransientTransactionError"):
                    raise Conflict("STALE_STORAGE", "Central records changed; reload and retry") from None
                raise
            finally:
                if conn is not None:
                    conn.close()

    def tx(self):
        return self._unit(True)

    def read(self):
        return self._unit(False)

    def reset(self):
        from .service import Conflict
        raise Conflict("RESET_DISABLED", "Demo reset is disabled for Atlas")

    def close(self):
        self.client.close()
