"""SQLite persistence. One connection guarded by a lock; writes use BEGIN IMMEDIATE transactions."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

TABLES = ("whatsapp_outbound", "whatsapp_inbound", "events", "messages", "visits", "patients", "retake_requests",
          "pages", "documents", "settings", "counters", "senders", "facilities")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS senders (
    sender_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    label TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'SIMULATOR' CHECK (channel IN ('SIMULATOR', 'WHATSAPP'))
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
    replaced_by TEXT,
    section_hint TEXT
);
CREATE TABLE IF NOT EXISTS retake_requests (
    request_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    page_id TEXT NOT NULL REFERENCES pages(page_id),
    sender_id TEXT NOT NULL REFERENCES senders(sender_id),
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'FULFILLED', 'CANCELLED')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    request_message_id INTEGER,
    replacement_page_id TEXT REFERENCES pages(page_id),
    replacement_message_id TEXT,
    closed_by TEXT,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS retake_one_pending_per_page
    ON retake_requests(page_id) WHERE status = 'PENDING';
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
CREATE TABLE IF NOT EXISTS whatsapp_inbound (
    inbound_id TEXT PRIMARY KEY,
    wa_message_id TEXT NOT NULL UNIQUE,
    message_type TEXT NOT NULL,
    sender_id TEXT,
    media_id TEXT,
    mime_type TEXT,
    media_sha256 TEXT,
    context_message_id TEXT,
    sent_at TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'DONE', 'FAILED', 'IGNORED', 'REJECTED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    media_ref TEXT,
    page_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS whatsapp_outbound (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    sender_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENT', 'DELIVERED', 'READ', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    wa_message_id TEXT UNIQUE,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
-- Queued in the same transaction as the message, so the service needs no change and simulator senders are never sent to.
CREATE TRIGGER IF NOT EXISTS whatsapp_outbound_enqueue AFTER INSERT ON messages
WHEN NEW.direction = 'OUT'
    AND EXISTS (SELECT 1 FROM senders WHERE sender_id = NEW.sender_id AND channel = 'WHATSAPP')
BEGIN
    INSERT INTO whatsapp_outbound(message_id, sender_id, status, next_attempt_at, updated_at)
    VALUES (NEW.id, NEW.sender_id, 'PENDING', NEW.created_at, NEW.created_at);
END;
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self._conn.executescript(SCHEMA_SQL)

    def _migrate(self) -> None:
        """Columns added after the first release; CREATE TABLE IF NOT EXISTS cannot add them to existing databases."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(pages)")}
        if columns and "replaced_by" not in columns:
            self._conn.execute("ALTER TABLE pages ADD COLUMN replaced_by TEXT")
        if columns and "section_hint" not in columns:
            self._conn.execute("ALTER TABLE pages ADD COLUMN section_hint TEXT")
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(senders)")}
        if columns and "channel" not in columns:
            self._conn.execute("ALTER TABLE senders ADD COLUMN channel TEXT NOT NULL DEFAULT 'SIMULATOR'")

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
