"""HTTP API, simulated WhatsApp webhook and back-office UI (standard library + cryptography)."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import mimetypes
import os
import re
import ssl
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import crypto
from .auth import Auth, AuthError, SESSION_HOURS
from .extraction import FixtureExtractor, HttpExtractor
from .live_ocr import LiveOcrExtractor, PaddleOcrReader, TesseractOcrReader
from .media import MAX_PHOTO_BYTES, MediaStore
from .service import DayOneService, Invalid, ServiceError
from .store import Store
from .sync import HttpCentralRegistry, LocalCentralRegistry
from .whatsapp import WhatsAppChannel, WhatsAppConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024
SESSION_COOKIE = "dayone_session"
CSRF_HEADER = "X-Requested-With"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
}

log = logging.getLogger("dayone")


def build_service(db_path: str | Path, *, grouping_window_seconds: int = 8, extractor_mode: str = "fixture",
                  encrypt: bool = True) -> DayOneService:
    readers = {"tesseract": TesseractOcrReader, "paddleocr": PaddleOcrReader}
    if extractor_mode not in ("fixture", "http") and extractor_mode not in readers:
        raise ValueError("Unsupported extractor: " + extractor_mode)
    var_dir = Path(db_path).parent
    keys = crypto.ciphers(crypto.load_master_key(var_dir)) if encrypt else {}
    media = MediaStore(var_dir / "media", keys.get("media"))
    service = DayOneService(
        Store(db_path, keys.get("database")),
        None,
        REPO_ROOT,
        grouping_window_seconds=grouping_window_seconds,
        media=media,
    )
    if extractor_mode == "fixture":
        service.extractor = FixtureExtractor(REPO_ROOT / "fixtures")
    elif extractor_mode == "http":
        if not os.environ.get("DAYONE_EXTRACTOR_URL"):
            raise ValueError("--extractor http needs DAYONE_EXTRACTOR_URL (and DAYONE_EXTRACTOR_TOKEN)")
        service.extractor = HttpExtractor(os.environ["DAYONE_EXTRACTOR_URL"], os.environ.get("DAYONE_EXTRACTOR_TOKEN", ""),
                                          service.read_media)
    else:
        service.extractor = LiveOcrExtractor(REPO_ROOT, reader=readers[extractor_mode](), media=service.read_media)
    service.auth = Auth(service.store)
    config = WhatsAppConfig.from_env()
    service.whatsapp = WhatsAppChannel(config, service) if config else None
    service.outbound_enabled = config is not None
    if os.environ.get("DAYONE_CENTRAL_URL"):
        service.central = HttpCentralRegistry(os.environ["DAYONE_CENTRAL_URL"], os.environ.get("DAYONE_CENTRAL_SECRET", ""))
    else:
        service.central = LocalCentralRegistry(var_dir / "central" / "registry.log", keys.get("central"),
                                               available=service.central_reachable)
    return service


class Api:
    """Routes: (method, pattern, role, handler). Role None = public, "any" = signed in, "admin" = admin only."""

    def __init__(self, service: DayOneService):
        self.service = service
        self.auth: Auth = service.auth
        s = service
        self.routes = [
            ("GET", r"/api/me", "any", lambda m, q, b, u: u),
            ("GET", r"/api/system", "any", lambda m, q, b, u: s.system_info()),
            ("POST", r"/api/system/ai", "admin", lambda m, q, b, u: s.set_ai_available(bool(b.get("available")))),
            ("POST", r"/api/system/central", "admin", lambda m, q, b, u: s.set_central_available(bool(b.get("available")))),
            ("POST", r"/api/demo/reset", "admin", self._reset),
            ("GET", r"/api/users", "admin", lambda m, q, b, u: self.auth.list_users()),
            ("POST", r"/api/users", "admin", lambda m, q, b, u: self.auth.create_user(
                b.get("username"), b.get("password"), b.get("role", "reviewer"))),
            ("GET", r"/api/senders", "any", lambda m, q, b, u: s.list_senders()),
            ("POST", r"/api/senders", "admin", lambda m, q, b, u: s.register_sender(
                sender_id=b.get("sender_id"), label=b.get("label"), facility_id=b.get("facility_id"),
                facility_name=b.get("facility_name"))),
            ("GET", r"/api/facilities", "any", lambda m, q, b, u: s.list_facilities()),
            ("GET", r"/api/media", "any", lambda m, q, b, u: s.list_media()),
            ("POST", r"/api/media", "any", lambda m, q, b, u: s.upload_photo(b)),
            ("POST", r"/api/whatsapp/messages", "any", lambda m, q, b, u: s.ingest_photo(
                sender_id=b.get("sender_id"), message_id=b.get("message_id"), media_ref=b.get("media_ref"),
                captured_at=b.get("captured_at"))),
            ("GET", r"/api/whatsapp/thread", "any", lambda m, q, b, u: s.thread(_query(q, "sender_id"))),
            ("GET", r"/api/documents", "any", lambda m, q, b, u: s.list_documents()),
            ("GET", r"/api/documents/(DOC-\d+)", "any", lambda m, q, b, u: s.get_document(m[1])),
            ("POST", r"/api/documents/(DOC-\d+)/fields", "any", lambda m, q, b, u: s.review_field(
                m[1], reviewer=u["username"], scope=b.get("scope"), field=b.get("field"), action=b.get("action"),
                encounter_index=b.get("encounter_index"), value=b.get("value"),
                field_status=b.get("field_status"), expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/documents/(DOC-\d+)/patient", "any", lambda m, q, b, u: s.select_patient(
                m[1], reviewer=u["username"], choice=b.get("choice"), patient_id=b.get("patient_id"),
                expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/documents/(DOC-\d+)/confirm", "any", lambda m, q, b, u: s.confirm(
                m[1], reviewer=u["username"], existing_visit_decisions=b.get("existing_visit_decisions"),
                expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/documents/(DOC-\d+)/manual-entry", "any", lambda m, q, b, u: s.start_manual_entry(
                m[1], reviewer=u["username"])),
            ("POST", r"/api/documents/(DOC-\d+)/encounters", "any", lambda m, q, b, u: s.add_manual_encounter(
                m[1], reviewer=u["username"], expected_revision=b.get("expected_revision"))),
            ("POST", r"/api/pages/(PAGE-\d+)/move", "any", lambda m, q, b, u: s.move_page(
                m[1], reviewer=u["username"], target_document_id=b.get("target_document_id"))),
            ("POST", r"/api/pages/(PAGE-\d+)/retake", "any", lambda m, q, b, u: s.request_retake(
                m[1], reviewer=u["username"], reason=b.get("reason"))),
            ("GET", r"/api/patients", "any", lambda m, q, b, u: s.list_patients()),
            ("GET", r"/api/export/visits\.json", "admin", lambda m, q, b, u: s.export_visits()),
            ("GET", r"/api/patients/(PAT-\d+)/timeline", "any", lambda m, q, b, u: s.patient_timeline(m[1])),
        ]

    def _reset(self, match, query, body, user):
        self.service.reset_demo(with_history=body.get("with_history", True))
        return {"ok": True}

    def dispatch(self, method: str, path: str, query: dict, body, user: dict | None):
        for route_method, pattern, role, handler in self.routes:
            match = re.fullmatch(pattern, path)
            if not match or route_method != method:
                continue
            if role is not None and user is None:
                return HTTPStatus.UNAUTHORIZED, _error("LOGIN_REQUIRED", "Connectez-vous pour continuer.")
            if role == "admin" and user["role"] != "admin":
                return HTTPStatus.FORBIDDEN, _error("ADMIN_ONLY", "Action réservée à un administrateur.")
            try:
                return HTTPStatus.OK, handler(match, query, body, user)
            except ServiceError as exc:
                return exc.status, {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
            except AuthError as exc:
                return exc.status, _error(exc.code, exc.message)
            except Exception:
                log.exception("unhandled error on %s %s", method, path)
                return HTTPStatus.INTERNAL_SERVER_ERROR, _error("INTERNAL", "Erreur interne.")
        return HTTPStatus.NOT_FOUND, _error("NOT_FOUND", "Route inconnue.")


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "details": None}}


def _query(query: dict, name: str) -> str:
    values = query.get(name)
    if not values:
        raise Invalid("QUERY_PARAM_REQUIRED", f"Paramètre {name} manquant.")
    return values[0]


def make_handler(api: Api, *, secure_cookies: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DayOne/0.2"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _session_token(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            if url.path == "/webhook/whatsapp":
                self._whatsapp(method, url)
                return
            if url.path.startswith("/api/"):
                # Cross-site requests cannot set a custom header without a CORS preflight, which is never granted.
                if method == "POST" and self.headers.get(CSRF_HEADER) != "dayone":
                    self._send_json(HTTPStatus.FORBIDDEN, _error("CSRF", "En-tête de requête manquant."))
                    return
                if method == "POST" and url.path == "/api/login":
                    self._login()
                    return
                if method == "POST" and url.path == "/api/logout":
                    api.auth.logout(self._session_token())
                    self._send_json(HTTPStatus.OK, {"ok": True}, cookie=self._cookie("", max_age=0))
                    return
                body = {}
                if method == "POST" and url.path == "/api/media":
                    body = self._read_bytes(MAX_PHOTO_BYTES)
                elif method == "POST":
                    body = self._read_json()
                if body is None:
                    return
                user = api.auth.user_for(self._session_token())
                status, payload = api.dispatch(method, url.path, parse_qs(url.query), body, user)
                self._send_json(status, payload)
            elif method == "GET" and url.path == "/export/visits.csv":
                user = api.auth.user_for(self._session_token())
                if user is None or user["role"] != "admin":
                    self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
                    return
                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=api.service.EXPORT_COLUMNS)
                writer.writeheader()
                writer.writerows(api.service.export_visits())
                self._send(HTTPStatus.OK, buffer.getvalue().encode("utf-8"), "text/csv; charset=utf-8")
            elif method == "GET" and url.path.startswith("/media/"):
                if api.auth.user_for(self._session_token()) is None:
                    self._send(HTTPStatus.UNAUTHORIZED, b"", "text/plain")
                    return
                self._send_media(unquote(url.path[len("/media/"):]))
            elif method == "GET":
                self._send_static(url.path)
            else:
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", "text/plain")

        def _whatsapp(self, method: str, url) -> None:
            """Meta Cloud API webhook: authenticated by the verify token (GET) or the app-secret signature (POST)."""
            channel = getattr(api.service, "whatsapp", None)
            if channel is None:
                self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return
            if method == "GET":
                query = parse_qs(url.query)
                challenge = channel.verify_subscription(*(query.get(k, [None])[0] for k in
                                                          ("hub.mode", "hub.verify_token", "hub.challenge")))
                if challenge is None:
                    self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")
                else:
                    self._send(HTTPStatus.OK, challenge.encode(), "text/plain")
                return
            body = self._read_bytes(MAX_BODY_BYTES)
            if body is None:
                return
            if not channel.signature_valid(body, self.headers.get("X-Hub-Signature-256")):
                self._send(HTTPStatus.UNAUTHORIZED, b"", "text/plain")
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._send(HTTPStatus.BAD_REQUEST, b"", "text/plain")
                return
            results = channel.handle_webhook(payload)
            log.info("whatsapp webhook: %s", [r["outcome"] for r in results])
            self._send_json(HTTPStatus.OK, {"ok": True})

        def _login(self) -> None:
            body = self._read_json()
            if body is None:
                return
            try:
                token, user = api.auth.login(body.get("username"), body.get("password"))
            except AuthError as exc:
                self._send_json(exc.status, _error(exc.code, exc.message))
                return
            log.info("login %s", user["username"])
            self._send_json(HTTPStatus.OK, user, cookie=self._cookie(token, max_age=SESSION_HOURS * 3600))

        def _cookie(self, value: str, *, max_age: int) -> str:
            flags = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            return flags + ("; Secure" if secure_cookies else "")

        def _read_bytes(self, limit: int):
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _error("BODY_TOO_LARGE", "Photo trop volumineuse."))
                return None
            return self.rfile.read(length) if length else b""

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _error("BODY_TOO_LARGE", "Requête trop volumineuse."))
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                body = None
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, _error("INVALID_JSON", "Corps JSON invalide."))
                return None
            return body

        def _send_json(self, status, payload, *, cookie: str | None = None) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json", cookie=cookie)

        def _send_media(self, media_ref: str) -> None:
            try:
                data = api.service.read_media(media_ref)
            except ServiceError:
                self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return
            # Private: patient pages must not sit in shared caches.
            self._send(HTTPStatus.OK, data, mimetypes.guess_type(media_ref)[0] or "application/octet-stream",
                       cache="private, max-age=600")

        def _send_static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (STATIC_DIR / name).resolve()
            if file.parent != STATIC_DIR.resolve() or not file.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
                return
            content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}.get(
                file.suffix, "application/octet-stream")
            self._send(HTTPStatus.OK, file.read_bytes(), f"{content_type}; charset=utf-8")

        def _send(self, status, data: bytes, content_type: str, *, cache: str = "no-store",
                  cookie: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            if secure_cookies:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            if cookie:
                self.send_header("Set-Cookie", cookie)
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
            if getattr(service, "whatsapp", None) is not None:
                service.whatsapp.deliver_pending()
        except Exception:
            log.exception("queue tick failed")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="DayOne maternal registry prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(REPO_ROOT / "var" / "dayone.sqlite3"))
    parser.add_argument("--window", type=int, default=8, help="multipage grouping window in seconds")
    parser.add_argument("--extractor", choices=("fixture", "tesseract", "paddleocr", "http"), default="fixture",
                        help="fixture for the stable demo; tesseract for local OCR of the specimen pages; "
                             "paddleocr for the slower PaddleOCR comparison; http for an external model service")
    parser.add_argument("--tls-cert", help="PEM certificate: serve HTTPS (with --tls-key)")
    parser.add_argument("--tls-key", help="PEM private key for --tls-cert")
    parser.add_argument("--add-user", metavar="NAME", help="create a back-office account, then exit")
    parser.add_argument("--role", choices=("reviewer", "admin"), default="reviewer", help="role for --add-user")
    parser.add_argument("--purge-media", type=int, metavar="DAYS",
                        help="delete uploaded photos of documents synced more than DAYS ago, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = build_service(args.db, grouping_window_seconds=args.window, extractor_mode=args.extractor)
    if args.add_user:
        import getpass
        user = service.auth.create_user(args.add_user, getpass.getpass(f"Mot de passe pour {args.add_user} : "), args.role)
        print(f"Compte créé : {user['username']} ({user['role']})")
        service.store.close()
        return
    if args.purge_media is not None:
        print(f"{service.purge_media(args.purge_media)} photo(s) supprimée(s).")
        service.store.close()
        return
    password = service.auth.ensure_admin()
    if password:
        print(f"Premier démarrage : compte « admin », mot de passe « {password} » (affiché une seule fois ; "
              "créez des comptes nominatifs avec --add-user).", flush=True)
    # Fixture mode includes a pre-confirmed history for the visual demo.  Real
    # OCR always requires a human review, so it must start with an empty demo.
    if args.extractor != "fixture":
        service.reset_demo(with_history=False)
    else:
        service.ensure_seed()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(Api(service), secure_cookies=bool(args.tls_cert)))
    scheme = "http"
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    stop = threading.Event()
    threading.Thread(target=_run_worker, args=(service, stop), daemon=True).start()
    print(f"DayOne prototype running at {scheme}://{args.host}:{args.port}/  (Ctrl+C to stop)", flush=True)
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
