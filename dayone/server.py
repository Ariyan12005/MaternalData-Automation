"""HTTP API, simulated WhatsApp webhook and back-office UI (standard library only)."""

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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .extraction import FixtureExtractor
from .service import DayOneService, Invalid, ServiceError
from .store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 12 * 1024 * 1024

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


def build_service(db_path: str | Path, *, grouping_window_seconds: int = 8) -> DayOneService:
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
    elif key:
        from .media import MediaStore
        media_store = MediaStore(REPO_ROOT / "var" / "media")
    return DayOneService(store, None if os.environ.get("DAYONE_EXTRACTOR") == "external" else FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                         grouping_window_seconds=grouping_window_seconds, media_store=media_store)



class Api:
    def __init__(self, service: DayOneService):
        self.service = service
        s = service
        self.routes = [
            ("POST", r"/api/admin/senders", lambda m, q, b, r: s.enroll_sender(
                facility_id=b.get("facility_id"), facility_name=b.get("facility_name"),
                sender_id=b.get("sender_id"), label=b.get("label"))),
            ("POST", r"/api/whatsapp/uploads", lambda m, q, b, r: s.ingest_upload(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"),
                image_base64=b.get("image_base64"), suffix=b.get("suffix", ".jpg"), group_id=b.get("group_id"))),
            ("POST", r"/webhooks/whatsapp", lambda m, q, b, r: s.ingest_upload(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"),
                image_base64=b.get("image_base64"), suffix=b.get("suffix", ".jpg"), group_id=b.get("group_id"))),
            ("POST", r"/api/extraction/jobs/claim", lambda m, q, b, r: s.claim_job(
                b.get("document_id") if isinstance(b.get("document_id"), str) else None)),
            ("POST", r"/api/extraction/jobs/(JOB-\d+)/result", lambda m, q, b, r: s.complete_job(
                m[1], token=b.get("token"), draft=b.get("draft"), expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/documents/(DOC-\d+)/close", lambda m, q, b, r: s.close_capture(m[1]) or {"ok": True}),
            ("GET", r"/api/system", lambda m, q, b, r: s.system_info()),
            ("POST", r"/api/system/ai", lambda m, q, b, r: s.set_ai_available(bool(b.get("available")))),
            ("POST", r"/api/demo/reset", self._reset),
            ("GET", r"/api/senders", lambda m, q, b, r: s.list_senders()),
            ("GET", r"/api/media", lambda m, q, b, r: s.list_media()),
            ("POST", r"/api/whatsapp/messages", lambda m, q, b, r: s.ingest_photo(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"),
                media_ref=b.get("media_ref"), group_id=b.get("group_id"))),
            ("GET", r"/api/whatsapp/thread", lambda m, q, b, r: s.thread(_query(q, "sender_id"))),
            ("GET", r"/api/documents", lambda m, q, b, r: s.list_documents()),
            ("GET", r"/api/documents/(DOC-\d+)", lambda m, q, b, r: s.get_document(m[1])),
            ("POST", r"/api/documents/(DOC-\d+)/fields", lambda m, q, b, r: s.review_field(
                m[1], reviewer=r, scope=b.get("scope"), field=b.get("field"), action=b.get("action"),
                encounter_index=b.get("encounter_index"), value=b.get("value"),
                field_status=b.get("field_status"), expected_revision=b.get("expected_revision"))),
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
            ("GET", r"/api/patients", lambda m, q, b, r: s.list_patients()),
            ("GET", r"/api/patients/(PAT-\d+)/timeline", lambda m, q, b, r: s.patient_timeline(m[1])),
        ]

    def _reset(self, match, query, body, reviewer):
        self.service.reset_demo(with_history=body.get("with_history", True))
        return {"ok": True}

    def dispatch(self, method: str, path: str, query: dict, body: dict, reviewer: str | None):
        for route_method, pattern, handler in self.routes:
            match = re.fullmatch(pattern, path)
            if match and route_method == method:
                try:
                    if method == "POST" and path.endswith(("/fields", "/patient", "/confirm", "/move", "/manual-entry")) and type(body.get("expected_revision")) is not int:
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
                except Exception:
                    log.error("request failed on %s %s", method, path)
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
            content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}.get(
                file.suffix, "application/octet-stream")
            self._send(HTTPStatus.OK, file.read_bytes(), f"{content_type}; charset=utf-8")

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


def _run_worker(service: DayOneService, stop: threading.Event, interval: float = 1.0) -> None:
    while not stop.wait(interval):
        try:
            service.tick()
        except Exception:
            log.error("queue tick failed; pending records retained")


def main(argv: list[str] | None = None) -> None:
    from .config import load_config
    load_config()
    parser = argparse.ArgumentParser(description="DayOne fixture-driven prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(REPO_ROOT / "var" / "dayone.sqlite3"))
    parser.add_argument("--window", type=int, default=8, help="multipage grouping window in seconds")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = build_service(args.db, grouping_window_seconds=args.window)
    if os.environ.get("DAYONE_STORAGE", "sqlite") != "mongodb":
        service.ensure_seed()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(Api(service)))
    stop = threading.Event()
    threading.Thread(target=_run_worker, args=(service, stop), daemon=True).start()
    print(f"DayOne prototype running at http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        service.store.close()


if __name__ == "__main__":
    main()
