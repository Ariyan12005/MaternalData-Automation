"""HTTP API, simulated WhatsApp webhook, Cloud API webhook listener and back-office UI (standard library only)."""

from __future__ import annotations

import argparse
import os
import hashlib
import hmac
import base64
import json
import logging
import mimetypes
import re
import threading
import uuid
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import extraction_http, live_ocr_adapter, ocr_engines, ocr_process, whatsapp
from .extraction import FixtureExtractor
from .extraction_pool import ExtractionPool
from .offline import OfflineQueue
from .security import Cipher
from .service import DayOneService, Invalid, ServiceError
from .store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 12 * 1024 * 1024
EXTRACTOR_MODES = ("fixture", "external", "paddle", "tesseract", "http")
LOCAL_LEASE_MARGIN_SECONDS = 60
# Routes that change a document the reviewer has on screen: the request carries the revision it was based on.
REVISIONED_ROUTES = ("/fields", "/patient", "/confirm", "/move", "/manual-entry", "/section")

log = logging.getLogger("dayone")


def _authorized(supplied: str, token: str) -> bool:
    digest = lambda value: hmac.new(b"dayone-auth", value.encode("utf-8"), hashlib.sha256).digest()
    if hmac.compare_digest(digest(supplied), digest("Bearer " + token)):
        return True
    if not supplied.startswith("Basic "):
        return False
    try:
        credentials = base64.b64decode(supplied[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(digest(credentials), digest("dayone:" + token))


def _demo_cipher() -> Cipher:
    key = os.environ.get("DAYONE_ENCRYPTION_KEY")
    if key:
        try:
            return Cipher(key)
        except Exception:
            pass
    fixed_32 = b"dayone-demo-fernet-key-32-bytes!"
    return Cipher(base64.urlsafe_b64encode(fixed_32).decode("ascii"))


def extractor_mode_from(value: str | None) -> str:
    """Unset: fixture. Any value given must be a known mode: a typo or an empty value never falls back to fixtures."""
    if value is None:
        return "fixture"
    mode = value.strip().lower()
    if mode not in EXTRACTOR_MODES:
        raise ValueError("DAYONE_EXTRACTOR must be fixture, external, paddle or tesseract")
    return mode


def build_service(db_path: str | Path, *, grouping_window_seconds: int = 8, inbound_media_dir: Path | None = None,
                  extractor_mode: str | None = None, ocr_engine=None, ocr_reader=None,
                  document_timeout: float | None = None) -> DayOneService:
    """extractor_mode defaults to DAYONE_EXTRACTOR. paddle and tesseract need ocr_engine, checked at startup, and
    ocr_reader, which reads its pages (the server passes one long-lived ocr_process.OcrProcessPool); document_timeout
    bounds the OCR of one document, in seconds. Raises ValueError before opening any database."""
    extractor_mode = extractor_mode_from(os.environ.get("DAYONE_EXTRACTOR") if extractor_mode is None else extractor_mode)
    if ocr_engine is not None and extractor_mode == "fixture":
        extractor_mode = ocr_engine.name
    if extractor_mode == "http":
        raise ValueError("DAYONE_EXTRACTOR=http is prepared but not connected (docs/extractor-contract.md)")
    if extractor_mode in ocr_engines.ENGINES and ocr_engine is None:
        raise ValueError(f"DAYONE_EXTRACTOR={extractor_mode} needs the OCR engine checked by `python -m dayone`")
    mode = os.environ.get("DAYONE_STORAGE", "sqlite")
    if mode not in ("sqlite", "mongodb"):
        raise ValueError("DAYONE_STORAGE must be sqlite or mongodb")
    secure = mode == "mongodb"
    key = os.environ.get("DAYONE_ENCRYPTION_KEY")
    if secure:
        if not os.environ.get("DAYONE_API_TOKEN"):
            raise ValueError("Configure DAYONE_API_TOKEN before Atlas startup")
        from .mongo_store import MongoStore
        store = MongoStore(os.environ.get("DAYONE_MONGODB_URI"), os.environ.get("DAYONE_MONGODB_DATABASE") or "dayone")
    else:
        store = Store(db_path)
    media_store = None
    if secure:
        from .media import MongoMediaStore
        media_store = MongoMediaStore(store.db, store.cipher)
    else:
        from .media import MediaStore
        media_store = MediaStore(REPO_ROOT / "var" / "media", cipher=_demo_cipher())
    service = DayOneService(store, None if extractor_mode == "external" else FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                            grouping_window_seconds=grouping_window_seconds, media_store=media_store,
                            inbound_media_dir=inbound_media_dir or whatsapp.media_dir_from_env(os.environ, REPO_ROOT))
    if ocr_engine is not None:
        service.extractor = live_ocr_adapter.LiveOcrAdapter(service.extraction_media, ocr_engine, reader=ocr_reader,
                                                            document_timeout=document_timeout)
        # Decrypted page copies go to the OCR process's locked run folder, removed at close or by the next start.
        service.extraction_temp_dir = getattr(ocr_reader, "work_root", None)
        if document_timeout is not None:
            # One document: its pages within document_timeout, plus at most one model load started before the deadline.
            start = getattr(ocr_reader, "start_timeout", ocr_process.DEFAULT_START_TIMEOUT)
            service.local_lease = timedelta(seconds=document_timeout + start + LOCAL_LEASE_MARGIN_SECONDS)
    return service


def ocr_reader_for(choice: str, settings: dict) -> ocr_process.OcrProcessPool:
    """One OCR process building the same engine as the one checked at startup (same OCR settings only)."""
    environ = {key: value for key, value in os.environ.items()
               if key in ("DAYONE_OCR_MODELS", "DAYONE_OCR_MODELS_DIR", "DAYONE_OCR_CPU_THREADS",
                          "DAYONE_TESSERACT_CMD")}
    spec = ocr_process.EngineSpec("dayone.ocr_engines:create_engine", {"choice": choice, "environ": environ})
    return ocr_process.OcrProcessPool(spec, workers=1, page_timeout=settings["page_timeout"],
                                      start_timeout=settings["start_timeout"], temp_root=settings["temp_root"])


class Api:
    def __init__(self, service: DayOneService, offline_queue: OfflineQueue | None = None):
        self.service = service
        if offline_queue is not None:
            self.offline_queue = offline_queue
        else:
            queue_path = REPO_ROOT / "var" / "offline-demo.sqlite3"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            self.offline_queue = OfflineQueue(queue_path, cipher=_demo_cipher())
        s = service
        self.routes = [
            ("POST", r"/api/simulator/offline/capture", lambda m, q, b, r: self._offline_capture(b)),
            ("GET", r"/api/simulator/offline/status", lambda m, q, b, r: self._offline_status()),
            ("POST", r"/api/simulator/offline/flush", lambda m, q, b, r: self._offline_flush()),
            ("POST", r"/api/admin/senders", lambda m, q, b, r: s.enroll_sender(
                facility_id=b.get("facility_id"), facility_name=b.get("facility_name"),
                sender_id=b.get("sender_id"), label=b.get("label"), channel=b.get("channel"))),
            ("POST", r"/api/whatsapp/uploads", lambda m, q, b, r: s.ingest_upload(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"),
                image_base64=b.get("image_base64"), suffix=b.get("suffix", ".jpg"), group_id=b.get("group_id"), retake_request_id=b.get("retake_request_id"))),
            ("POST", r"/webhooks/whatsapp", lambda m, q, b, r: s.ingest_upload(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"),
                image_base64=b.get("image_base64"), suffix=b.get("suffix", ".jpg"), group_id=b.get("group_id"), retake_request_id=b.get("retake_request_id"))),
            ("POST", r"/api/extraction/jobs/claim", lambda m, q, b, r: s.claim_job(
                b.get("document_id") if isinstance(b.get("document_id"), str) else None)),
            ("POST", r"/api/extraction/jobs/(JOB-\d+)/result", lambda m, q, b, r: s.complete_job(
                m[1], token=b.get("token"), draft=b.get("draft"), expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/extraction/jobs/(JOB-\d+)/failure", lambda m, q, b, r: s.fail_job(
                m[1], token=b.get("token"), expected_revision=b.get("expected_revision"), code=b.get("code"))),
            ("POST", r"/api/documents/(DOC-\d+)/close", lambda m, q, b, r: s.close_capture(m[1]) or {"ok": True}),
            ("GET", r"/api/whatsapp/deliveries", lambda m, q, b, r: whatsapp.delivery_overview(s.store)),
            ("GET", r"/api/system", lambda m, q, b, r: s.system_info()),
            ("POST", r"/api/system/ai", lambda m, q, b, r: s.set_ai_available(bool(b.get("available")))),
            ("POST", r"/api/demo/reset", self._reset),
            ("GET", r"/api/senders", lambda m, q, b, r: s.list_senders()),
            ("GET", r"/api/media", lambda m, q, b, r: s.list_media()),
            ("POST", r"/api/whatsapp/messages", lambda m, q, b, r: s.ingest_photo(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"), media_ref=b.get("media_ref"),
                group_id=b.get("group_id"), retake_request_id=b.get("retake_request_id"))),
            ("GET", r"/api/whatsapp/thread", lambda m, q, b, r: s.thread(_query(q, "sender_id"))),
            ("GET", r"/api/documents", lambda m, q, b, r: s.list_documents()),
            ("GET", r"/api/documents/(DOC-\d+)", lambda m, q, b, r: s.get_document(m[1])),
            ("POST", r"/api/documents/(DOC-\d+)/fields", lambda m, q, b, r: s.review_field(
                m[1], reviewer=r, scope=b.get("scope"), field=b.get("field"), action=b.get("action"),
                encounter_index=b.get("encounter_index"), value=b.get("value"),
                field_status=b.get("field_status"), expected_revision=b.get("expected_revision"),
                section=b.get("section"), item_index=b.get("item_index"))),
            ("POST", r"/api/documents/(DOC-\d+)/patient", lambda m, q, b, r: s.select_patient(
                m[1], reviewer=r, choice=b.get("choice"), patient_id=b.get("patient_id"),
                expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/documents/(DOC-\d+)/confirm", lambda m, q, b, r: s.confirm(
                m[1], reviewer=r, existing_visit_decisions=b.get("existing_visit_decisions"),
                expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/documents/(DOC-\d+)/manual-entry", lambda m, q, b, r: s.start_manual_entry(
                m[1], reviewer=r, expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/pages/(PAGE-\d+)/move", lambda m, q, b, r: s.move_page(
                m[1], reviewer=r, target_document_id=b.get("target_document_id"), expected_revision=b.get("expected_revision"), target_expected_revision=b.get("target_expected_revision"))),
            ("POST", r"/api/pages/(PAGE-\d+)/retake", lambda m, q, b, r: s.request_retake(m[1], reviewer=r)),
            ("POST", r"/api/pages/(PAGE-\d+)/section", lambda m, q, b, r: s.set_page_section(
                m[1], reviewer=r, section=b.get("section"), expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/retakes/(RTK-\d+)/cancel", lambda m, q, b, r: s.cancel_retake(m[1], reviewer=r)),
            ("POST", r"/api/documents/(DOC-\d+)/sync", lambda m, q, b, r: s.sync_document(m[1], reviewer=r or "system")),
            ("GET", r"/api/documents/(DOC-\d+)/conversational", lambda m, q, b, r: s.conversational_prompt(m[1])),
            ("POST", r"/api/documents/(DOC-\d+)/conversational", lambda m, q, b, r: s.conversational_reply(
                m[1], reviewer=r or "midwife", **b)),
            ("GET", r"/api/patients", lambda m, q, b, r: s.list_patients()),
            ("GET", r"/api/patients/(PAT-\d+)/timeline", lambda m, q, b, r: s.patient_timeline(m[1])),
            ("GET", r"/api/patients/(PAT-\d+)/export", lambda m, q, b, r: s.export_patient(m[1])),
            ("GET", r"/api/export", lambda m, q, b, r: {"rows": s.export_all_patients()}),
            ("GET", r"/api/export/csv", lambda m, q, b, r: {"csv": s.export_csv()}),
        ]

    def _offline_capture(self, body: dict) -> dict:
        sender_id = body.get("sender_id") or "whatsapp:+212600000001"
        message_id = body.get("message_id") or f"OFFLINE-{uuid.uuid4().hex[:8]}"
        img_b64 = body.get("image_base64")
        if not img_b64:
            raise Invalid("IMAGE_REQUIRED", "image_base64 manquant pour la capture hors-ligne.")
        try:
            raw_bytes = base64.b64decode(img_b64)
        except Exception:
            raise Invalid("INVALID_IMAGE_BASE64", "Décodage base64 échoué.")
        group_id = body.get("group_id")
        suffix = body.get("suffix", ".jpg")
        key = self.offline_queue.capture(sender_id, message_id, raw_bytes, group_id=group_id, suffix=suffix)
        return {
            "ok": True,
            "key": key,
            "message_id": message_id,
            "state": "PENDING",
            "buffered_count": len(self.offline_queue.list_items("PENDING")),
        }

    def _offline_status(self) -> dict:
        return {
            "items": self.offline_queue.list_items(),
            "pending_count": len(self.offline_queue.list_items("PENDING")),
            "synced_count": len(self.offline_queue.list_items("SYNCED")),
        }

    def _offline_flush(self) -> dict:
        def _send(body):
            return self.service.ingest_upload(**body)
        def _close(doc_id):
            return self.service.close_capture(doc_id) or {"ok": True}
        delivered = self.offline_queue.flush(_send, close_group=_close)
        return {
            "ok": True,
            "delivered": delivered,
            "items": self.offline_queue.list_items(),
        }

    def _reset(self, match, query, body, reviewer):
        if os.environ.get("DAYONE_STORAGE", "sqlite") == "mongodb" or whatsapp.outbound_hold_active(self.service.store):
            raise Invalid("RESET_DISABLED", "Reset disabled for secure storage or held outbound messages")
        self.service.reset_demo(with_history=body.get("with_history", True))
        if self.offline_queue:
            self.offline_queue.reset()
        return {"ok": True}

    def close(self):
        if self.offline_queue:
            self.offline_queue.close()

    def dispatch(self, method: str, path: str, query: dict, body: dict, reviewer: str | None):
        for route_method, pattern, handler in self.routes:
            match = re.fullmatch(pattern, path)
            if match and route_method == method:
                try:
                    if method == "POST" and path.endswith(REVISIONED_ROUTES) and type(body.get("expected_revision")) is not int:
                        raise Invalid("REVISION_REQUIRED", "Provide integer expected_revision")
                    if path.endswith("/move") and body.get("target_document_id") and type(body.get("target_expected_revision")) is not int:
                        raise Invalid("REVISION_REQUIRED", "Provide target_expected_revision for regrouping")
                    if path.endswith("/confirm"):
                        decisions = body.get("existing_visit_decisions")
                        if decisions is None:
                            decisions = {}
                        if not isinstance(decisions, dict):
                            raise Invalid("INVALID_DECISIONS", "Visit decisions must be an object")
                        if any(value == "UPDATE" for value in decisions.values()):
                            raise Invalid("VISIT_VERSION_REQUIRED", "UPDATE requires selected fields and expected_version")
                    return HTTPStatus.OK, handler(match, query, body, reviewer)
                except ServiceError as exc:
                    return exc.status, {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
                except Exception as exc:
                    # The type only: a message or traceback may quote request values or draft fields.
                    log.error("unhandled error on %s %s (%s)", method, path, type(exc).__name__)
                    return HTTPStatus.INTERNAL_SERVER_ERROR, {
                        "error": {"code": "INTERNAL", "message": "Erreur interne.", "details": None}}
        return HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": "Route inconnue.", "details": None}}


def _query(query: dict, name: str) -> str:
    values = query.get(name)
    if not values:
        raise Invalid("QUERY_PARAM_REQUIRED", f"Paramètre {name} manquant.")
    return values[0]


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DayOne/0.1"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            token = os.environ.get("DAYONE_API_TOKEN")
            if token and not _authorized(self.headers.get("Authorization", ""), token):
                self._send_error(HTTPStatus.UNAUTHORIZED, "AUTH_REQUIRED", "Provide backend bearer token")
                return
            if url.path.startswith(("/api/", "/webhooks/")):
                body = {}
                if method == "POST":
                    body = self._read_json()
                    if body is None:
                        return
                status, payload = api.dispatch(method, url.path, parse_qs(url.query), body,
                                               self.headers.get("X-Reviewer"))
                self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")
            elif method == "GET" and url.path.startswith("/media/"):
                self._send_media(unquote(url.path[len("/media/"):]))
            elif method == "GET" and url.path in ("/export/registry.csv", "/export/maternal_registry.csv", "/api/export/registry.csv"):
                csv_data = api.service.export_csv().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="maternal_registry_export.csv"')
                self.send_header("Content-Length", str(len(csv_data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(csv_data)
            elif method == "GET":
                self._send_static(url.path)
            else:
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", "text/plain")

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._send_error(HTTPStatus.BAD_REQUEST, "INVALID_LENGTH", "Invalid body length")
                return None
            if length > MAX_BODY_BYTES:
                self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "BODY_TOO_LARGE", "Requête trop volumineuse.")
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = None
            if not isinstance(body, dict):
                self._send_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", "Corps JSON invalide.")
                return None
            return body

        def _send_error(self, status, code, message):
            payload = {"error": {"code": code, "message": message, "details": None}}
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _send_media(self, media_ref: str) -> None:
            try:
                data = api.service.read_media(media_ref)
            except ServiceError:
                self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return
            self._send(HTTPStatus.OK, data, mimetypes.guess_type(media_ref)[0] or "application/octet-stream",
                       cache=False)

        def _send_static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (STATIC_DIR / name).resolve()
            if file.parent != STATIC_DIR.resolve() or not file.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
                return
            text_types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            image_types = {".jpg": "image/jpeg", ".png": "image/png"}
            if file.suffix in text_types:
                self._send(HTTPStatus.OK, file.read_bytes(), f"{text_types[file.suffix]}; charset=utf-8")
            else:
                self._send(HTTPStatus.OK, file.read_bytes(), image_types.get(file.suffix, "application/octet-stream"),
                           cache=True)

        def _send(self, status, data: bytes, content_type: str, *, cache: bool = False) -> None:
            self.send_response(status)
            if status == HTTPStatus.UNAUTHORIZED:
                self.send_header("WWW-Authenticate", 'Basic realm="DayOne staff", charset="UTF-8"')
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def log_request(self, code="-", size="-"):
            # Path only: query strings carry sender numbers.
            log.info("%s %s -> %s", self.command, urlparse(self.path).path, code)

        def log_message(self, format, *args):
            log.warning("http: %s", format % args)

    return Handler


def make_webhook_handler(adapter: whatsapp.WhatsAppCloud):
    """Serves only the Cloud API webhook path; this is the one listener a tunnel may expose."""

    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "DayOne-Webhook"

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != whatsapp.WEBHOOK_PATH:
                self._send(HTTPStatus.NOT_FOUND, b"")
                return
            challenge = adapter.subscription_challenge(parse_qs(url.query))
            if challenge is None:
                self._send(HTTPStatus.FORBIDDEN, b"")
                return
            self._send(HTTPStatus.OK, challenge.encode("ascii"))

        def do_POST(self):
            if urlparse(self.path).path != whatsapp.WEBHOOK_PATH:
                self._send(HTTPStatus.NOT_FOUND, b"")
                return
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                self._send(HTTPStatus.LENGTH_REQUIRED, b"")
                return
            if int(length) > whatsapp.MAX_WEBHOOK_BYTES:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"")
                return
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                self._send(HTTPStatus.BAD_REQUEST, b"")
                return
            if not adapter.signature_valid(raw, self.headers.get("X-Hub-Signature-256")):
                log.warning("webhook rejected: missing or invalid signature")
                self._send(HTTPStatus.UNAUTHORIZED, b"")
                return
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict):
                self._send(HTTPStatus.BAD_REQUEST, b"")
                return
            try:
                summary = adapter.record_webhook(payload)
            except Exception as exc:
                # Non-200 makes Meta retry; duplicates are dropped by WhatsApp message ID.
                log.error("webhook not recorded (%s)", type(exc).__name__)
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"")
                return
            log.info("webhook recorded: %s", summary)
            self._send(HTTPStatus.OK, b"")

        def _send(self, status, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_request(self, code="-", size="-"):
            log.info("webhook %s %s -> %s", self.command, urlparse(self.path).path, code)

        def log_message(self, format, *args):
            # args can hold the raw request line, whose query string carries hub.verify_token.
            log.warning("webhook http error")

    return WebhookHandler


def _run_worker(service: DayOneService, stop: threading.Event, adapter: whatsapp.WhatsAppCloud | None = None,
                interval: float = 1.0, send_outbound: bool = True, pool: ExtractionPool | None = None) -> None:
    """Every second: WhatsApp inbound, queue the documents to extract, WhatsApp outbound. With a pool, extraction
    runs on the pool's threads and never delays the WhatsApp jobs; without one, it runs here (service.tick)."""
    while not stop.wait(interval):
        if adapter is not None:
            try:
                adapter.process_inbound()
            except Exception as exc:
                log.error("whatsapp inbound tick failed (%s)", type(exc).__name__)
        try:
            if pool is not None:
                pool.submit(service.queued_documents())
            else:
                service.tick()
        except Exception as exc:
            log.error("queue tick failed (%s); pending records retained", type(exc).__name__)
        if adapter is not None and send_outbound:
            try:
                adapter.deliver_outbound()
            except Exception as exc:
                log.error("whatsapp outbound tick failed (%s)", type(exc).__name__)


def main(argv: list[str] | None = None) -> None:
    from .config import load_config
    load_config()
    parser = argparse.ArgumentParser(description="DayOne fixture-driven prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(REPO_ROOT / "var" / "dayone.sqlite3"))
    parser.add_argument("--window", type=int, default=8, help="multipage grouping window in seconds")
    parser.add_argument("--env-file", help="read KEY=VALUE settings from this file (existing variables win)")
    parser.add_argument("--whatsapp-mode", choices=("simulator", "cloud"),
                        help="default: DAYONE_WHATSAPP_MODE, else simulator")
    parser.add_argument("--webhook-host", default="127.0.0.1")
    parser.add_argument("--webhook-port", type=int, default=8001)
    parser.add_argument("--hold-outbound", action="store_true",
                        help="queue outgoing WhatsApp messages but never send them; the hold is stored in the "
                             "database and survives restarts until `python -m dayone.whatsapp discard-held`")
    parser.add_argument("--extractor", choices=EXTRACTOR_MODES,
                        help="default: DAYONE_EXTRACTOR, else fixture. paddle (primary) or tesseract (optional) runs "
                             "local OCR in one separate process with page and document timeouts (docs/ocr.md); "
                             "external leaves extraction to workers of the job API; http is prepared but not "
                             "connected yet")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.env_file:
        whatsapp.load_env_file(args.env_file)
    mode = args.whatsapp_mode or os.environ.get("DAYONE_WHATSAPP_MODE", "simulator").strip() or "simulator"
    if mode not in ("simulator", "cloud"):
        parser.error("DAYONE_WHATSAPP_MODE must be simulator or cloud")
    try:
        extractor_mode = extractor_mode_from(args.extractor or os.environ.get("DAYONE_EXTRACTOR"))
    except ValueError as exc:
        parser.error(str(exc))
    ocr_engine = None
    try:
        ocr_settings = ocr_process.settings_from_env(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    if extractor_mode in ocr_engines.ENGINES:
        try:
            ocr_engine = ocr_engines.create_engine(extractor_mode)
        except ValueError as exc:
            parser.error(str(exc))
        missing = ocr_engine.missing()
        if missing:
            parser.error(f"extractor {extractor_mode}: missing {', '.join(missing)}. See docs/ocr.md, "
                         "or use --extractor fixture (the default).")
    if extractor_mode == "http":
        try:
            extraction_http.load_extractor_config(os.environ)
        except extraction_http.ExtractorConfigError as exc:
            parser.error(str(exc))
        parser.error("extractor http: the endpoint settings are valid, but the request format, authentication and "
                     "error responses are not agreed with Fatma yet, so no page is sent. See docs/extractor-contract.md. "
                     "Use --extractor fixture (the default) meanwhile.")

    config = None
    if mode == "cloud":
        if not whatsapp.is_loopback(args.host):
            parser.error("cloud mode: the back-office has no authentication; keep --host on 127.0.0.1 "
                         "and expose only the webhook port")
        if args.webhook_port == args.port:
            parser.error("cloud mode: --webhook-port must differ from --port")
        try:
            config = whatsapp.load_config(os.environ, REPO_ROOT)
        except whatsapp.ConfigError as exc:
            parser.error(str(exc))

    ocr_reader = ocr_reader_for(extractor_mode, ocr_settings) if ocr_engine is not None else None
    try:
        service = build_service(args.db, grouping_window_seconds=args.window,
                                inbound_media_dir=config.media_dir if config else None, extractor_mode=extractor_mode,
                                ocr_engine=ocr_engine, ocr_reader=ocr_reader,
                                document_timeout=ocr_settings["document_timeout"])
    except ValueError as exc:
        if ocr_reader is not None:
            ocr_reader.close()
        parser.error(str(exc))
    if config is None and os.environ.get("DAYONE_STORAGE", "sqlite") != "mongodb":
        service.ensure_seed()
    if service.extractor is not None:
        released = service.release_local_leases()
        if released:
            log.info("%d extraction(s) of the previous run will start again", released)
    if args.hold_outbound:
        whatsapp.hold_outbound(service.store)
    elif config is not None and whatsapp.outbound_hold_active(service.store):
        service.store.close()
        if ocr_reader is not None:
            ocr_reader.close()
        parser.error("cloud mode: this database was run with --hold-outbound and its queued messages must never be "
                     "sent by accident. Keep --hold-outbound, or run `python -m dayone.whatsapp discard-held --db "
                     "<path>` to drop them (nothing is sent) before sending new messages.")
    if config is not None:
        whatsapp.hold_outbound(service.store)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(Api(service)))
    adapter, webhook_server = None, None
    if config is not None:
        adapter = whatsapp.WhatsAppCloud(service, config)
        webhook_server = ThreadingHTTPServer((args.webhook_host, args.webhook_port), make_webhook_handler(adapter))
        threading.Thread(target=webhook_server.serve_forever, daemon=True).start()
    stop = threading.Event()
    # One document at a time, read by one OCR process that keeps its models loaded between pages. In external mode
    # nothing is extracted here: workers claim jobs through the API.
    pool = ExtractionPool(service, workers=1) if service.extractor is not None else None
    if ocr_reader is not None:
        ocr_reader.warm_up()
    # Outbound WhatsApp messages are never sent by this worker: they stay queued and held.
    threading.Thread(target=_run_worker, args=(service, stop, adapter),
                     kwargs={"send_outbound": False, "pool": pool}, daemon=True).start()
    print(f"DayOne prototype running at http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    print(f"Extractor: {service.system_info()['extractor']}")
    if webhook_server is not None:
        print(f"WhatsApp Cloud API webhook (only this port may be tunnelled): "
              f"http://{args.webhook_host}:{args.webhook_port}{whatsapp.WEBHOOK_PATH}  API {config.api_version}")
        print("Outgoing WhatsApp messages are held; live delivery is disabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if webhook_server is not None:
            webhook_server.shutdown()
            webhook_server.server_close()
        server.server_close()
        # Stopping the OCR processes ends any page in progress; its document stays queued for the next start.
        if pool is not None:
            pool.shutdown()
        if ocr_reader is not None:
            ocr_reader.close()
        if pool is not None:
            pool.wait_idle(15)
        service.store.close()


if __name__ == "__main__":
    main()
