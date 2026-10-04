"""SQLite persistence. One connection guarded by a lock; writes use BEGIN IMMEDIATE transactions.

With a cipher the database lives in memory and an AES-GCM encrypted snapshot is
written atomically after every committed transaction, so the file on disk is
never readable without the key.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

TABLES = ("events", "messages", "visits", "patients", "pages", "documents", "settings", "counters", "senders", "facilities")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS senders (
    sender_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL REFERENCES senders(sender_id),
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    status TEXT NOT NULL,
    grouping_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_page_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    draft_json TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    selection_json TEXT,
    registration_json TEXT,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    page_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    source_message_id TEXT NOT NULL UNIQUE,
    sender_id TEXT NOT NULL,
    media_ref TEXT NOT NULL,
    media_sha256 TEXT NOT NULL,
    position INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    retake_requested_at TEXT,
    retake_reason TEXT,
    superseded_by TEXT
);
CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    registry_file_number TEXT,
    midwife_patient_code TEXT,
    flags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS patients_file_number
    ON patients(facility_id, registry_file_number) WHERE registry_file_number IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS patients_code
    ON patients(facility_id, midwife_patient_code) WHERE midwife_patient_code IS NOT NULL;
CREATE TABLE IF NOT EXISTS visits (
    visit_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    encounter_type TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    source_documents_json TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    UNIQUE (patient_id, encounter_type, visit_date)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')),
    body TEXT,
    media_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


# Columns added after the first release: (table, column, declaration).
MIGRATIONS = (
    ("pages", "retake_requested_at", "TEXT"),
    ("pages", "retake_reason", "TEXT"),
    ("pages", "superseded_by", "TEXT"),
)


class Store:
    def __init__(self, path: str | Path, cipher=None):
        self.path = str(path)
        self.cipher = cipher if self.path != ":memory:" else None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        target = ":memory:" if self.cipher else self.path
        self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        if self.cipher and Path(self.path).exists():
            raw = Path(self.path).read_bytes()
            if raw.startswith(b"SQLite format 3\x00"):
                # A database written before encryption: load it once, then it is saved encrypted.
                plain = sqlite3.connect(self.path)
                plain.backup(self._conn)
                plain.close()
                for suffix in ("-wal", "-shm"):
                    Path(self.path + suffix).unlink(missing_ok=True)
            else:
                self._conn.deserialize(self.cipher.decrypt(raw, b"database"))
        self._conn.execute("PRAGMA foreign_keys = ON")
        if not self.cipher and self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._migrate()
        self._persist()

    def _migrate(self) -> None:
        for table, column, declaration in MIGRATIONS:
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _persist(self) -> None:
        """Write the encrypted snapshot (no-op for a plain database)."""
        if not self.cipher:
            return
        blob = self.cipher.encrypt(self._conn.serialize(), b"database")
        temporary = f"{self.path}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            self._persist()

    @contextmanager
    def read(self):
        with self._lock:
            yield self._conn

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = OFF")
            for table in TABLES:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA_SQL)
            self._persist()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def next_id(db: sqlite3.Connection, prefix: str) -> str:
    db.execute(
        "INSERT INTO counters(name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (prefix,),
    )
    value = db.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()[0]
    return f"{prefix}-{value:06d}"
