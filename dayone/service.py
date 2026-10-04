"""Domain operations: ingest, grouping, extraction queue, review, registration and timeline."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import linking, schema
from .extraction import ExtractionError
from .store import Store, next_id

log = logging.getLogger("dayone.service")

REVIEWABLE = {"AI_PROCESSED", "NEEDS_REVIEW", "VALIDATED", "PATIENT_MATCHED", "DUPLICATE_SUSPECTED"}
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png"}

DEMO_FACILITY = {"facility_id": "FAC-SIDI-SMAIL", "name": "C/S Sidi Smail"}
DEMO_SENDER = {"sender_id": "whatsapp:+212600000001", "label": "Sage-femme – C/S Sidi Smail"}
DEMO_HISTORY_PAGES = ("data/Paper Registry/dossiers_specimen_10_patientes-01.png",
                      "data/Paper Registry/dossiers_specimen_10_patientes-03.png")


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


class DayOneService:
    def __init__(self, store: Store, extractor, repo_root: Path, *, grouping_window_seconds: int = 8, clock=utcnow):
        self.store = store
        self.extractor = extractor
        self.repo_root = Path(repo_root).resolve()
        self.media_dir = (self.repo_root / "data" / "Paper Registry").resolve()
        self.window = timedelta(seconds=grouping_window_seconds)
        self.clock = clock

    # ------------------------------------------------------------------ setup

    def ensure_seed(self) -> None:
        with self.store.read() as db:
            empty = db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0] == 0
        if empty:
            self.reset_demo()

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
        self.process_document(document_id)
        self.select_patient(document_id, reviewer="seed", choice="NEW")
        self.confirm(document_id, reviewer="seed")

    # ------------------------------------------------------------------ system

    def system_info(self) -> dict:
        with self.store.read() as db:
            ai_available = self._ai_available(db)
        return {
            "ai_available": ai_available,
            "grouping_window_seconds": int(self.window.total_seconds()),
            "extractor": getattr(self.extractor, "name", type(self.extractor).__name__),
            "catalog": schema.catalog(),
        }

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
            specimen = re.match(r"dossiers_specimen_10_patientes-(\d+)", path.name)
            return (0, int(specimen.group(1)), path.name) if specimen else (1, 0, path.name)

        files = [p for p in self.media_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
        return [p.relative_to(self.repo_root).as_posix() for p in sorted(files, key=sort_key)]

    def resolve_media(self, media_ref: str) -> Path:
        if not isinstance(media_ref, str) or not media_ref:
            raise Invalid("MEDIA_REQUIRED", "Référence d'image manquante.")
        path = (self.repo_root / media_ref).resolve()
        if path.parent != self.media_dir or not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            raise NotFound("MEDIA_NOT_FOUND", "Image introuvable.")
        return path

    # ------------------------------------------------------------------ ingest

    def ingest_photo(self, *, sender_id: str, message_id: str, media_ref: str) -> dict:
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 200:
            raise Invalid("MESSAGE_ID_REQUIRED", "Identifiant de message WhatsApp manquant.")
        digest = hashlib.sha256(self.resolve_media(media_ref).read_bytes()).hexdigest()
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

            document = self._open_document(db, sender_id, now)
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

            position = db.execute("SELECT COUNT(*) FROM pages WHERE document_id = ?", (document_id,)).fetchone()[0] + 1
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

    def _open_document(self, db, sender_id: str, now: datetime):
        row = db.execute(
            "SELECT * FROM documents WHERE sender_id = ? AND status = 'CAPTURED' ORDER BY document_id DESC LIMIT 1",
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
                "SELECT id, direction, body, media_ref, created_at FROM messages WHERE sender_id = ? ORDER BY id",
                (sender_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ queue

    def close_capture(self, document_id: str) -> None:
        with self.store.tx() as db:
            document = self._load_document(db, document_id)
            if document["status"] == "CAPTURED":
                self._set_status(db, document_id, "PENDING_AI", self.clock())

    def tick(self) -> list[str]:
        """Close expired grouping windows, then extract queued documents if AI is available."""
        now = self.clock()
        with self.store.tx() as db:
            for row in db.execute("SELECT document_id, last_page_at FROM documents WHERE status = 'CAPTURED'").fetchall():
                if now - datetime.fromisoformat(row["last_page_at"]) > self.window:
                    self._set_status(db, row["document_id"], "PENDING_AI", now)
            if not self._ai_available(db):
                return []
            pending = [row["document_id"] for row in db.execute(
                "SELECT document_id FROM documents WHERE status = 'PENDING_AI' ORDER BY document_id")]
        return [document_id for document_id in pending if self.process_document(document_id)]

    def process_document(self, document_id: str) -> bool:
        with self.store.read() as db:
            refs = self._page_refs(db, document_id)
        draft, failure = None, None
        try:
            draft = self.extractor.extract(refs)
            problems = schema.validate_draft(draft)
            if problems:
                draft, failure = None, ("INVALID_EXTRACTION", problems[0])
        except ExtractionError as exc:
            failure = (exc.code, exc.message)
        except Exception:
            log.exception("extraction error for %s; will retry", document_id)
            return False

        now = self.clock()
        with self.store.tx() as db:
            document = db.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
            if document is None or document["status"] != "PENDING_AI" or self._page_refs(db, document_id) != refs:
                return False
            if draft is None:
                db.execute("UPDATE documents SET failure_reason = ? WHERE document_id = ?", (failure[0], document_id))
                self._set_status(db, document_id, "PROCESSING_FAILED", now,
                                 detail={"code": failure[0], "message": failure[1]})
                return True
            draft["extraction"]["processed_at"] = _iso(now)
            self._save_draft(db, document_id, draft, now)
            db.execute("UPDATE documents SET failure_reason = NULL WHERE document_id = ?", (document_id,))
            self._set_status(db, document_id, "AI_PROCESSED", now)
            self._recompute_status(db, document_id, now)
        return True

    def start_manual_entry(self, document_id: str, *, reviewer: str) -> dict:
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            document = self._load_document(db, document_id)
            if document["status"] != "PROCESSING_FAILED":
                raise Conflict("NOT_FAILED", "La saisie manuelle est réservée aux dossiers en échec d'extraction.")
            self._save_draft(db, document_id, schema.manual_draft(self._page_refs(db, document_id), _iso(now)), now)
            self._event(db, document_id, "MANUAL_ENTRY_STARTED", reviewer, now, {})
            self._set_status(db, document_id, "NEEDS_REVIEW", now, actor=reviewer)
        return self.get_document(document_id)

    # ------------------------------------------------------------------ review

    def review_field(self, document_id: str, *, reviewer: str, scope: str, field: str, action: str,
                     encounter_index: int | None = None, value: object = None, field_status: str | None = None,
                     expected_revision: int | None = None) -> dict:
        reviewer = self._reviewer(reviewer)
        spec = schema.FIELDS.get(field)
        if spec is None or spec.scope != scope:
            raise Invalid("UNKNOWN_FIELD", "Champ inconnu pour cette section.")
        now = self.clock()
        with self.store.tx() as db:
            document = self._load_reviewable(db, document_id, expected_revision)
            draft = json.loads(document["draft_json"])
            try:
                fv = schema.get_field(draft, scope, field, encounter_index)
            except KeyError:
                raise Invalid("UNKNOWN_ENCOUNTER", "Visite inconnue dans ce dossier.") from None
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
            slot = draft["encounters"][encounter_index]["slot"] if scope == "encounter" else None
            self._event(db, document_id, "FIELD_REVIEWED", reviewer, now, {
                "scope": scope, "encounter_index": encounter_index, "slot": slot, "field": field,
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
            if document["status"] == "REGISTERED":
                return {**json.loads(document["registration_json"]), "replayed": True}
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
                         and decisions.get(str(m["encounter_index"])) not in ("UPDATE", "KEEP")]
            if undecided:
                raise Conflict("DECISION_REQUIRED", "Certaines visites existent déjà avec des valeurs différentes.",
                               {"encounters": undecided})

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
            if decision == "UPDATE":
                outcome = "UPDATED"
                history.append({
                    "at": _iso(now), "by": reviewer, "document_id": document_id,
                    "previous": {d["field"]: stored_fields.get(d["field"]) for d in match["diffs"]},
                })
                for diff in match["diffs"]:
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

    def move_page(self, page_id: str, *, reviewer: str, target_document_id: str | None = None) -> dict:
        """Split (no target) or regroup a page; both affected drafts are reset and re-queued."""
        reviewer = self._reviewer(reviewer)
        now = self.clock()
        with self.store.tx() as db:
            page = db.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,)).fetchone()
            if page is None:
                raise NotFound("PAGE_NOT_FOUND", "Page introuvable.")
            source = self._load_document(db, page["document_id"])
            if source["status"] == "REGISTERED":
                raise Conflict("ALREADY_REGISTERED", "Ce dossier est déjà enregistré.")
            if target_document_id:
                if target_document_id == source["document_id"]:
                    raise Invalid("SAME_DOCUMENT", "La page est déjà dans ce dossier.")
                target = self._load_document(db, target_document_id)
                if target["status"] == "REGISTERED":
                    raise Conflict("ALREADY_REGISTERED", "Le dossier cible est déjà enregistré.")
                if target["facility_id"] != source["facility_id"]:
                    raise Forbidden("OTHER_FACILITY", "Le dossier cible appartient à un autre établissement.")
            else:
                if db.execute("SELECT COUNT(*) FROM pages WHERE document_id = ?", (source["document_id"],)).fetchone()[0] == 1:
                    raise Invalid("ONLY_PAGE", "Cette page est déjà seule dans son dossier.")
                target_document_id = next_id(db, "DOC")
                db.execute(
                    "INSERT INTO documents(document_id, sender_id, facility_id, status, grouping_status, "
                    "created_at, last_page_at, updated_at) VALUES (?, ?, ?, 'PENDING_AI', 'CONFIRMED', ?, ?, ?)",
                    (target_document_id, page["sender_id"], source["facility_id"], _iso(now), _iso(now), _iso(now)),
                )
                self._event(db, target_document_id, "STATUS_CHANGED", reviewer, now, {"from": None, "to": "PENDING_AI"})

            position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM pages WHERE document_id = ?",
                                  (target_document_id,)).fetchone()[0]
            db.execute("UPDATE pages SET document_id = ?, position = ? WHERE page_id = ?",
                       (target_document_id, position, page_id))
            for document_id in (source["document_id"], target_document_id):
                self._reset_after_regroup(db, document_id, page_id, source["document_id"], target_document_id,
                                          reviewer, now)
        return {"page_id": page_id, "from_document_id": source["document_id"], "to_document_id": target_document_id}

    def _reset_after_regroup(self, db, document_id, page_id, from_id, to_id, reviewer, now) -> None:
        pages = db.execute("SELECT page_id FROM pages WHERE document_id = ? ORDER BY position", (document_id,)).fetchall()
        detail = {"page_id": page_id, "from": from_id, "to": to_id}
        if not pages:
            db.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            self._event(db, document_id, "DOCUMENT_EMPTIED", reviewer, now, detail)
            return
        for position, row in enumerate(pages, start=1):
            db.execute("UPDATE pages SET position = ? WHERE page_id = ?", (position, row["page_id"]))
        document = self._load_document(db, document_id)
        draft = _loads(document["draft_json"])
        detail["discarded_reviews"] = sum(
            1 for *_, fv in schema.iter_fields(draft) if fv["verification"]["state"] != "UNVERIFIED"
        ) if draft else 0
        db.execute(
            "UPDATE documents SET draft_json = NULL, selection_json = NULL, failure_reason = NULL, "
            "grouping_status = 'CONFIRMED', revision = revision + 1, updated_at = ? WHERE document_id = ?",
            (_iso(now), document_id),
        )
        self._event(db, document_id, "PAGES_REGROUPED", reviewer, now, detail)
        self._set_status(db, document_id, "PENDING_AI", now, actor=reviewer)

    # ------------------------------------------------------------------ reads

    def list_documents(self) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute(
                "SELECT d.*, s.label AS sender_label, "
                "(SELECT COUNT(*) FROM pages p WHERE p.document_id = d.document_id) AS page_count "
                "FROM documents d JOIN senders s USING (sender_id) ORDER BY d.document_id DESC"
            ).fetchall()
        items = []
        for row in rows:
            draft = _loads(row["draft_json"])
            registration = _loads(row["registration_json"])
            items.append({
                "document_id": row["document_id"], "status": row["status"],
                "grouping_status": row["grouping_status"], "page_count": row["page_count"],
                "sender_label": row["sender_label"], "updated_at": row["updated_at"],
                "blocking_count": len(schema.blocking_fields(draft)) if draft else 0,
                "encounter_count": len(draft["encounters"]) if draft else 0,
                "patient_id": registration["patient_id"] if registration else None,
            })
        return items

    def get_document(self, document_id: str) -> dict:
        with self.store.read() as db:
            document = self._load_document(db, document_id)
            pages = [dict(row) for row in db.execute(
                "SELECT page_id, position, media_ref, received_at, source_message_id FROM pages "
                "WHERE document_id = ? ORDER BY position", (document_id,))]
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
                    "next_step": self._next_step(document["status"], blocking, selection, matches),
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
                key: document[key] for key in (
                    "document_id", "status", "grouping_status", "sender_id", "facility_id",
                    "created_at", "last_page_at", "updated_at", "revision", "failure_reason",
                )
            },
            "next_step": review["next_step"] if review else self._next_step(document["status"], [], None, []),
            "pages": pages,
            "draft": draft,
            "review": review,
            "registration": _loads(document["registration_json"]),
            "events": events,
            "move_targets": move_targets,
        }

    @staticmethod
    def _next_step(status: str, blocking: list, selection: dict | None, matches: list) -> str:
        if status == "CAPTURED":
            return "COLLECTING"
        if status == "PENDING_AI":
            return "WAITING_AI"
        if status == "PROCESSING_FAILED":
            return "FAILED"
        if status == "REGISTERED":
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
            "SELECT media_ref FROM pages WHERE document_id = ? ORDER BY position", (document_id,))]

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
