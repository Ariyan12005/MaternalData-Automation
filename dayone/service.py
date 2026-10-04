"""Domain operations: ingest, grouping, extraction queue, review, registration and timeline."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import export_columns, extended, linking, schema
from .extraction import (RETAKE_REASONS, ExtractionCancelled, ExtractionError, ExtractorUnavailable,
                         FixtureExtractor)
from .store import Store, next_id

log = logging.getLogger("dayone.service")

REVIEWABLE = {"AI_PROCESSED", "NEEDS_REVIEW", "VALIDATED", "PATIENT_MATCHED", "DUPLICATE_SUSPECTED"}
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png"}
INBOUND_MEDIA_PREFIX = "whatsapp-media/"
INBOUND_MEDIA_NAME = re.compile(r"[0-9a-f]{64}\.(?:jpg|png)")

DEMO_FACILITY = {"facility_id": "FAC-SIDI-SMAIL", "name": "C/S Sidi Smail"}
DEMO_SENDER = {"sender_id": "whatsapp:+212600000001", "label": "Sage-femme – C/S Sidi Smail"}
DEMO_HISTORY_PAGES = ("data/Paper Registry/1-1.jpg", "data/Paper Registry/1-4.jpg")
EXTERNAL_LEASE = timedelta(minutes=5)
# Tokens of in-process extraction leases; external workers get unprefixed tokens.
LOCAL_TOKEN_PREFIX = "local-"


class ServiceError(Exception):
    status = 400

    def __init__(self, code: str, message: str, details: object = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class NotFound(ServiceError):
    status = 404


class Forbidden(ServiceError):
    status = 403


class Conflict(ServiceError):
    status = 409


class Invalid(ServiceError):
    status = 422


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _loads(text: str | None):
    return None if text is None else json.loads(text)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _secret_equal(left: str, right: str) -> bool:
    digest = lambda value: hmac.new(b"dayone-secret", value.encode("utf-8"), hashlib.sha256).digest()
    return hmac.compare_digest(digest(left), digest(right))


class DayOneService:
    def __init__(self, store: Store, extractor, repo_root: Path, *, grouping_window_seconds: int = 8, clock=utcnow, media_store=None, inbound_media_dir: Path | None = None):
        self.store = store
        self.extractor = extractor
        self.repo_root = Path(repo_root).resolve()
        self.media_dir = (self.repo_root / "data" / "Paper Registry").resolve()
        self.inbound_media_dir = Path(inbound_media_dir).resolve() if inbound_media_dir else None
        self.window = timedelta(seconds=grouping_window_seconds)
        self.clock = clock
        self.media_store = media_store
        # Folder for the page files that extraction_media decrypts for path-based OCR (None: the system temp folder).
        # The server points it at its OCR run folder, which the next start removes if this process dies.
        self.extraction_temp_dir: Path | None = None
        # Lease of an in-process extraction. It must outlast the extractor's bounded run (the server sets it from the
        # OCR timeouts); external workers keep EXTERNAL_LEASE.
        self.local_lease = EXTERNAL_LEASE
        # Documents being extracted right now: {document_id: {"pages_done", "pages", "started_at", "last_page"}}.
        # In memory only; extraction runs on the extraction pool's thread.
        self._progress: dict[str, dict] = {}
        # Documents whose last extraction attempt hit an unavailable extractor or an unexpected error and that stay
        # queued for another attempt: {document_id: {"attempts", "reason", "failed_at"}}. Same lock, in memory only.
        self._retries: dict[str, dict] = {}
        self._progress_lock = threading.Lock()

    # ------------------------------------------------------------------ setup

    def ensure_seed(self) -> None:
        with self.store.read() as db:
            empty = db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0] == 0
        if empty:
            with self.store.tx() as db:
                db.execute("INSERT OR IGNORE INTO facilities(facility_id, name) VALUES (?, ?)", (DEMO_FACILITY["facility_id"], DEMO_FACILITY["name"]))
                db.execute("INSERT OR IGNORE INTO senders(sender_id, facility_id, label) VALUES (?, ?, ?)", (DEMO_SENDER["sender_id"], DEMO_FACILITY["facility_id"], DEMO_SENDER["label"]))
            with self.store.read() as db:
                pristine = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            if pristine and isinstance(self.extractor, FixtureExtractor):
                self._seed_prior_registration()

    def reset_demo(self, *, with_history: bool = True) -> None:
        self.store.reset()
        with self.store.tx() as db:
            db.execute("INSERT INTO facilities(facility_id, name) VALUES (?, ?)",
                       (DEMO_FACILITY["facility_id"], DEMO_FACILITY["name"]))
            db.execute("INSERT INTO senders(sender_id, facility_id, label) VALUES (?, ?, ?)",
                       (DEMO_SENDER["sender_id"], DEMO_FACILITY["facility_id"], DEMO_SENDER["label"]))
        if with_history:
            self._seed_prior_registration()

    def _seed_prior_registration(self) -> None:
        """An earlier digitization of the same booklet, already reviewed, so the demo has a patient to link to."""
        document_id = None
        for number, ref in enumerate(DEMO_HISTORY_PAGES, start=1):
            document_id = self.ingest_photo(
                sender_id=DEMO_SENDER["sender_id"], message_id=f"wamid.seed-{number}", media_ref=ref,
            )["document_id"]
        self.close_capture(document_id)
        # Always the reviewed fixture, whatever the extractor: the booklet photos are never sent to OCR.
        self.process_document(document_id, extractor=FixtureExtractor(self.repo_root / "fixtures"))
        self.select_patient(document_id, reviewer="seed", choice="NEW")
        self.confirm(document_id, reviewer="seed")

    # ------------------------------------------------------------------ system

    def system_info(self) -> dict:
        with self.store.read() as db:
            ai_available = self._ai_available(db)
        return {
            "ai_available": ai_available,
            "grouping_window_seconds": int(self.window.total_seconds()),
            "extractor": "external" if self.extractor is None else getattr(self.extractor, "name", type(self.extractor).__name__),
            "catalog": schema.catalog(),
            "retake_reasons": RETAKE_REASONS,
            "extracting": self.extraction_progress(),
        }

    def extraction_progress(self, document_id: str | None = None):
        """Progress of the documents being extracted (all, or one document's progress or None)."""
        with self._progress_lock:
            if document_id is not None:
                return dict(self._progress[document_id]) if document_id in self._progress else None
            return [{"document_id": d, **p} for d, p in sorted(self._progress.items())]

    def extraction_retry(self, document_id: str) -> dict | None:
        """The failed attempts of a document still queued for extraction, or None."""
        with self._progress_lock:
            return dict(self._retries[document_id]) if document_id in self._retries else None

    def _note_attempt(self, document_id: str, reason: str | None) -> None:
        with self._progress_lock:
            if reason is None:
                self._retries.pop(document_id, None)
                return
            previous = self._retries.get(document_id, {}).get("attempts", 0)
            self._retries[document_id] = {"attempts": previous + 1, "reason": reason, "failed_at": _iso(self.clock())}

    def set_ai_available(self, available: bool) -> dict:
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES ('ai_available', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if available else "0",),
            )
        return self.system_info()

    @staticmethod
    def _ai_available(db) -> bool:
        row = db.execute("SELECT value FROM settings WHERE key = 'ai_available'").fetchone()
        return row is None or row["value"] == "1"

    def list_senders(self) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute(
                "SELECT s.sender_id, s.label, s.facility_id, f.name AS facility_name "
                "FROM senders s JOIN facilities f USING (facility_id) ORDER BY s.label"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ media

    def list_media(self) -> list[str]:
        def sort_key(path: Path):
            return (0 if re.fullmatch(r"1-\d+\.jpg", path.name) else 1, path.name)

        files = [p for p in self.media_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
        return [p.relative_to(self.repo_root).as_posix() for p in sorted(files, key=sort_key)]

    def resolve_media(self, media_ref: str) -> Path:
        if not isinstance(media_ref, str) or not media_ref:
            raise Invalid("MEDIA_REQUIRED", "Référence d'image manquante.")
        if media_ref.startswith(INBOUND_MEDIA_PREFIX):
            name = media_ref[len(INBOUND_MEDIA_PREFIX):]
            if self.inbound_media_dir is None or not INBOUND_MEDIA_NAME.fullmatch(name):
                raise NotFound("MEDIA_NOT_FOUND", "Image introuvable.")
            path = self.inbound_media_dir / name
            if not path.is_file():
                raise NotFound("MEDIA_NOT_FOUND", "Image introuvable.")
            return path
        path = (self.repo_root / media_ref).resolve()
        if path.parent != self.media_dir or not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            raise NotFound("MEDIA_NOT_FOUND", "Image introuvable.")
        return path

    def read_media(self, media_ref):
        if isinstance(media_ref, str) and media_ref.startswith("secure/"):
            if self.media_store is None:
                raise NotFound("MEDIA_NOT_FOUND", "Encrypted media is unavailable")
            try:
                return self.media_store.get(media_ref)
            except (ValueError, FileNotFoundError):
                raise NotFound("MEDIA_NOT_FOUND", "Image unavailable") from None
        return self.resolve_media(media_ref).read_bytes()

    @contextmanager
    def extraction_media(self, page_refs):
        """Materialize originals for path-based OCR only for the extraction lifetime.

        Extractors that accept bytes should use read_media directly. Returned
        paths retain their original ref as the dictionary key for provenance.
        """
        with tempfile.TemporaryDirectory(prefix="dayone-extraction-", dir=self.extraction_temp_dir) as directory:
            paths = {}
            for index, ref in enumerate(page_refs):
                suffix = Path(ref).suffix.lower()
                if suffix not in MEDIA_SUFFIXES:
                    raise Invalid("INVALID_MEDIA", "Unsupported extraction media")
                path = Path(directory) / f"page-{index}{suffix}"
                path.write_bytes(self.read_media(ref))
                paths[ref] = path
            yield paths

    def ingest_upload(self, *, sender_id, message_id, image_base64, suffix=".jpg", group_id=None, retake_request_id=None):
        import base64
        import binascii
        if self.media_store is None:
            raise Invalid("SECURE_MEDIA_DISABLED", "Configure an encryption key for photo uploads")
        with self.store.read() as db:
            if db.execute("SELECT 1 FROM senders WHERE sender_id=?", (sender_id,)).fetchone() is None:
                raise Forbidden("UNKNOWN_SENDER", "Sender is not enrolled")
        try:
            if not isinstance(image_base64, str):
                raise ValueError()
            data = base64.b64decode("".join(image_base64.split()), validate=True)
            ref = self.media_store.put(data, (suffix or ".jpg").lower())
        except (ValueError, binascii.Error):
            raise Invalid("INVALID_IMAGE", "Upload must be a JPEG or PNG of at most 8 MiB") from None
        return self.ingest_photo(sender_id=sender_id, message_id=message_id, media_ref=ref, group_id=group_id,
                                 retake_request_id=retake_request_id)

    @staticmethod
    def _valid_visit_decision(decision, match):
        if isinstance(decision, str):
            return decision in ("UPDATE", "KEEP")
        return (isinstance(decision, dict) and set(decision) == {"action", "fields", "expected_version"}
                and decision["action"] == "UPDATE" and isinstance(decision["fields"], list)
                and bool(decision["fields"]) and all(isinstance(f, str) for f in decision["fields"])
                and set(decision["fields"]) <= {d["field"] for d in match["diffs"]})

    def enroll_sender(self, *, facility_id, facility_name, sender_id, label, channel=None):
        if channel is not None and channel not in ("SIMULATOR", "WHATSAPP"):
            raise Invalid("INVALID_CHANNEL", "Channel must be SIMULATOR or WHATSAPP")
        for value in (facility_id, facility_name, sender_id, label):
            if not isinstance(value, str) or not 1 <= len(value) <= 100:
                raise Invalid("INVALID_ENROLLMENT", "Enrollment fields must contain 1 to 100 characters")
        with self.store.tx() as db:
            prior = db.execute("SELECT facility_id FROM senders WHERE sender_id=?", (sender_id,)).fetchone()
            if prior and prior["facility_id"] != facility_id:
                raise Conflict("SENDER_FACILITY_CONFLICT", "Sender is already assigned to another facility")
            db.execute("INSERT OR IGNORE INTO facilities VALUES (?, ?)", (facility_id, facility_name))
            db.execute("INSERT OR IGNORE INTO senders(sender_id, facility_id, label) VALUES (?, ?, ?)", (sender_id, facility_id, label))
            if channel is not None:
                db.execute("UPDATE senders SET channel=? WHERE sender_id=?", (channel, sender_id))
        return {"ok": True}

    def claim_job(self, document_id=None):
        now = self.clock()
        with self.store.tx() as db:
            if not self._ai_available(db):
                return None
            rows = db.execute("SELECT * FROM documents WHERE status='PENDING_AI' ORDER BY document_id").fetchall()
            for document in rows:
                if document_id and document["document_id"] != document_id:
                    continue
                job = self._lease(db, document, now, EXTERNAL_LEASE, "")
                if job is not None:
                    return job
        return None

    def _lease(self, db, document, now: datetime, duration: timedelta, prefix: str) -> dict | None:
        """Lease the document's current revision, unless it is done or leased until later (by anyone)."""
        old = db.execute("SELECT * FROM jobs WHERE document_id=? AND revision=?", (document["document_id"], document["revision"])).fetchone()
        if old and (old["state"] == "DONE" or datetime.fromisoformat(old["lease_until"]) > now):
            return None
        job_id = old["job_id"] if old else next_id(db, "JOB")
        token = prefix + secrets.token_urlsafe(32)
        lease = _iso(now + duration)
        db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, 'LEASED') ON CONFLICT(job_id) DO UPDATE SET token=excluded.token, lease_until=excluded.lease_until, state='LEASED'", (job_id, document["document_id"], document["revision"], token, lease))
        return {"job_id": job_id, "document_id": document["document_id"], "expected_revision": document["revision"], "token": token, "lease_until": lease, "page_refs": self._page_refs(db, document["document_id"])}

    def release_local_leases(self) -> int:
        """At server start: the in-process extractions of a previous run ended with it, so their documents can be
        extracted again at once instead of after their lease. External leases are kept."""
        now = _iso(self.clock())
        with self.store.tx() as db:
            return db.execute("UPDATE jobs SET lease_until = ? WHERE state = 'LEASED' AND lease_until > ? "
                              "AND token LIKE ?", (now, now, LOCAL_TOKEN_PREFIX + "%")).rowcount

    @staticmethod
    def _job_held(job_row, job: dict, now: datetime) -> bool:
        return (job_row is not None and _secret_equal(job_row["token"], job["token"]) and job_row["state"] == "LEASED"
                and datetime.fromisoformat(job_row["lease_until"]) > now)

    def complete_job(self, job_id, *, token, draft, expected_revision):
        if type(expected_revision) is not int:
            raise Invalid("REVISION_REQUIRED", "Extraction callback requires expected_revision")
        problems = schema.validate_draft(draft)
        if problems:
            raise Invalid("INVALID_EXTRACTION", "Extraction violates shared schema", problems)
        for *_where, fv in [*schema.iter_fields(draft), *extended.iter_fields(draft)]:
            if fv["verification"]["state"] != "UNVERIFIED" or fv["corrections"]:
                raise Invalid("EXTRACTOR_VERIFICATION", "Extraction cannot claim human verification")
        now = self.clock()
        with self.store.tx() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or not isinstance(token, str) or not _secret_equal(job["token"], token):
                raise Forbidden("INVALID_JOB", "Invalid extraction lease")
            if job["state"] == "DONE":
                return {"ok": True, "replayed": True}
            document = self._load_document(db, job["document_id"])
            self._check_revision(document, expected_revision)
            if document["revision"] != job["revision"] or document["status"] != "PENDING_AI" or datetime.fromisoformat(job["lease_until"]) <= now:
                raise Conflict("STALE_JOB", "Extraction lease expired or document changed")
            if set(p["page_ref"] for p in draft["pages"]) != set(self._page_refs(db, job["document_id"])):
                raise Invalid("PAGE_MISMATCH", "Extraction pages differ from capture")
            draft["extraction"]["processed_at"] = _iso(now)
            self._save_draft(db, job["document_id"], draft, now)
            self._set_status(db, job["document_id"], "AI_PROCESSED", now)
            self._recompute_status(db, job["document_id"], now)
            db.execute("UPDATE jobs SET state='DONE' WHERE job_id=?", (job_id,))
        return {"ok": True, "replayed": False}

    def fail_job(self, job_id, *, token, expected_revision, code):
        if type(expected_revision) is not int:
            raise Invalid("REVISION_REQUIRED", "Provide integer expected_revision")
        if code not in ("ILLEGIBLE_IMAGE", "UNSUPPORTED_LAYOUT", "EXTRACTION_FAILED"):
            raise Invalid("INVALID_FAILURE", "Use an allowed extraction failure code")
        now = self.clock()
        with self.store.tx() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or not isinstance(token, str) or not _secret_equal(job["token"], token):
                raise Forbidden("INVALID_JOB", "Invalid extraction lease")
            if job["state"] == "FAILED":
                return {"ok": True, "replayed": True}
            document = self._load_document(db, job["document_id"])
            self._check_revision(document, expected_revision)
            if (job["state"] != "LEASED" or document["revision"] != job["revision"]
                    or document["status"] != "PENDING_AI"
                    or datetime.fromisoformat(job["lease_until"]) <= now):
                raise Conflict("STALE_JOB", "Extraction lease expired or document changed")
            db.execute("UPDATE jobs SET state='FAILED' WHERE job_id=?", (job_id,))
            db.execute("UPDATE documents SET failure_reason=?, revision=revision+1 WHERE document_id=?",
                       (code, job["document_id"]))
            self._set_status(db, job["document_id"], "PROCESSING_FAILED", now, detail={"code": code})
        return {"ok": True, "replayed": False}

    # ------------------------------------------------------------------ ingest

    def ingest_photo(self, *, sender_id: str, message_id: str, media_ref: str, group_id: str | None = None,
                     retake_request_id: str | None = None) -> dict:
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 200:
            raise Invalid("MESSAGE_ID_REQUIRED", "Message identifier required")
        if group_id is not None and (not isinstance(group_id, str) or not 1 <= len(group_id) <= 100):
            raise Invalid("INVALID_GROUP", "Group identifier must contain 1 to 100 characters")
        if retake_request_id is not None and not isinstance(retake_request_id, str):
            raise Invalid("RETAKE_ID_INVALID", "Invalid retake identifier")
        digest = hashlib.sha256(self.read_media(media_ref)).hexdigest()
        now = self.clock()
        with self.store.tx() as db:
            duplicate = db.execute(
                "SELECT page_id, document_id, position FROM pages WHERE source_message_id = ?", (message_id,)
            ).fetchone()
            if duplicate:
                return {**dict(duplicate), "duplicate": True, "acknowledgment": None}

            sender = db.execute("SELECT * FROM senders WHERE sender_id = ?", (sender_id,)).fetchone()
            if sender is None:
                raise Forbidden("UNKNOWN_SENDER", "Numéro non enregistré auprès d'un établissement.")
            if retake_request_id is not None:
                return self._ingest_replacement(db, sender, message_id, media_ref, digest, retake_request_id, now)

            group = db.execute("SELECT document_id FROM capture_groups WHERE group_id=? AND sender_id=?", (group_id, sender_id)).fetchone() if group_id else None
            document = self._load_document(db, group["document_id"]) if group else self._open_document(db, sender_id, now)
            if group and document["status"] != "CAPTURED":
                raise Conflict("GROUP_CLOSED", "Capture group is closed; use a new group ID")
            if group_id and not group:
                document = None
            if document is None:
                document_id = next_id(db, "DOC")
                db.execute(
                    "INSERT INTO documents(document_id, sender_id, facility_id, status, grouping_status, "
                    "created_at, last_page_at, updated_at) VALUES (?, ?, ?, 'CAPTURED', 'PROVISIONAL', ?, ?, ?)",
                    (document_id, sender_id, sender["facility_id"], _iso(now), _iso(now), _iso(now)),
                )
                self._event(db, document_id, "STATUS_CHANGED", "system", now, {"from": None, "to": "CAPTURED"})
            else:
                document_id = document["document_id"]
                db.execute("UPDATE documents SET last_page_at = ?, updated_at = ? WHERE document_id = ?",
                           (_iso(now), _iso(now), document_id))

            if group_id and not group:
                db.execute("INSERT INTO capture_groups VALUES (?, ?, ?)", (group_id, sender_id, document_id))
            position = db.execute("SELECT COUNT(*) FROM pages WHERE document_id = ? AND replaced_by IS NULL", (document_id,)).fetchone()[0] + 1
            page_id = next_id(db, "PAGE")
            db.execute(
                "INSERT INTO pages(page_id, document_id, source_message_id, sender_id, media_ref, media_sha256, "
                "position, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (page_id, document_id, message_id, sender_id, media_ref, digest, position, _iso(now)),
            )
            acknowledgment = f"Reçu : page {position}. Merci, le traitement est en cours."
            db.execute("INSERT INTO messages(sender_id, direction, body, media_ref, created_at) VALUES (?, 'IN', NULL, ?, ?)",
                       (sender_id, media_ref, _iso(now)))
            db.execute("INSERT INTO messages(sender_id, direction, body, media_ref, created_at) VALUES (?, 'OUT', ?, NULL, ?)",
                       (sender_id, acknowledgment, _iso(now)))
            self._event(db, document_id, "PAGE_RECEIVED", "system", now, {"page_id": page_id, "position": position})
        return {"page_id": page_id, "document_id": document_id, "position": position,
                "duplicate": False, "acknowledgment": acknowledgment}

    def _ingest_replacement(self, db, sender, message_id: str, media_ref: str, digest: str,
                            request_id: str, now: datetime) -> dict:
        request = db.execute("SELECT * FROM retake_requests WHERE request_id = ?", (request_id,)).fetchone()
        if request is None:
            raise NotFound("RETAKE_NOT_FOUND", "Demande de reprise introuvable.")
        if sender["facility_id"] != request["facility_id"]:
            raise Forbidden("OTHER_FACILITY", "Cette demande de reprise appartient à un autre établissement.")
        if sender["sender_id"] != request["sender_id"]:
            raise Forbidden("NOT_REQUEST_RECIPIENT", "Cette demande de reprise a été envoyée à un autre numéro.")
        if request["status"] != "PENDING":
            raise Conflict("RETAKE_NOT_PENDING", "Cette demande de reprise est déjà close.", {"status": request["status"]})
        original = db.execute("SELECT * FROM pages WHERE page_id = ?", (request["page_id"],)).fetchone()
        document = self._load_document(db, original["document_id"])
        if document["status"] in ("REGISTERED", "SYNCED"):
            raise Conflict("ALREADY_REGISTERED", "Ce dossier est déjà enregistré.")

        page_id = next_id(db, "PAGE")
        db.execute(
            "INSERT INTO pages(page_id, document_id, source_message_id, sender_id, media_ref, media_sha256, "
            "position, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (page_id, document["document_id"], message_id, sender["sender_id"], media_ref, digest,
             original["position"], _iso(now)),
        )
        db.execute("UPDATE pages SET replaced_by = ? WHERE page_id = ?", (page_id, original["page_id"]))
        db.execute(
            "UPDATE retake_requests SET status = 'FULFILLED', replacement_page_id = ?, replacement_message_id = ?, "
            "closed_at = ? WHERE request_id = ?",
            (page_id, message_id, _iso(now), request_id),
        )
        acknowledgment = f"Reçu : nouvelle photo de la page {original['position']}. Merci, le traitement est en cours."
        db.execute("INSERT INTO messages(sender_id, direction, body, media_ref, created_at) VALUES (?, 'IN', NULL, ?, ?)",
                   (sender["sender_id"], media_ref, _iso(now)))
        db.execute("INSERT INTO messages(sender_id, direction, body, media_ref, created_at) VALUES (?, 'OUT', ?, NULL, ?)",
                   (sender["sender_id"], acknowledgment, _iso(now)))

        detail = {"request_id": request_id, "page_id": page_id, "replaced_page_id": original["page_id"],
                  "position": original["position"], **self._discard_review(db, document["document_id"], now)}
        self._event(db, document["document_id"], "PAGE_REPLACED", "system", now, detail)
        if document["status"] != "CAPTURED":
            self._set_status(db, document["document_id"], "PENDING_AI", now)
        return {"page_id": page_id, "document_id": document["document_id"], "position": original["position"],
                "duplicate": False, "acknowledgment": acknowledgment,
                "retake_request_id": request_id, "replaced_page_id": original["page_id"]}

    # ------------------------------------------------------------------ retakes

    def request_retake(self, page_id: str, *, reviewer: str) -> dict:
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            page = db.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            if page is None:
                raise NotFound("PAGE_NOT_FOUND", "Page introuvable.")
            if page["replaced_by"]:
                raise Conflict("PAGE_REPLACED", f"Cette page a déjà été remplacée par {page['replaced_by']}.")
            document = self._load_document(db, page["document_id"])
            if document["status"] in ("REGISTERED", "SYNCED"):
                raise Conflict("ALREADY_REGISTERED",
                               "Ce dossier est déjà enregistré : les nouvelles photos formeront un nouveau dossier.")
            existing = db.execute("SELECT request_id FROM retake_requests WHERE page_id = ? AND status = 'PENDING'",
                                  (page_id,)).fetchone()
            if existing:
                return {**self._retakes(db, "r.request_id = ?", (existing["request_id"],))[0], "created": False}

            request_id = next_id(db, "RTK")
            reason = self._retake_reason(db, document, page)
            body = f"Merci de reprendre la photo de la page {page['position']}" + (
                f" : {RETAKE_REASONS[reason]}" if reason else ".")
            message = db.execute(
                "INSERT INTO messages(sender_id, direction, body, media_ref, created_at) VALUES (?, 'OUT', ?, NULL, ?)",
                (page["sender_id"], body, _iso(now)),
            )
            db.execute(
                "INSERT INTO retake_requests(request_id, document_id, page_id, sender_id, facility_id, status, "
                "requested_by, requested_at, request_message_id) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
                (request_id, document["document_id"], page_id, page["sender_id"], document["facility_id"],
                 reviewer, _iso(now), message.lastrowid),
            )
            self._event(db, document["document_id"], "RETAKE_REQUESTED", reviewer, now,
                        {"request_id": request_id, "page_id": page_id, "position": page["position"], "reason": reason})
            return {**self._retakes(db, "r.request_id = ?", (request_id,))[0], "created": True}

    def _retake_reason(self, db, document, page) -> str | None:
        """Why the extractor could not use this page's photo (a RETAKE_REASONS key), from the draft or the failure."""
        if document["draft_json"]:
            draft = json.loads(document["draft_json"])
            index = page["position"] - 1
            if 0 <= index < len(draft["pages"]) and draft["pages"][index]["page_ref"] == page["media_ref"]:
                reason = draft["pages"][index].get("retake_reason")
                return reason if reason in RETAKE_REASONS else None
            return None
        detail = self._failure_detail(db, document)
        reason = next((p["reason"] for p in detail.get("pages", []) if p["page_id"] == page["page_id"]), None)
        return reason if reason in RETAKE_REASONS else None

    @staticmethod
    def _failure_detail(db, document) -> dict:
        """Detail of the extraction failure that put the document in PROCESSING_FAILED ({} otherwise)."""
        if document["status"] != "PROCESSING_FAILED":
            return {}
        for row in db.execute("SELECT detail_json FROM events WHERE document_id = ? AND type = 'STATUS_CHANGED' "
                              "ORDER BY id DESC", (document["document_id"],)):
            detail = json.loads(row["detail_json"])
            if detail.get("to") == "PROCESSING_FAILED":
                return detail
        return {}

    def cancel_retake(self, request_id: str, *, reviewer: str) -> dict:
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            request = db.execute("SELECT * FROM retake_requests WHERE request_id = ?", (request_id,)).fetchone()
            if request is None:
                raise NotFound("RETAKE_NOT_FOUND", "Demande de reprise introuvable.")
            if request["status"] == "FULFILLED":
                raise Conflict("RETAKE_NOT_PENDING", "La nouvelle photo a déjà été reçue.", {"status": "FULFILLED"})
            if request["status"] == "PENDING":
                db.execute("UPDATE retake_requests SET status = 'CANCELLED', closed_by = ?, closed_at = ? "
                           "WHERE request_id = ?", (reviewer, _iso(now), request_id))
                position = db.execute("SELECT position FROM pages WHERE page_id = ?",
                                      (request["page_id"],)).fetchone()["position"]
                db.execute(
                    "INSERT INTO messages(sender_id, direction, body, media_ref, created_at) VALUES (?, 'OUT', ?, NULL, ?)",
                    (request["sender_id"], f"Demande annulée : inutile de reprendre la photo de la page {position}.",
                     _iso(now)),
                )
                self._event(db, request["document_id"], "RETAKE_CANCELLED", reviewer, now,
                            {"request_id": request_id, "page_id": request["page_id"], "position": position})
            return self._retakes(db, "r.request_id = ?", (request_id,))[0]

    @staticmethod
    def _retakes(db, where: str, params: tuple) -> list[dict]:
        rows = db.execute(
            "SELECT r.*, p.position, p.media_ref AS original_media_ref, rp.media_ref AS replacement_media_ref "
            "FROM retake_requests r JOIN pages p ON p.page_id = r.page_id "
            f"LEFT JOIN pages rp ON rp.page_id = r.replacement_page_id WHERE {where} ORDER BY r.request_id",
            params,
        ).fetchall()
        keys = ("request_id", "document_id", "page_id", "position", "status", "requested_by", "requested_at",
                "original_media_ref", "replacement_page_id", "replacement_media_ref", "closed_by", "closed_at")
        return [{key: row[key] for key in keys} for row in rows]

    def _open_document(self, db, sender_id: str, now: datetime):
        row = db.execute(
            "SELECT * FROM documents WHERE sender_id = ? AND status = 'CAPTURED' AND document_id NOT IN (SELECT document_id FROM capture_groups) ORDER BY document_id DESC LIMIT 1",
            (sender_id,),
        ).fetchone()
        if row is None:
            return None
        if now - datetime.fromisoformat(row["last_page_at"]) <= self.window:
            return row
        self._set_status(db, row["document_id"], "PENDING_AI", now)
        return None

    def thread(self, sender_id: str) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute(
                "SELECT m.id, m.direction, m.body, m.media_ref, m.created_at, "
                "r.request_id AS retake_request_id, r.status AS retake_status "
                "FROM messages m LEFT JOIN retake_requests r ON r.request_message_id = m.id "
                "WHERE m.sender_id = ? ORDER BY m.id",
                (sender_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ queue

    def close_capture(self, document_id: str) -> dict:
        with self.store.tx() as db:
            document = self._load_document(db, document_id)
            if document["status"] == "CAPTURED":
                self._set_status(db, document_id, "PENDING_AI", self.clock())
        return {"ok": True}

    def queued_documents(self) -> list[str]:
        """Close expired grouping windows; return the documents waiting for extraction (none while AI is off)."""
        now = self.clock()
        with self.store.tx() as db:
            for row in db.execute("SELECT document_id, last_page_at FROM documents WHERE status = 'CAPTURED' AND document_id NOT IN (SELECT document_id FROM capture_groups)").fetchall():
                if now - datetime.fromisoformat(row["last_page_at"]) > self.window:
                    self._set_status(db, row["document_id"], "PENDING_AI", now)
            if not self._ai_available(db):
                return []
            return [row["document_id"] for row in db.execute(
                "SELECT document_id FROM documents WHERE status = 'PENDING_AI' ORDER BY document_id")]

    def tick(self) -> list[str]:
        """Close expired grouping windows, then extract queued documents in this thread if AI is available.

        The server uses dayone/extraction_pool.py instead, so that extraction never delays WhatsApp jobs.
        """
        return [document_id for document_id in self.queued_documents() if self.process_document(document_id)]

    def process_document(self, document_id: str, *, extractor=None) -> bool:
        return self.extract_document(document_id, extractor=extractor) == "DONE"

    def extract_document(self, document_id: str, *, extractor=None) -> str:
        """Extract one queued document in this process, under an extraction lease like an external worker's.

        Returns DONE (draft saved or failure recorded), STALE (pages, section choices or revision changed meanwhile,
        or the lease was lost: extract again now), RETRY (extractor unavailable: try again later) or SKIPPED (no
        local extractor, AI off, not queued, or leased by another worker)."""
        extractor = extractor or self.extractor
        if extractor is None:
            return "SKIPPED"
        with self.store.tx() as db:
            document = db.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
            if document is None or document["status"] != "PENDING_AI" or not self._ai_available(db):
                return "SKIPPED"
            job = self._lease(db, document, self.clock(), self.local_lease, LOCAL_TOKEN_PREFIX)
            if job is None:
                return "SKIPPED"
            state = self._page_state(db, document_id)
        refs = [ref for _page_id, ref, _hint in state]

        def is_current() -> bool:
            with self.store.read() as db:
                return self._still_current(db, document_id, job, state, self.clock())

        def progress(done: int, total: int, outcome: str) -> None:
            with self._progress_lock:
                entry = self._progress.get(document_id)
                if entry is not None:
                    entry.update(pages_done=done, pages=total, last_page=outcome)
            log.info("extraction of %s: page %d/%d %s", document_id, done, total, outcome)

        draft, failure = None, None
        with self._progress_lock:
            self._progress[document_id] = {"pages_done": 0, "pages": len(refs), "started_at": _iso(self.clock()),
                                           "last_page": None}
        try:
            if getattr(extractor, "accepts_page_context", False):
                draft = extractor.extract(refs, section_hints=[hint for *_rest, hint in state], is_current=is_current,
                                          progress=progress)
            else:
                draft = extractor.extract(refs)
            problems = schema.validate_draft(draft)
            if problems:
                draft, failure = None, ExtractionError("INVALID_EXTRACTION", problems[0])
        except ExtractionError as exc:
            failure = exc
        except ExtractionCancelled:
            self._release_lease(job)
            return "STALE"
        except ExtractorUnavailable as exc:
            log.warning("extractor unavailable for %s (%s); will retry", document_id, exc.code)
            self._release_lease(job)
            self._note_attempt(document_id, exc.code)
            return "RETRY"
        except Exception as exc:
            # The type only: a message or traceback may quote page text or draft values.
            log.error("extraction error for %s (%s); will retry", document_id, type(exc).__name__)
            self._release_lease(job)
            self._note_attempt(document_id, "EXTRACTION_ERROR")
            return "RETRY"
        finally:
            with self._progress_lock:
                self._progress.pop(document_id, None)

        if draft is not None:
            problem = self._draft_problem(draft, refs)
            if problem:
                draft, failure = None, ExtractionError(*problem)
        now = self.clock()
        with self.store.tx() as db:
            if not self._still_current(db, document_id, job, state, now):
                self._release_lease(job, db)
                return "STALE"
            if draft is None:
                detail = {"code": failure.code, "message": failure.message}
                pages = [{"page_id": state[p["index"]][0], "position": p["index"] + 1, "reason": p["reason"]}
                         for p in getattr(failure, "pages", []) if 0 <= p.get("index", -1) < len(state)]
                if pages:
                    detail["pages"] = pages
                db.execute("UPDATE jobs SET state = 'FAILED' WHERE job_id = ?", (job["job_id"],))
                db.execute("UPDATE documents SET failure_reason = ? WHERE document_id = ?", (failure.code, document_id))
                self._set_status(db, document_id, "PROCESSING_FAILED", now, detail=detail)
            else:
                draft["extraction"]["processed_at"] = _iso(now)
                self._save_draft(db, document_id, draft, now)
                db.execute("UPDATE jobs SET state = 'DONE' WHERE job_id = ?", (job["job_id"],))
                db.execute("UPDATE documents SET failure_reason = NULL WHERE document_id = ?", (document_id,))
                self._set_status(db, document_id, "AI_PROCESSED", now)
                self._recompute_status(db, document_id, now)
        self._note_attempt(document_id, None)
        return "DONE"

    def _still_current(self, db, document_id: str, job: dict, state: list, now: datetime) -> bool:
        """The lease is still this extraction's, and the document is still the queued revision with the same pages
        and section choices (page IDs, not media refs: a replacement photo may reuse the same media ref)."""
        document = db.execute("SELECT status, revision FROM documents WHERE document_id = ?", (document_id,)).fetchone()
        return (document is not None and document["status"] == "PENDING_AI"
                and document["revision"] == job["expected_revision"]
                and self._job_held(db.execute("SELECT * FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone(), job, now)
                and self._page_state(db, document_id) == state)

    def _release_lease(self, job: dict, db=None) -> None:
        """Ends this extraction's lease early so that the document can be claimed again without waiting for it."""
        if db is None:
            with self.store.tx() as db:
                self._release_lease(job, db)
            return
        db.execute("UPDATE jobs SET lease_until = ? WHERE job_id = ? AND token = ? AND state = 'LEASED'",
                   (_iso(self.clock()), job["job_id"], job["token"]))

    @staticmethod
    def _draft_problem(draft: dict, page_refs: list[str]) -> tuple[str, str] | None:
        """What an extractor may not do, locally or through the job API: claim a human review, or describe other
        pages than the ones it was given."""
        for *_rest, fv in [*schema.iter_fields(draft), *extended.iter_fields(draft)]:
            if fv["verification"]["state"] != "UNVERIFIED" or fv["corrections"]:
                return "EXTRACTOR_VERIFICATION", "Extraction cannot claim human verification"
        if set(p["page_ref"] for p in draft["pages"]) != set(page_refs):
            return "PAGE_MISMATCH", "Extraction pages differ from capture"
        return None

    def set_page_section(self, page_id: str, *, reviewer: str, section: str | None,
                         expected_revision: int | None = None) -> dict:
        """Record the reviewer's section for a page (None: back to detection) and extract the document again."""
        reviewer = self._reviewer(reviewer)
        if section is not None and section not in schema.PAGE_SECTIONS:
            raise Invalid("UNKNOWN_SECTION", "Section de page inconnue.")
        now = self.clock()
        with self.store.tx() as db:
            page = db.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            if page is None:
                raise NotFound("PAGE_NOT_FOUND", "Page introuvable.")
            if page["replaced_by"]:
                raise Conflict("PAGE_REPLACED", f"Cette page a été remplacée par {page['replaced_by']}.")
            document = self._load_document(db, page["document_id"])
            if document["status"] in ("REGISTERED", "SYNCED"):
                raise Conflict("ALREADY_REGISTERED", "Ce dossier est déjà enregistré.")
            self._check_revision(document, expected_revision)
            if page["section_hint"] == section:
                return self.get_document(document["document_id"])
            db.execute("UPDATE pages SET section_hint = ? WHERE page_id = ?", (section, page_id))
            detail = {"page_id": page_id, "position": page["position"], "section": section,
                      "previous": page["section_hint"]}
            if document["status"] not in ("CAPTURED", "PENDING_AI"):
                detail.update(self._discard_review(db, document["document_id"], now))
            else:
                db.execute("UPDATE documents SET revision = revision + 1, updated_at = ? WHERE document_id = ?",
                           (_iso(now), document["document_id"]))
            self._event(db, document["document_id"], "PAGE_SECTION_SET", reviewer, now, detail)
            if document["status"] != "CAPTURED":
                self._set_status(db, document["document_id"], "PENDING_AI", now, actor=reviewer)
        return self.get_document(document["document_id"])

    def start_manual_entry(self, document_id: str, *, reviewer: str, expected_revision: int | None = None) -> dict:
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            document = self._load_document(db, document_id)
            self._check_revision(document, expected_revision)
            if document["status"] not in ("PROCESSING_FAILED", "PENDING_AI"):
                raise Conflict("NOT_FAILED", "La saisie manuelle est réservée aux dossiers en échec d'extraction.")
            self._save_draft(db, document_id, schema.manual_draft(self._page_refs(db, document_id), _iso(now)), now)
            self._event(db, document_id, "MANUAL_ENTRY_STARTED", reviewer, now, {})
            self._set_status(db, document_id, "NEEDS_REVIEW", now, actor=reviewer)
        return self.get_document(document_id)

    # ------------------------------------------------------------------ review

    def review_field(self, document_id: str, *, reviewer: str, scope: str, field: str, action: str,
                     encounter_index: int | None = None, value: object = None, field_status: str | None = None,
                     expected_revision: int | None = None, section: str | None = None,
                     item_index: int | None = None) -> dict:
        """scope "extended" reviews a field of draft["extended"] (section, item_index for list sections)."""
        reviewer = self._reviewer(reviewer)
        spec = extended.FIELDS.get(field) if scope == "extended" else schema.FIELDS.get(field)
        if spec is None or (scope != "extended" and spec.scope != scope) or (
                scope == "extended" and extended.SECTION_OF[field] != section):
            raise Invalid("UNKNOWN_FIELD", "Champ inconnu pour cette section.")
        now = self.clock()
        with self.store.tx() as db:
            document = self._load_reviewable(db, document_id, expected_revision)
            draft = json.loads(document["draft_json"])
            try:
                if scope == "extended":
                    fv = extended.get_field(draft, section, item_index, field)
                else:
                    fv = schema.get_field(draft, scope, field, encounter_index)
            except KeyError:
                raise Invalid("UNKNOWN_ENCOUNTER", "Visite ou élément inconnu dans ce dossier.") from None
            previous = {"value": fv["value"], "field_status": fv["field_status"]}

            if action == "CONFIRM":
                if fv["value"] is None and spec.required:
                    raise Invalid("VALUE_REQUIRED", f"« {spec.label} » est obligatoire : saisissez la valeur.")
                if fv["value"] is None and fv["field_status"] == "NEEDS_REVIEW":
                    raise Invalid("NOTHING_TO_CONFIRM", "Aucune valeur lue : corrigez ou choisissez un statut.")
                if fv["field_status"] == "NEEDS_REVIEW":
                    fv["field_status"] = "KNOWN"
                fv["verification"] = {"state": "CONFIRMED", "by": reviewer, "at": _iso(now)}
            elif action in ("CORRECT", "SET_STATUS"):
                if action == "CORRECT":
                    try:
                        fv["value"] = spec.parse(value)
                    except schema.InvalidValue as exc:
                        raise Invalid("INVALID_VALUE", f"{spec.label} : {exc}") from None
                    fv["field_status"] = "KNOWN"
                else:
                    if field_status not in schema.MISSING_STATUSES:
                        raise Invalid("INVALID_STATUS", "Statut non autorisé.")
                    if spec.required:
                        raise Invalid("VALUE_REQUIRED", f"« {spec.label} » est obligatoire : saisissez la valeur.")
                    fv["value"], fv["field_status"] = None, field_status
                fv["verification"] = {"state": "CORRECTED", "by": reviewer, "at": _iso(now)}
                fv["corrections"].append({
                    "at": _iso(now), "by": reviewer, "action": action, "previous": previous,
                    "new": {"value": fv["value"], "field_status": fv["field_status"]},
                })
            else:
                raise Invalid("UNKNOWN_ACTION", "Action inconnue.")

            self._save_draft(db, document_id, draft, now)
            if field in schema.KEY_FIELDS:
                db.execute("UPDATE documents SET selection_json=NULL WHERE document_id=?", (document_id,))
            slot = draft["encounters"][encounter_index]["slot"] if scope == "encounter" else None
            where = {"section": section, "item_index": item_index} if scope == "extended" else {}
            self._event(db, document_id, "FIELD_REVIEWED", reviewer, now, {
                "scope": scope, "encounter_index": encounter_index, "slot": slot, **where, "field": field,
                "action": action, "previous": previous,
                "new": {"value": fv["value"], "field_status": fv["field_status"]},
            })
            self._recompute_status(db, document_id, now)
        return self.get_document(document_id)

    def select_patient(self, document_id: str, *, reviewer: str, choice: str, patient_id: str | None = None,
                       expected_revision: int | None = None) -> dict:
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            document = self._load_reviewable(db, document_id, expected_revision)
            if choice == "EXISTING":
                patient = db.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
                if patient is None:
                    raise NotFound("PATIENT_NOT_FOUND", "Patiente introuvable.")
                if patient["facility_id"] != document["facility_id"]:
                    raise Forbidden("OTHER_FACILITY", "Cette patiente appartient à un autre établissement.")
            elif choice in ("NEW", "UNSURE"):
                patient_id = None
            else:
                raise Invalid("UNKNOWN_CHOICE", "Choix inconnu.")
            selection = {"choice": choice, "patient_id": patient_id, "by": reviewer, "at": _iso(now)}
            db.execute(
                "UPDATE documents SET selection_json = ?, revision = revision + 1, updated_at = ? WHERE document_id = ?",
                (_dumps(selection), _iso(now), document_id),
            )
            self._event(db, document_id, "PATIENT_SELECTED", reviewer, now, selection)
            self._recompute_status(db, document_id, now)
        return self.get_document(document_id)

    def confirm(self, document_id: str, *, reviewer: str, existing_visit_decisions: dict | None = None,
                expected_revision: int | None = None) -> dict:
        reviewer = self._reviewer(reviewer)
        decisions = {str(key): value for key, value in (existing_visit_decisions or {}).items()}
        now = self.clock()
        with self.store.tx() as db:
            document = self._load_document(db, document_id)
            if document["status"] in ("REGISTERED", "SYNCED"):
                return {**json.loads(document["registration_json"]), "replayed": True}
            pending = db.execute("SELECT request_id FROM retake_requests WHERE document_id = ? AND status = 'PENDING'",
                                 (document_id,)).fetchall()
            if pending:
                raise Conflict("RETAKE_PENDING", "Une reprise de photo est en attente : attendez la nouvelle photo "
                               "ou annulez la demande.", {"retake_request_ids": [row[0] for row in pending]})
            draft = _loads(document["draft_json"])
            if document["status"] != "PATIENT_MATCHED":
                raise Conflict("NOT_READY", "Le dossier n'est pas prêt : vérifiez les champs et choisissez la patiente.", {
                    "status": document["status"],
                    "blocking": schema.blocking_fields(draft) if draft else [],
                })
            self._check_revision(document, expected_revision)
            selection = json.loads(document["selection_json"])

            for _scope, _index, _slot, _name, fv in schema.iter_fields(draft):
                if fv["verification"]["state"] == "UNVERIFIED":
                    fv["verification"] = {"state": "CONFIRMED", "by": reviewer, "at": _iso(now)}

            keys = {key: draft["document_fields"][key]["value"] for key in schema.KEY_FIELDS}
            if selection["choice"] == "NEW":
                patient_id = self._create_patient(db, document["facility_id"], keys, reviewer, now)
            else:
                patient_id = selection["patient_id"]
                self._fill_patient_keys(db, patient_id, document["facility_id"], keys)

            matches = linking.encounter_matches(db, patient_id, draft)
            undecided = [m for m in matches if m["outcome"] == "EXISTS_DIFFERENT"
                         and not self._valid_visit_decision(decisions.get(str(m["encounter_index"])), m)]
            if undecided:
                raise Conflict("DECISION_REQUIRED", "Certaines visites existent déjà avec des valeurs différentes.",
                               {"encounters": undecided})

            for match in matches:
                decision = decisions.get(str(match["encounter_index"]))
                if isinstance(decision, dict) and decision["expected_version"] != match.get("existing_version"):
                    raise Conflict("STALE_VISIT", "Existing visit changed; reload its summary before updating")
            visits = [self._register_encounter(db, document_id, patient_id, draft, match,
                                               decisions.get(str(match["encounter_index"])), reviewer, now)
                      for match in matches]
            registration = {
                "document_id": document_id, "patient_id": patient_id,
                "patient_created": selection["choice"] == "NEW", "visits": visits,
                "registered_by": reviewer, "registered_at": _iso(now),
            }
            db.execute(
                "UPDATE documents SET draft_json = ?, registration_json = ?, grouping_status = 'CONFIRMED', "
                "revision = revision + 1, updated_at = ? WHERE document_id = ?",
                (_dumps(draft), _dumps(registration), _iso(now), document_id),
            )
            self._event(db, document_id, "REGISTERED", reviewer, now,
                        {"patient_id": patient_id, "visits": [v["visit_id"] for v in visits]})
            self._set_status(db, document_id, "REGISTERED", now, actor=reviewer)
        return {**registration, "replayed": False}

    def _register_encounter(self, db, document_id, patient_id, draft, match, decision, reviewer, now) -> dict:
        encounter = draft["encounters"][match["encounter_index"]]
        fields = encounter["fields"]
        summary = {"visit_date": match["visit_date"], "slot": encounter["slot"]}
        if match["outcome"] == "NEW":
            visit_id = next_id(db, "VIS")
            db.execute(
                "INSERT INTO visits(visit_id, patient_id, encounter_type, visit_date, slot, fields_json, "
                "source_documents_json, created_at, updated_at, registered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (visit_id, patient_id, encounter["encounter_type"], match["visit_date"], encounter["slot"],
                 _dumps(fields), _dumps([document_id]), _iso(now), _iso(now), reviewer),
            )
            return {**summary, "visit_id": visit_id, "outcome": "CREATED"}

        row = db.execute("SELECT * FROM visits WHERE visit_id = ?", (match["existing_visit_id"],)).fetchone()
        sources = json.loads(row["source_documents_json"])
        if document_id not in sources:
            sources.append(document_id)
        stored_fields, history = json.loads(row["fields_json"]), json.loads(row["history_json"])
        outcome = "UNCHANGED"
        if match["outcome"] == "EXISTS_DIFFERENT":
            outcome = "KEPT_EXISTING"
            if decision == "UPDATE" or isinstance(decision, dict):
                outcome = "UPDATED"
                selected = set(decision["fields"]) if isinstance(decision, dict) else {d["field"] for d in match["diffs"]}
                changes = [d for d in match["diffs"] if d["field"] in selected]
                history.append({
                    "at": _iso(now), "by": reviewer, "document_id": document_id,
                    "previous": {d["field"]: stored_fields.get(d["field"]) for d in changes},
                    "selected_fields": sorted(selected),
                })
                for diff in changes:
                    stored_fields[diff["field"]] = fields[diff["field"]]
        db.execute(
            "UPDATE visits SET fields_json = ?, history_json = ?, source_documents_json = ?, updated_at = ? "
            "WHERE visit_id = ?",
            (_dumps(stored_fields), _dumps(history), _dumps(sources), _iso(now), row["visit_id"]),
        )
        return {**summary, "visit_id": row["visit_id"], "outcome": outcome}

    def _create_patient(self, db, facility_id: str, keys: dict, reviewer: str, now: datetime) -> str:
        present = {key: value for key, value in keys.items() if value is not None}
        for key, value in present.items():
            holder = db.execute(f"SELECT patient_id FROM patients WHERE facility_id = ? AND {key} = ?",
                                (facility_id, value)).fetchone()
            if holder:
                raise Conflict(
                    "KEY_ALREADY_REGISTERED",
                    f"{schema.FIELDS[key].label} {value} appartient déjà à {holder['patient_id']}. "
                    "Corrigez la clé ou choisissez cette patiente.",
                    {"field": key, "patient_id": holder["patient_id"]},
                )
        patient_id = next_id(db, "PAT")
        db.execute(
            "INSERT INTO patients(patient_id, facility_id, registry_file_number, midwife_patient_code, flags_json, "
            "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (patient_id, facility_id, keys["registry_file_number"], keys["midwife_patient_code"],
             _dumps([] if present else ["NO_LINK_KEY"]), _iso(now), reviewer),
        )
        return patient_id

    def _fill_patient_keys(self, db, patient_id: str, facility_id: str, keys: dict) -> None:
        patient = db.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        for key, value in keys.items():
            if value is None or patient[key] is not None:
                continue
            taken = db.execute(f"SELECT 1 FROM patients WHERE facility_id = ? AND {key} = ?",
                               (facility_id, value)).fetchone()
            if not taken:
                db.execute(f"UPDATE patients SET {key} = ? WHERE patient_id = ?", (value, patient_id))

    # ------------------------------------------------------------------ grouping

    def move_page(self, page_id: str, *, reviewer: str, target_document_id: str | None = None, expected_revision: int | None = None, target_expected_revision: int | None = None) -> dict:
        """Split (no target) or regroup a page; both affected drafts are reset and re-queued."""
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            page = db.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            if page is None:
                raise NotFound("PAGE_NOT_FOUND", "Page introuvable.")
            if page["replaced_by"]:
                raise Conflict("PAGE_REPLACED", f"Cette page a été remplacée par {page['replaced_by']}.")
            if db.execute("SELECT 1 FROM retake_requests WHERE page_id = ? AND status = 'PENDING'", (page_id,)).fetchone():
                raise Conflict("RETAKE_PENDING", "Une reprise est en attente pour cette page : annulez-la avant de la déplacer.")
            source = self._load_document(db, page["document_id"])
            self._check_revision(source, expected_revision)
            if source["status"] in ("REGISTERED", "SYNCED"):
                raise Conflict("ALREADY_REGISTERED", "Ce dossier est déjà enregistré.")
            if target_document_id:
                if target_document_id == source["document_id"]:
                    raise Invalid("SAME_DOCUMENT", "La page est déjà dans ce dossier.")
                target = self._load_document(db, target_document_id)
                self._check_revision(target, target_expected_revision)
                if target["status"] in ("REGISTERED", "SYNCED"):
                    raise Conflict("ALREADY_REGISTERED", "Le dossier cible est déjà enregistré.")
                if target["facility_id"] != source["facility_id"]:
                    raise Forbidden("OTHER_FACILITY", "Le dossier cible appartient à un autre établissement.")
            else:
                if len(self._page_ids(db, source["document_id"])) == 1:
                    raise Invalid("ONLY_PAGE", "Cette page est déjà seule dans son dossier.")
                target_document_id = next_id(db, "DOC")
                db.execute(
                    "INSERT INTO documents(document_id, sender_id, facility_id, status, grouping_status, "
                    "created_at, last_page_at, updated_at) VALUES (?, ?, ?, 'PENDING_AI', 'CONFIRMED', ?, ?, ?)",
                    (target_document_id, page["sender_id"], source["facility_id"], _iso(now), _iso(now), _iso(now)),
                )
                self._event(db, target_document_id, "STATUS_CHANGED", reviewer, now, {"from": None, "to": "PENDING_AI"})

            position = len(self._page_ids(db, target_document_id)) + 1
            # Replaced originals and their retake requests travel with the active page so history stays attached.
            chain, frontier = [page_id], [page_id]
            while frontier:
                marks = ",".join("?" * len(frontier))
                frontier = [row[0] for row in db.execute(
                    f"SELECT page_id FROM pages WHERE replaced_by IN ({marks})", frontier)]
                chain += frontier
            marks = ",".join("?" * len(chain))
            db.execute(f"UPDATE pages SET document_id = ?, position = ? WHERE page_id IN ({marks})",
                       (target_document_id, position, *chain))
            db.execute(f"UPDATE retake_requests SET document_id = ? WHERE page_id IN ({marks})",
                       (target_document_id, *chain))
            for document_id in (source["document_id"], target_document_id):
                self._reset_after_regroup(db, document_id, page_id, source["document_id"], target_document_id,
                                          reviewer, now)
        return {"page_id": page_id, "from_document_id": source["document_id"], "to_document_id": target_document_id}

    def _reset_after_regroup(self, db, document_id, page_id, from_id, to_id, reviewer, now) -> None:
        old_positions = [row[0] for row in db.execute(
            "SELECT position FROM pages WHERE document_id = ? AND replaced_by IS NULL ORDER BY position", (document_id,))]
        detail = {"page_id": page_id, "from": from_id, "to": to_id}
        if not old_positions:
            db.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            self._event(db, document_id, "DOCUMENT_EMPTIED", reviewer, now, detail)
            return
        # Ascending order never collides: unprocessed positions are always above the new one.
        for position, old in enumerate(old_positions, start=1):
            db.execute("UPDATE pages SET position = ? WHERE document_id = ? AND position = ?",
                       (position, document_id, old))
        detail.update(self._discard_review(db, document_id, now))
        db.execute("UPDATE documents SET grouping_status = 'CONFIRMED' WHERE document_id = ?", (document_id,))
        self._event(db, document_id, "PAGES_REGROUPED", reviewer, now, detail)
        self._set_status(db, document_id, "PENDING_AI", now, actor=reviewer)

    def _discard_review(self, db, document_id: str, now: datetime) -> dict:
        """Drop a stale draft and patient selection; the caller re-queues extraction."""
        document = self._load_document(db, document_id)
        draft = _loads(document["draft_json"])
        discarded = {
            "discarded_reviews": sum(
                1 for *_, fv in [*schema.iter_fields(draft), *extended.iter_fields(draft)]
                if fv["verification"]["state"] != "UNVERIFIED"
            ) if draft else 0,
            "selection_discarded": document["selection_json"] is not None,
        }
        db.execute(
            "UPDATE documents SET draft_json = NULL, selection_json = NULL, failure_reason = NULL, "
            "revision = revision + 1, updated_at = ? WHERE document_id = ?",
            (_iso(now), document_id),
        )
        return discarded

    # ------------------------------------------------------------------ reads

    def list_documents(self) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute(
                "SELECT d.*, s.label AS sender_label, "
                "(SELECT COUNT(*) FROM pages p WHERE p.document_id = d.document_id AND p.replaced_by IS NULL) "
                "AS page_count, "
                "(SELECT COUNT(*) FROM retake_requests r WHERE r.document_id = d.document_id AND r.status = 'PENDING') "
                "AS pending_retakes "
                "FROM documents d JOIN senders s USING (sender_id) ORDER BY d.document_id DESC"
            ).fetchall()
        items = []
        for row in rows:
            draft = _loads(row["draft_json"])
            registration = _loads(row["registration_json"])
            items.append({
                "document_id": row["document_id"], "status": row["status"],
                "grouping_status": row["grouping_status"], "page_count": row["page_count"],
                "pending_retakes": row["pending_retakes"],
                "sender_label": row["sender_label"], "updated_at": row["updated_at"],
                "blocking_count": len(schema.blocking_fields(draft)) if draft else 0,
                "encounter_count": len(draft["encounters"]) if draft else 0,
                "patient_id": registration["patient_id"] if registration else None,
                "extracting": self.extraction_progress(row["document_id"]) is not None,
                "retrying": row["status"] == "PENDING_AI" and self.extraction_retry(row["document_id"]) is not None,
            })
        return items

    def get_document(self, document_id: str) -> dict:
        with self.store.read() as db:
            document = self._load_document(db, document_id)
            retakes = self._retakes(db, "r.document_id = ?", (document_id,))
            pending_by_page = {r["page_id"]: r["request_id"] for r in retakes if r["status"] == "PENDING"}
            pages = [{**dict(row), "pending_retake_id": pending_by_page.get(row["page_id"])} for row in db.execute(
                "SELECT page_id, position, media_ref, received_at, source_message_id, section_hint FROM pages "
                "WHERE document_id = ? AND replaced_by IS NULL ORDER BY position", (document_id,))]
            failure = self._failure_detail(db, document)
            draft = _loads(document["draft_json"])
            selection = _loads(document["selection_json"])
            review = None
            if draft is not None:
                blocking = schema.blocking_fields(draft)
                candidates = linking.find_candidates(db, document["facility_id"], draft)
                matched_patient = selection["patient_id"] if selection and selection["choice"] == "EXISTING" else None
                matches = linking.encounter_matches(db, matched_patient, draft)
                review = {
                    "blocking": blocking,
                    "candidates": candidates,
                    "suggested_patient_id": linking.suggested_patient(candidates, draft),
                    "selection": selection,
                    "selection_warnings": linking.selection_warnings(selection, candidates),
                    "encounter_matches": matches,
                    "next_step": self._next_step(document["status"], blocking, selection, matches,
                                                 bool(pending_by_page)),
                }
            events = [
                {"type": row["type"], "actor": row["actor"], "at": row["at"], "detail": json.loads(row["detail_json"])}
                for row in db.execute("SELECT * FROM events WHERE document_id = ? ORDER BY id", (document_id,))
            ]
            move_targets = [row["document_id"] for row in db.execute(
                "SELECT document_id FROM documents WHERE facility_id = ? AND document_id != ? AND status != 'REGISTERED' "
                "ORDER BY document_id DESC", (document["facility_id"], document_id))]
        return {
            "document": {
                **{key: document[key] for key in (
                    "document_id", "status", "grouping_status", "sender_id", "facility_id",
                    "created_at", "last_page_at", "updated_at", "revision", "failure_reason",
                )},
                "failure_message": failure.get("message"),
                "failure_pages": failure.get("pages", []),
                "extraction_progress": self.extraction_progress(document_id),
                "extraction_retry": self.extraction_retry(document_id) if document["status"] == "PENDING_AI" else None,
            },
            "next_step": review["next_step"] if review else self._next_step(document["status"], [], None, [],
                                                                             bool(pending_by_page)),
            "pages": pages,
            "retakes": retakes,
            "draft": draft,
            "review": review,
            "registration": _loads(document["registration_json"]),
            "events": events,
            "move_targets": move_targets,
        }

    @staticmethod
    def _next_step(status: str, blocking: list, selection: dict | None, matches: list,
                   retake_pending: bool = False) -> str:
        if status == "CAPTURED":
            return "COLLECTING"
        if retake_pending and status != "REGISTERED":
            return "WAITING_RETAKE"
        if status == "PENDING_AI":
            return "WAITING_AI"
        if status == "PROCESSING_FAILED":
            return "FAILED"
        if status in ("REGISTERED", "SYNCED"):
            return "DONE"
        if blocking:
            return "FIELD"
        if selection is None or selection["choice"] == "UNSURE":
            return "PATIENT"
        if any(m["outcome"] == "EXISTS_DIFFERENT" for m in matches):
            return "EXISTING_VISITS"
        return "CONFIRM"

    def list_patients(self) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute(
                "SELECT p.patient_id, p.facility_id, p.registry_file_number, p.midwife_patient_code, p.flags_json, "
                "COUNT(v.visit_id) AS visit_count, MAX(v.visit_date) AS last_visit_date "
                "FROM patients p LEFT JOIN visits v USING (patient_id) GROUP BY p.patient_id ORDER BY p.patient_id"
            ).fetchall()
        return [{**dict(row), "flags": json.loads(row["flags_json"])} for row in rows]

    def patient_timeline(self, patient_id: str) -> dict:
        with self.store.read() as db:
            patient = db.execute(
                "SELECT p.*, f.name AS facility_name FROM patients p JOIN facilities f USING (facility_id) "
                "WHERE patient_id = ?", (patient_id,)).fetchone()
            if patient is None:
                raise NotFound("PATIENT_NOT_FOUND", "Patiente introuvable.")
            visits = [
                {
                    "visit_id": row["visit_id"], "visit_date": row["visit_date"], "slot": row["slot"],
                    "encounter_type": row["encounter_type"],
                    "fields": {name: {"value": fv["value"], "field_status": fv["field_status"],
                                      "verification": fv["verification"]["state"]}
                               for name, fv in json.loads(row["fields_json"]).items()},
                    "source_documents": json.loads(row["source_documents_json"]),
                    "revisions": len(json.loads(row["history_json"])),
                    "history": json.loads(row["history_json"]),
                    "field_details": json.loads(row["fields_json"]),
                    "updated_at": row["updated_at"],
                }
                for row in db.execute("SELECT * FROM visits WHERE patient_id = ? ORDER BY visit_date", (patient_id,))
            ]
        return {
            "patient": {
                "patient_id": patient["patient_id"], "facility_id": patient["facility_id"],
                "facility_name": patient["facility_name"],
                "registry_file_number": patient["registry_file_number"],
                "midwife_patient_code": patient["midwife_patient_code"],
                "flags": json.loads(patient["flags_json"]),
            },
            "visits": visits,
        }

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _reviewer(reviewer: str | None) -> str:
        reviewer = (reviewer or "").strip()
        if not re.fullmatch(r"[\w.@ \-]{2,60}", reviewer):
            raise Invalid("REVIEWER_REQUIRED", "Indiquez l'identifiant de l'agent back-office.")
        return reviewer

    @staticmethod
    def _load_document(db, document_id: str):
        document = db.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
        if document is None:
            raise NotFound("DOCUMENT_NOT_FOUND", "Dossier introuvable.")
        return document

    def _load_reviewable(self, db, document_id: str, expected_revision: int | None):
        document = self._load_document(db, document_id)
        if document["status"] not in REVIEWABLE:
            raise Conflict("NOT_REVIEWABLE", "Ce dossier ne peut pas être modifié dans son état actuel.",
                           {"status": document["status"]})
        self._check_revision(document, expected_revision)
        return document

    @staticmethod
    def _check_revision(document, expected_revision: int | None) -> None:
        if expected_revision is not None and int(expected_revision) != document["revision"]:
            raise Conflict("STALE_REVISION", "Le dossier a été modifié entre-temps : rechargez-le.",
                           {"revision": document["revision"]})

    @staticmethod
    def _page_refs(db, document_id: str) -> list[str]:
        return [row["media_ref"] for row in db.execute(
            "SELECT media_ref FROM pages WHERE document_id = ? AND replaced_by IS NULL ORDER BY position", (document_id,))]

    @staticmethod
    def _page_state(db, document_id: str) -> list[tuple[str, str, str | None]]:
        """(page_id, media_ref, section_hint) of the active pages: what an extraction result was computed from."""
        return [tuple(row) for row in db.execute(
            "SELECT page_id, media_ref, section_hint FROM pages WHERE document_id = ? AND replaced_by IS NULL "
            "ORDER BY position", (document_id,))]

    @staticmethod
    def _page_ids(db, document_id: str) -> list[str]:
        return [row["page_id"] for row in db.execute(
            "SELECT page_id FROM pages WHERE document_id = ? AND replaced_by IS NULL ORDER BY position", (document_id,))]

    @staticmethod
    def _save_draft(db, document_id: str, draft: dict, now: datetime) -> None:
        db.execute("UPDATE documents SET draft_json = ?, revision = revision + 1, updated_at = ? WHERE document_id = ?",
                   (_dumps(draft), _iso(now), document_id))

    def _recompute_status(self, db, document_id: str, now: datetime) -> None:
        document = self._load_document(db, document_id)
        if document["status"] not in REVIEWABLE:
            return
        draft, selection = json.loads(document["draft_json"]), _loads(document["selection_json"])
        if selection and selection["choice"] == "UNSURE":
            status = "DUPLICATE_SUSPECTED"
        elif schema.blocking_fields(draft):
            status = "NEEDS_REVIEW"
        elif selection:
            status = "PATIENT_MATCHED"
        else:
            status = "VALIDATED"
        self._set_status(db, document_id, status, now)

    def _set_status(self, db, document_id: str, status: str, now: datetime, *, actor: str = "system",
                    detail: dict | None = None) -> None:
        current = db.execute("SELECT status FROM documents WHERE document_id = ?", (document_id,)).fetchone()["status"]
        if current == status:
            return
        db.execute("UPDATE documents SET status = ?, updated_at = ? WHERE document_id = ?",
                   (status, _iso(now), document_id))
        self._event(db, document_id, "STATUS_CHANGED", actor, now, {"from": current, "to": status, **(detail or {})})

    @staticmethod
    def _event(db, document_id: str | None, event_type: str, actor: str, now: datetime, detail: dict) -> None:
        db.execute("INSERT INTO events(document_id, type, actor, at, detail_json) VALUES (?, ?, ?, ?, ?)",
                   (document_id, event_type, actor, _iso(now), _dumps(detail)))

    # ------------------------------------------------------------------ sync, conversational & export

    def sync_document(self, document_id: str, *, reviewer: str = "system",
                      central_sink: Callable[[dict], dict] | None = None) -> dict:
        """Synchronize a registered document and its visits with the central registry (simulated).
        Transitions status from REGISTERED to SYNCED. Idempotent on retry."""
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        target_name = "Registre National des Dossiers Maternels (Simulé)"
        sync_error = None
        with self.store.tx() as db:
            document = self._load_document(db, document_id)
            if document["status"] == "SYNCED":
                row = db.execute("SELECT ack_id, ack_signature, synced_at FROM central_sync_log WHERE document_id = ?",
                                 (document_id,)).fetchone()
                ack = {
                    "ack_id": row["ack_id"] if row else "REPLAYED",
                    "status": "ACKNOWLEDGED_SIMULATED",
                    "ack_signature": row["ack_signature"] if row else "",
                    "synced_at": row["synced_at"] if row else document["updated_at"],
                }
                return {
                    "document_id": document_id, "status": "SYNCED", "synced": True, "replayed": True,
                    "target": target_name, "ack": ack,
                }
            if document["status"] != "REGISTERED":
                raise Conflict("NOT_REGISTERED", "Seuls les dossiers enregistrés peuvent être synchronisés.",
                               {"status": document["status"]})
            registration = json.loads(document["registration_json"]) if document["registration_json"] else {}
            patient_id = registration.get("patient_id")
            visits = [
                dict(row) for row in db.execute(
                    "SELECT visit_id, visit_date, slot, encounter_type, fields_json FROM visits WHERE patient_id = ?",
                    (patient_id,)
                ).fetchall()
            ] if patient_id else []
            sync_payload = {
                "document_id": document_id,
                "facility_id": document["facility_id"],
                "patient_id": patient_id,
                "visits": visits,
                "synced_at": _iso(now),
            }

            ack_id = f"ACK-REG-SIM-{uuid.uuid4().hex[:12].upper()}"
            sig_payload = f"{document_id}:{patient_id}:{_iso(now)}".encode("utf-8")
            ack_signature = hmac.new(b"simulated-central-registry-secret", sig_payload, hashlib.sha256).hexdigest()
            ack_data = {
                "ack_id": ack_id,
                "status": "ACKNOWLEDGED_SIMULATED",
                "target": target_name,
                "ack_signature": ack_signature,
                "synced_at": _iso(now),
            }

            if central_sink is not None:
                try:
                    sink_res = central_sink(sync_payload)
                    if (not isinstance(sink_res, dict)
                            or sink_res.get("status") not in ("ACKNOWLEDGED", "ACKNOWLEDGED_SIMULATED")
                            or not isinstance(sink_res.get("ack_id"), str)
                            or not sink_res["ack_id"].strip()):
                        raise Invalid("SYNC_ACK_REQUIRED", "Le registre doit confirmer la réception avant la synchronisation.")
                    ack_data["ack_id"] = sink_res["ack_id"]
                except Exception as exc:
                    sync_error = exc
                    self._event(db, document_id, "SYNC_FAILED", reviewer, now,
                                {"error_type": type(exc).__name__, "patient_id": patient_id})

            if sync_error is None:
                sync_id = next_id(db, "SYNC")
                db.execute(
                    "INSERT OR REPLACE INTO central_sync_log(sync_id, document_id, patient_id, ack_id, target, status, payload_json, ack_signature, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sync_id, document_id, patient_id, ack_data["ack_id"], target_name, "ACKNOWLEDGED_SIMULATED",
                     _dumps(sync_payload), ack_signature, _iso(now)),
                )
                self._set_status(db, document_id, "SYNCED", now, actor=reviewer)
                self._event(db, document_id, "DOCUMENT_SYNCED", reviewer, now,
                            {"patient_id": patient_id, "visits_count": len(visits), "ack_id": ack_data["ack_id"], "target": target_name})
        if sync_error is not None:
            raise sync_error
        return {
            "document_id": document_id, "status": "SYNCED", "synced": True, "replayed": False,
            "target": target_name, "ack": ack_data,
        }

    def conversational_prompt(self, document_id: str) -> dict:
        """Returns structured prompt, question text in French, and available actions for a conversational midwife client."""
        doc = self.get_document(document_id)
        d = doc["document"]
        status = d["status"]
        step = doc.get("next_step")
        review = doc.get("review") or {}
        blocking = review.get("blocking", [])
        matches = review.get("matches", []) or review.get("encounter_matches", [])
        visits = review.get("visits", []) or matches

        if step == "WAITING_AI":
            return {
                "step": "WAITING_AI",
                "question": "Votre photo est en cours d'analyse par l'IA. Merci de patienter quelques instants.",
                "actions": ["MANUAL_ENTRY"],
                "document_id": document_id,
                "expected_revision": d["revision"],
            }
        if step == "WAITING_RETAKE":
            return {
                "step": "WAITING_RETAKE",
                "question": "Une reprise de photo est en attente. Merci d'envoyer la nouvelle photo.",
                "actions": [],
                "document_id": document_id,
            }
        if step == "FAILED":
            return {
                "step": "FAILED",
                "question": "La lecture de la fiche a échoué. Souhaitez-vous démarrer la saisie manuelle ou reprendre la photo ?",
                "actions": ["MANUAL_ENTRY", "RETAKE"],
                "document_id": document_id,
                "expected_revision": d["revision"],
            }
        if step == "DONE":
            return {
                "step": "DONE",
                "question": f"Le dossier est {'synchronisé avec le registre central (simulé)' if status == 'SYNCED' else 'enregistré avec succès'}.",
                "actions": ["SYNC"] if status == "REGISTERED" else [],
                "document_id": document_id,
            }
        if step == "FIELD":
            first = blocking[0]
            if first.get("scope") == "page":
                p_idx = first.get("page_index", 0) + 1
                reason = first.get("reason", "PHOTO_UNUSABLE")
                reason_desc = {
                    "UNKNOWN_LAYOUT": "Mise en page non reconnue",
                    "SECTION_UNCERTAIN": "Section reconnue avec un doute",
                    "GRID_NOT_FOUND": "Tableau des visites introuvable",
                    "GRID_COLUMN_UNREAD": "Colonne écrite non lue",
                    "PHOTO_UNUSABLE": "Photo inutilisable",
                    "OCR_TIMEOUT": "Lecture trop longue",
                    "OCR_FAILED": "Lecture impossible",
                }.get(reason, reason)
                q = f"La page {p_idx} nécessite une vérification ({reason_desc}). Souhaitez-vous demander une reprise de photo, confirmer sa section, ou saisir à la main ?"
                return {
                    "step": "FIELD",
                    "field": first,
                    "question": q,
                    "actions": ["RETAKE", "SECTION", "MANUAL_ENTRY"],
                    "document_id": document_id,
                    "expected_revision": d["revision"],
                }

            field_name = first.get("field")
            spec = schema.FIELDS.get(field_name) or extended.FIELDS.get(field_name)
            label = spec.label if spec else field_name
            val = first.get("value")
            reason = first.get("reason")
            if reason == "REQUIRED_MISSING":
                q = f"Pour le champ obligatoire {label}, aucune valeur n'a été lue. Merci de renseigner la valeur ou de demander une reprise de photo."
                actions = ["CORRECT", "RETAKE"]
            elif reason == "DUPLICATE_ENCOUNTER_DATE":
                q = f"Deux visites ont la même date ({val}). Merci de corriger la date d'une des visites."
                actions = ["CORRECT"]
            else:
                val_str = f"« {val} »" if val is not None else "illisible ou absent"
                q = f"Question de vérification : pour le champ {label}, la valeur lue est {val_str}. Souhaitez-vous confirmer cette valeur, la corriger, ou la marquer illisible ?"
                actions = ["CONFIRM", "CORRECT", "ILLEGIBLE", "NOT_PROVIDED"]
            return {
                "step": "FIELD",
                "field": first,
                "question": q,
                "actions": actions,
                "document_id": document_id,
                "expected_revision": d["revision"],
            }
        if step == "PATIENT":
            suggested = next((m for m in matches if m.get("suggested")), None) or review.get("suggested_patient_id")
            if suggested:
                pat_id = suggested if isinstance(suggested, str) else suggested.get("patient_id")
                q = f"Patiente suggérée : {pat_id} correspond aux clés du registre. Confirmez-vous cette patiente, est-ce une nouvelle patiente, ou êtes-vous incertaine ?"
                candidate_id = pat_id
            else:
                q = "Aucune patiente ne correspond aux clés. Souhaitez-vous créer une nouvelle patiente ou choisir parmi les patientes existantes ?"
                candidate_id = None
            return {
                "step": "PATIENT",
                "question": q,
                "suggested_patient_id": candidate_id,
                "candidates": review.get("candidates", []),
                "actions": ["CHOOSE", "NEW", "UNSURE"],
                "document_id": document_id,
                "expected_revision": d["revision"],
            }
        if step == "EXISTING_VISITS":
            return {
                "step": "EXISTING_VISITS",
                "question": "Certaines visites existent déjà avec des valeurs différentes. Souhaitez-vous mettre à jour les valeurs ou conserver l'existante ?",
                "actions": ["UPDATE", "KEEP"],
                "visits": visits,
                "document_id": document_id,
                "expected_revision": d["revision"],
            }
        return {
            "step": "CONFIRM",
            "question": f"Toutes les vérifications sont terminées. Confirmer et enregistrer les visites ?",
            "visits": visits,
            "actions": ["CONFIRM"],
            "document_id": document_id,
            "expected_revision": d["revision"],
        }

    def conversational_reply(self, document_id: str, *, reviewer: str = "midwife", action: str, **kwargs) -> dict:
        """Processes a midwife's conversational response and returns the updated conversational state."""
        prompt = self.conversational_prompt(document_id)
        step = prompt.get("step")
        rev = kwargs.get("expected_revision")
        if prompt.get("expected_revision") is not None:
            if type(rev) is not int:
                raise Invalid("REVISION_REQUIRED", "La révision du dossier affiché est requise.")
            if rev != prompt["expected_revision"]:
                raise Conflict("STALE_REVISION", "Le dossier a été modifié entre-temps : rechargez-le.",
                               {"revision": prompt["expected_revision"]})

        if step == "FIELD":
            field_info = prompt["field"]
            act = action.upper()
            if field_info.get("scope") == "page":
                p_idx = field_info.get("page_index", 0)
                pages = self.get_document(document_id)["pages"]
                page_id = pages[p_idx]["page_id"] if p_idx < len(pages) else None
                if act == "RETAKE":
                    if page_id:
                        self.request_retake(page_id, reviewer=reviewer)
                elif act == "SECTION":
                    sec = kwargs.get("section")
                    if page_id:
                        self.set_page_section(page_id, reviewer=reviewer, section=sec, expected_revision=rev)
                elif act == "MANUAL_ENTRY":
                    self.start_manual_entry(document_id, reviewer=reviewer, expected_revision=rev)
                else:
                    raise Invalid("UNKNOWN_ACTION", f"Action {action} non reconnue pour l'étape PAGE.")
            else:
                if act == "CONFIRM":
                    self.review_field(document_id, reviewer=reviewer, scope=field_info.get("scope"),
                                      field=field_info.get("field"), encounter_index=field_info.get("encounter_index"),
                                      section=field_info.get("section"), item_index=field_info.get("item_index"),
                                      action="CONFIRM", expected_revision=rev)
                elif act == "CORRECT":
                    val = kwargs.get("value")
                    self.review_field(document_id, reviewer=reviewer, scope=field_info.get("scope"),
                                      field=field_info.get("field"), encounter_index=field_info.get("encounter_index"),
                                      section=field_info.get("section"), item_index=field_info.get("item_index"),
                                      action="CORRECT", value=val, expected_revision=rev)
                elif act in ("ILLEGIBLE", "NOT_PROVIDED"):
                    self.review_field(document_id, reviewer=reviewer, scope=field_info.get("scope"),
                                      field=field_info.get("field"), encounter_index=field_info.get("encounter_index"),
                                      section=field_info.get("section"), item_index=field_info.get("item_index"),
                                      action="SET_STATUS", field_status=act, expected_revision=rev)
                elif act == "RETAKE":
                    pages = self.get_document(document_id)["pages"]
                    if pages:
                        self.request_retake(pages[0]["page_id"], reviewer=reviewer)
                else:
                    raise Invalid("UNKNOWN_ACTION", f"Action {action} non reconnue pour l'étape FIELD.")

        elif step == "PATIENT":
            act = action.upper()
            if act in ("CHOOSE", "PATIENT", "EXISTING"):
                pat_id = kwargs.get("patient_id") or prompt.get("suggested_patient_id")
                if not pat_id:
                    raise Invalid("PATIENT_ID_REQUIRED", "Identifiant de la patiente requis.")
                self.select_patient(document_id, reviewer=reviewer, choice="EXISTING", patient_id=pat_id, expected_revision=rev)
            elif act == "NEW":
                self.select_patient(document_id, reviewer=reviewer, choice="NEW", expected_revision=rev)
            elif act == "UNSURE":
                self.select_patient(document_id, reviewer=reviewer, choice="UNSURE", expected_revision=rev)
            else:
                raise Invalid("UNKNOWN_ACTION", f"Action {action} non reconnue pour l'étape PATIENT.")

        elif step == "EXISTING_VISITS":
            if action.upper() != "CONFIRM":
                raise Invalid("VISIT_REVIEW_REQUIRED", "Choisissez les valeurs dans le récapitulatif, puis confirmez l'enregistrement.")
            decisions = kwargs.get("existing_visit_decisions")
            self.confirm(document_id, reviewer=reviewer, existing_visit_decisions=decisions, expected_revision=rev)

        elif step == "CONFIRM":
            if action.upper() == "CONFIRM":
                self.confirm(document_id, reviewer=reviewer, existing_visit_decisions=kwargs.get("existing_visit_decisions"), expected_revision=rev)
            else:
                raise Invalid("UNKNOWN_ACTION", f"Action {action} non reconnue pour l'étape CONFIRM.")

        elif step in ("FAILED", "WAITING_AI"):
            if action.upper() == "MANUAL_ENTRY":
                self.start_manual_entry(document_id, reviewer=reviewer, expected_revision=rev)
            elif action.upper() == "RETAKE":
                pages = self.get_document(document_id)["pages"]
                if pages:
                    self.request_retake(pages[0]["page_id"], reviewer=reviewer)
            else:
                raise Invalid("UNKNOWN_ACTION", f"Action {action} non reconnue pour l'étape {step}.")

        elif step == "DONE" and action.upper() == "SYNC":
            self.sync_document(document_id, reviewer=reviewer)

        return self.conversational_prompt(document_id)

    def export_patient(self, patient_id: str, *, assumptions: dict | None = None) -> dict[str, object]:
        """Produces a dictionary matching the 31 columns of maternal_registry_synthetic.csv for one patient."""
        with self.store.read() as db:
            patient = db.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
            if patient is None:
                raise NotFound("PATIENT_NOT_FOUND", "Patiente introuvable.")
            visit_rows = db.execute("SELECT * FROM visits WHERE patient_id = ? ORDER BY visit_date ASC, visit_id ASC", (patient_id,)).fetchall()
            visits = [
                {
                    "visit_id": row["visit_id"], "visit_date": row["visit_date"], "slot": row["slot"],
                    "encounter_type": row["encounter_type"], "fields": json.loads(row["fields_json"]),
                    "sources": json.loads(row["source_documents_json"]),
                }
                for row in visit_rows
            ]
            source_doc_ids = set()
            for v in visits:
                source_doc_ids.update(v.get("sources", []))
            # Deterministic selection: sorted by document_id DESC
            sorted_source_ids = sorted(source_doc_ids, reverse=True)
            extended_draft = None
            for s_id in sorted_source_ids:
                s_doc = db.execute("SELECT draft_json FROM documents WHERE document_id = ?", (s_id,)).fetchone()
                if s_doc and s_doc["draft_json"]:
                    d_json = json.loads(s_doc["draft_json"])
                    if d_json.get("extended"):
                        extended_draft = d_json["extended"]
                        break
        return export_columns.compute_export_row(dict(patient), visits, extended_draft, assumptions=assumptions)

    def export_all_patients(self, *, assumptions: dict | None = None) -> list[dict[str, object]]:
        """Produces 31-column rows for every registered patient."""
        with self.store.read() as db:
            patient_ids = [row["patient_id"] for row in db.execute("SELECT patient_id FROM patients ORDER BY patient_id").fetchall()]
        return [self.export_patient(pid, assumptions=assumptions) for pid in patient_ids]

    def export_csv(self, *, assumptions: dict | None = None) -> str:
        """Exports all patients to RFC-4180 CSV matching data/maternal_registry_synthetic.csv."""
        rows = self.export_all_patients(assumptions=assumptions)
        return export_columns.export_to_csv(rows)
