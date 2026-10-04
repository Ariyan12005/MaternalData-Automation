"""HTTP API, simulated WhatsApp webhook and back-office UI (standard library only)."""

from __future__ import annotations

import argparse
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
from .live_ocr import LiveOcrExtractor
from .service import DayOneService, Invalid, ServiceError
from .store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024

log = logging.getLogger("dayone")


def build_service(db_path: str | Path, *, grouping_window_seconds: int = 8, extractor_mode: str = "fixture") -> DayOneService:
    if extractor_mode not in ("fixture", "paddleocr"):
        raise ValueError("Unsupported extractor: " + extractor_mode)
    extractor = FixtureExtractor(REPO_ROOT / "fixtures") if extractor_mode == "fixture" else LiveOcrExtractor(REPO_ROOT)
    return DayOneService(
        Store(db_path),
        extractor,
        REPO_ROOT,
        grouping_window_seconds=grouping_window_seconds,
    )


class Api:
    def __init__(self, service: DayOneService):
        self.service = service
        s = service
        self.routes = [
            ("GET", r"/api/system", lambda m, q, b, r: s.system_info()),
            ("POST", r"/api/system/ai", lambda m, q, b, r: s.set_ai_available(bool(b.get("available")))),
            ("POST", r"/api/demo/reset", self._reset),
            ("GET", r"/api/senders", lambda m, q, b, r: s.list_senders()),
            ("GET", r"/api/media", lambda m, q, b, r: s.list_media()),
            ("POST", r"/api/whatsapp/messages", lambda m, q, b, r: s.ingest_photo(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"), media_ref=b.get("media_ref"))),
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
                m[1], reviewer=r)),
            ("POST", r"/api/pages/(PAGE-\d+)/move", lambda m, q, b, r: s.move_page(
                m[1], reviewer=r, target_document_id=b.get("target_document_id"))),
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
                    return HTTPStatus.OK, handler(match, query, body, reviewer)
                except ServiceError as exc:
                    return exc.status, {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
                except Exception:
                    log.exception("unhandled error on %s %s", method, path)
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
            if url.path.startswith("/api/"):
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
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "BODY_TOO_LARGE", "Requête trop volumineuse.")
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
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
                path = api.service.resolve_media(media_ref)
            except ServiceError:
                self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return
            self._send(HTTPStatus.OK, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                       cache=True)

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
            log.exception("queue tick failed")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="DayOne maternal registry prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(REPO_ROOT / "var" / "dayone.sqlite3"))
    parser.add_argument("--window", type=int, default=8, help="multipage grouping window in seconds")
    parser.add_argument("--extractor", choices=("fixture", "paddleocr"), default="fixture",
                        help="fixture for the stable demo; paddleocr for local real-photo OCR")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = build_service(args.db, grouping_window_seconds=args.window, extractor_mode=args.extractor)
    # Fixture mode includes a pre-confirmed history for the visual demo.  Real
    # OCR always requires a human review, so it must start with an empty demo.
    if args.extractor == "paddleocr":
        service.reset_demo(with_history=False)
    else:
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
