"""WhatsApp Cloud API adapter against a local fake of the Graph API. No credentials, no real network, no real messages."""

import base64
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from dayone.extraction import FixtureExtractor
from dayone.server import Api, _run_worker, make_handler, make_webhook_handler
from dayone.server import main as server_main
from dayone.service import DayOneService
from dayone.store import Store
from dayone.whatsapp import (CloudConfig, ConfigError, WhatsAppCloud, discard_held, hold_outbound, load_config,
                             load_env_file, register_sender, RESEND_PHOTO_TEXT)

from tests.test_flow import COVER, REPO_ROOT, REVIEWER, FlowTestCase

TOKEN = "test-access-token-not-real"
APP_SECRET = "test-app-secret-not-real"
VERIFY_TOKEN = "test-verify-token-not-real"
PHONE_NUMBER_ID = "106540352242922"
PHONE = "212611112222"
CLOUD_SENDER = "whatsapp:+212611112222"
JPEG = (REPO_ROOT / "data" / "Paper Registry" / "1-1.jpg").read_bytes()
JPEG_2 = (REPO_ROOT / "data" / "Paper Registry" / "1-4.jpg").read_bytes()
PNG = (REPO_ROOT / "data" / "Paper Registry" / "dossiers_specimen_10_patientes-01.png").read_bytes()
STORED_NAME = re.compile(r"[0-9a-f]{64}\.(?:jpg|png)")
POLL = 0.02

logging.getLogger("dayone").addHandler(logging.NullHandler())


def serve(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": POLL}, daemon=True).start()
    return server


def b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


class FakeMeta:
    """Plays the documented Graph API responses: media metadata, authenticated download, send message."""

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.requests: list[dict] = []
        self.media: dict[str, dict] = {}
        self.download_failures: dict[str, list[int]] = {}
        self.redirects: dict[str, str] = {}
        self.delay_seconds = 0.0
        self.send_script: list[tuple[int, dict]] = []
        self.sent: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake._get(self)

            def do_POST(self):
                fake._post(self)

            def log_message(self, *args):
                pass

        self.server = serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        self.server.handle_error = lambda request, client_address: None
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _reply(handler, status: int, body: bytes, content_type: str = "application/json", headers: dict | None = None):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body)

    def _record(self, handler, body=None):
        self.requests.append({"method": handler.command, "path": handler.path,
                              "authorization": handler.headers.get("Authorization"), "body": body})

    def _get(self, handler):
        self._record(handler)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if handler.headers.get("Authorization") != f"Bearer {self.token}":
            self._reply(handler, 401, json.dumps({"error": {"code": 190}}).encode())
            return
        url = urlparse(handler.path)
        parts = url.path.strip("/").split("/")
        if parts[0] == "download":
            media_id = parts[1]
            if self.redirects.get(media_id):
                self._reply(handler, 302, b"", headers={"Location": self.redirects[media_id]})
                return
            failures = self.download_failures.get(media_id)
            if failures:
                self._reply(handler, failures.pop(0), b"{}")
                return
            item = self.media[media_id]
            self._reply(handler, 200, item["data"], item.get("served_type", item["mime"]))
            return
        item = self.media.get(parts[1]) if len(parts) == 2 else None
        if item is None:
            self._reply(handler, 404, json.dumps({"error": {"code": 100}}).encode())
            return
        self._reply(handler, 200, json.dumps({
            "messaging_product": "whatsapp", "url": f"{self.base_url}/download/{parts[1]}", "mime_type": item["mime"],
            "sha256": b64_sha256(item["data"]), "file_size": str(item.get("file_size", len(item["data"]))),
            "id": parts[1],
        }).encode())

    def _post(self, handler):
        body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        self._record(handler, body)
        if handler.headers.get("Authorization") != f"Bearer {self.token}":
            self._reply(handler, 401, json.dumps({"error": {"code": 190}}).encode())
            return
        if self.send_script:
            status, payload = self.send_script.pop(0)
            self._reply(handler, status, json.dumps(payload).encode())
            return
        self.sent.append(body)
        self._reply(handler, 200, json.dumps({
            "messaging_product": "whatsapp", "contacts": [{"input": body["to"], "wa_id": body["to"].lstrip("+")}],
            "messages": [{"id": f"wamid.out-{len(self.sent)}"}],
        }).encode())


class CloudTestCase(FlowTestCase):
    def setUp(self):
        proxy_env = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        proxy_env.start()
        self.addCleanup(proxy_env.stop)
        super().setUp()
        self.meta = FakeMeta()
        self.addCleanup(self.meta.close)
        self.adapter = self.make_adapter()
        register_sender(self.service.store, phone="+" + PHONE, facility_id="FAC-SIDI-SMAIL", label="Sage-femme test")
        self.wamid_number = 0

    @property
    def media_dir(self) -> Path:
        return (Path(self.tmp.name) / "media").resolve()

    def make_service(self) -> DayOneService:
        return DayOneService(Store(self.db_path), FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                             grouping_window_seconds=8, clock=self.clock, inbound_media_dir=self.media_dir)

    def make_adapter(self, **overrides) -> WhatsAppCloud:
        settings = dict(verify_token=VERIFY_TOKEN, app_secret=APP_SECRET, access_token=TOKEN,
                        phone_number_id=PHONE_NUMBER_ID, api_version="v26.0", media_dir=self.media_dir,
                        graph_base_url=self.meta.base_url, http_timeout=2.0)
        settings.update(overrides)
        return WhatsAppCloud(self.service, CloudConfig(**settings))

    # -- payloads in the documented shape

    def image(self, media_id: str, data: bytes = JPEG, *, mime: str = "image/jpeg", context_id: str | None = None,
              phone: str = PHONE, wamid: str | None = None, sha256: str | None = None, **media_extra) -> dict:
        self.meta.media[media_id] = {"data": data, "mime": mime, **media_extra}
        if wamid is None:
            self.wamid_number += 1
            wamid = f"wamid.in-{self.wamid_number}"
        message = {"from": phone, "id": wamid, "timestamp": "1759514400", "type": "image",
                   "image": {"mime_type": mime, "sha256": sha256 or b64_sha256(data), "id": media_id}}
        if context_id:
            message["context"] = {"from": "15550783881", "id": context_id}
        return message

    @staticmethod
    def change(*, messages=(), statuses=(), phone_number_id: str = PHONE_NUMBER_ID) -> dict:
        value = {"messaging_product": "whatsapp",
                 "metadata": {"display_phone_number": "15550783881", "phone_number_id": phone_number_id}}
        if messages:
            value["contacts"] = [{"profile": {"name": "Sage-femme"}, "wa_id": PHONE}]
            value["messages"] = list(messages)
        if statuses:
            value["statuses"] = list(statuses)
        return {"value": value, "field": "messages"}

    def payload(self, *changes: dict) -> dict:
        return {"object": "whatsapp_business_account",
                "entry": [{"id": "102290129340398", "changes": [change]} for change in changes]}

    @staticmethod
    def status(wa_message_id: str, status: str, error_code: int | None = None) -> dict:
        item = {"id": wa_message_id, "status": status, "timestamp": "1759514500", "recipient_id": PHONE}
        if error_code is not None:
            item["errors"] = [{"code": error_code, "title": "t", "message": "t", "error_data": {"details": "d"}}]
        return item

    def receive(self, *messages: dict) -> dict:
        return self.adapter.record_webhook(self.payload(self.change(messages=messages)))

    # -- reads

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.service.store.read() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def inbound(self) -> list[dict]:
        return self.rows("SELECT * FROM whatsapp_inbound ORDER BY inbound_id")

    def outbound(self) -> list[dict]:
        return self.rows("SELECT o.*, m.body FROM whatsapp_outbound o JOIN messages m ON m.id = o.message_id "
                         "ORDER BY o.message_id")

    def cloud_pages(self) -> list[dict]:
        return self.rows("SELECT * FROM pages WHERE sender_id = ? ORDER BY page_id", (CLOUD_SENDER,))

    def cloud_document(self) -> tuple[str, str]:
        """A real-photo document: no fixture matches, so it goes through manual entry like any unknown page set."""
        self.receive(self.image("media-1", wamid="wamid.in-photo"))
        self.adapter.process_inbound()
        self.adapter.deliver_outbound()
        self.wait_for_draft()
        page = self.cloud_pages()[0]
        self.service.start_manual_entry(page["document_id"], reviewer=REVIEWER)
        return page["document_id"], page["page_id"]


class WebhookEndpointTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.webhook = serve(ThreadingHTTPServer(("127.0.0.1", 0), make_webhook_handler(self.adapter)))
        self.addCleanup(self.webhook.server_close)
        self.addCleanup(self.webhook.shutdown)

    def call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.webhook.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def post(self, payload: dict, *, secret: str = APP_SECRET, tamper: bool = False, sign: bool = True):
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if sign:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if tamper:
            body = body.replace(b"212611112222", b"212699999999")
        return self.call("POST", "/webhooks/whatsapp", body, headers)

    def test_verification_echoes_challenge_only_with_the_configured_token(self):
        ok = self.call("GET", f"/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=1158201444"
                              f"&hub.verify_token={VERIFY_TOKEN}")
        self.assertEqual(ok, (200, b"1158201444"))
        for query in (f"hub.mode=subscribe&hub.challenge=1&hub.verify_token=wrong",
                      f"hub.challenge=1&hub.verify_token={VERIFY_TOKEN}",
                      f"hub.mode=subscribe&hub.challenge=%3Cscript%3E&hub.verify_token={VERIFY_TOKEN}"):
            self.assertEqual(self.call("GET", f"/webhooks/whatsapp?{query}")[0], 403, query)

    def test_signature_is_checked_on_raw_bytes_before_parsing(self):
        event = self.payload(self.change(messages=[self.image("media-1")]))
        self.assertEqual(self.post(event, sign=False)[0], 401)
        self.assertEqual(self.post(event, secret="another-secret")[0], 401)
        self.assertEqual(self.post(event, tamper=True)[0], 401)
        not_json = b"{not json"
        self.assertEqual(self.call("POST", "/webhooks/whatsapp", not_json,
                                   {"X-Hub-Signature-256": "sha256=" + "0" * 64})[0], 401)
        self.assertEqual(self.inbound(), [])

    def test_signed_event_is_persisted_before_any_meta_call_or_acknowledgment(self):
        status, _ = self.post(self.payload(self.change(messages=[self.image("media-1")])))
        self.assertEqual(status, 200)
        [job] = self.inbound()
        self.assertEqual((job["status"], job["sender_id"], job["media_id"]), ("PENDING", CLOUD_SENDER, "media-1"))
        self.assertEqual(self.meta.requests, [])
        self.assertEqual(self.cloud_pages(), [])
        self.assertEqual(self.service.thread(CLOUD_SENDER), [])

    def test_listener_serves_only_the_webhook_path(self):
        for method, path in (("GET", "/"), ("GET", "/api/documents"), ("GET", "/api/patients"),
                             ("POST", "/api/demo/reset"), ("POST", "/api/whatsapp/messages"),
                             ("GET", "/media/data/Paper%20Registry/1-1.jpg")):
            self.assertEqual(self.call(method, path, b"{}" if method == "POST" else None)[0], 404, path)
        self.assertEqual(len(self.service.list_patients()), 1)

    def test_oversized_body_is_refused_unread(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.webhook.server_address[1], timeout=5)
        connection.putrequest("POST", "/webhooks/whatsapp")
        connection.putheader("Content-Length", str(3 * 1024 * 1024 + 1))
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)
        connection.close()

    def test_sandbox_relay_forwards_only_the_webhook_and_forces_one_redelivery(self):
        spec = importlib.util.spec_from_file_location("webhook_relay", REPO_ROOT / "tools" / "webhook_relay.py")
        relay_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relay_module)
        relay = serve(ThreadingHTTPServer(("127.0.0.1", 0), relay_module.make_handler(
            f"http://127.0.0.1:{self.webhook.server_address[1]}", fail_first=1)))
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)

        def via_relay(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
            connection = http.client.HTTPConnection("127.0.0.1", relay.server_address[1], timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        verify = f"/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=42&hub.verify_token={VERIFY_TOKEN}"
        self.assertEqual(via_relay("GET", verify), (200, b"42"))
        self.assertEqual(via_relay("GET", "/api/documents")[0], 404)
        body = json.dumps(self.payload(self.change(messages=[self.image("media-1")]))).encode()
        headers = {"Content-Type": "application/json",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()}
        self.assertEqual(via_relay("POST", "/webhooks/whatsapp", body, headers)[0], 503)
        self.assertEqual([job["status"] for job in self.inbound()], ["PENDING"])
        self.assertEqual(via_relay("POST", "/webhooks/whatsapp", body, headers)[0], 200)
        self.assertEqual(len(self.inbound()), 1)

    def test_back_office_listener_has_no_webhook_route(self):
        server = serve(ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(self.service))))
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("GET", f"/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=1"
                                      f"&hub.verify_token={VERIFY_TOKEN}")
            self.assertEqual(connection.getresponse().status, 404)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()


class InboundTest(CloudTestCase):
    def test_batch_separates_images_statuses_other_messages_and_other_numbers(self):
        text = {"from": PHONE, "id": "wamid.text-1", "timestamp": "1759514400", "type": "text", "text": {"body": "fin"}}
        summary = self.adapter.record_webhook(self.payload(
            self.change(messages=[self.image("media-1"), text]),
            self.change(statuses=[self.status("wamid.unknown", "delivered")]),
            self.change(messages=[self.image("media-2")], phone_number_id="999"),
        ))
        self.assertEqual((summary["queued"], summary["ignored"], summary["statuses"], summary["skipped"]), (1, 1, 0, 1))
        self.assertEqual([(row["message_type"], row["status"]) for row in self.inbound()],
                         [("image", "PENDING"), ("text", "IGNORED")])

    def test_image_is_downloaded_with_the_token_and_enters_the_review_workflow(self):
        self.receive(self.image("media-1", wamid="wamid.in-photo"))
        self.assertEqual(self.adapter.process_inbound(), 1)

        media_call, download_call = self.meta.requests
        self.assertEqual(urlparse(media_call["path"]).path, "/v26.0/media-1")
        self.assertEqual(parse_qs(urlparse(media_call["path"]).query), {"phone_number_id": [PHONE_NUMBER_ID]})
        self.assertEqual({media_call["authorization"], download_call["authorization"]}, {f"Bearer {TOKEN}"})

        [page] = self.cloud_pages()
        name = page["media_ref"].removeprefix("whatsapp-media/")
        self.assertRegex(name, STORED_NAME)
        self.assertEqual((self.media_dir / name).read_bytes(), JPEG)
        self.assertEqual(page["source_message_id"], "wamid.in-photo")
        self.assertEqual(self.service.resolve_media(page["media_ref"]).read_bytes(), JPEG)
        self.assertEqual(self.inbound()[0]["status"], "DONE")

        thread = self.service.thread(CLOUD_SENDER)
        self.assertEqual([m["body"] for m in thread if m["direction"] == "OUT"],
                         ["Reçu : page 1. Merci, le traitement est en cours."])
        self.assertEqual(self.adapter.deliver_outbound(), 1)
        [sent] = self.meta.sent
        self.assertEqual(sent, {"messaging_product": "whatsapp", "recipient_type": "individual",
                                "to": "+212611112222", "type": "text",
                                "text": {"body": "Reçu : page 1. Merci, le traitement est en cours."}})
        self.assertEqual((self.outbound()[0]["status"], self.outbound()[0]["wa_message_id"]), ("SENT", "wamid.out-1"))

        self.wait_for_draft()
        document = self.service.get_document(page["document_id"])
        self.assertEqual(document["document"]["status"], "PROCESSING_FAILED")
        manual = self.service.start_manual_entry(page["document_id"], reviewer=REVIEWER)
        self.assertEqual(manual["document"]["status"], "NEEDS_REVIEW")

    def test_redelivered_webhooks_and_reprocessing_create_one_page_and_one_acknowledgment(self):
        event = self.image("media-1", wamid="wamid.in-photo")
        self.receive(event)
        self.assertEqual(self.receive(event)["duplicates"], 1)
        self.adapter.process_inbound()
        with self.service.store.tx() as db:  # crash after ingest, before the job was marked done
            db.execute("UPDATE whatsapp_inbound SET status = 'PENDING', next_attempt_at = ?", (self.clock.now.isoformat(),))
        self.adapter.process_inbound()
        self.adapter.deliver_outbound()
        self.assertEqual(len(self.cloud_pages()), 1)
        self.assertEqual(len(self.outbound()), 1)
        self.assertEqual(len(self.meta.sent), 1)

    def test_unknown_sender_media_is_never_downloaded_nor_its_number_kept(self):
        self.receive(self.image("media-1", phone="212600000777"))
        self.adapter.process_inbound()
        [job] = self.inbound()
        self.assertEqual((job["status"], job["sender_id"], job["media_id"]), ("REJECTED", None, None))
        self.assertEqual(self.meta.requests, [])

    def test_transient_download_failure_retries_later_without_acknowledging(self):
        self.receive(self.image("media-1"))
        self.meta.download_failures["media-1"] = [503]
        self.adapter.process_inbound()
        [job] = self.inbound()
        self.assertEqual((job["status"], job["attempts"], job["last_error"]), ("PENDING", 1, "HTTP_503"))
        self.assertEqual((self.cloud_pages(), self.service.thread(CLOUD_SENDER)), ([], []))

        requests = len(self.meta.requests)
        self.adapter.process_inbound()
        self.assertEqual(len(self.meta.requests), requests, "retried before its backoff")
        self.clock.advance(31)
        self.adapter.process_inbound()
        self.assertEqual(self.inbound()[0]["status"], "DONE")
        self.assertEqual(len(self.cloud_pages()), 1)

    def test_expired_media_url_is_requeried(self):
        self.receive(self.image("media-1"))
        self.meta.download_failures["media-1"] = [404]
        self.adapter.process_inbound()
        self.assertEqual(self.inbound()[0]["status"], "PENDING")

    def test_hash_mismatch_is_retried_not_stored(self):
        self.receive(self.image("media-1", sha256=b64_sha256(b"something else")))
        self.adapter.process_inbound()
        [job] = self.inbound()
        self.assertEqual((job["status"], job["last_error"]), ("PENDING", "SHA256_MISMATCH"))
        self.assertFalse(self.media_dir.exists() and any(self.media_dir.iterdir()))

    def test_media_that_is_too_large_or_not_an_image_is_refused_and_the_midwife_asked_again(self):
        cases = {
            "declared-too-large": dict(file_size=6 * 1024 * 1024),
            "png-label-jpeg-bytes": dict(mime="image/png"),
            "webp": dict(mime="image/webp"),
        }
        for media_id, extra in cases.items():
            self.receive(self.image(media_id, **extra))
        self.adapter.process_inbound()
        self.assertEqual([(row["media_id"], row["status"], row["last_error"]) for row in self.inbound()], [
            ("declared-too-large", "FAILED", "TOO_LARGE"),
            ("png-label-jpeg-bytes", "FAILED", "NOT_AN_IMAGE"),
            ("webp", "FAILED", "UNSUPPORTED_MEDIA_TYPE"),
        ])
        self.assertEqual([row["body"] for row in self.outbound()], [RESEND_PHOTO_TEXT] * 3)
        self.assertEqual(self.cloud_pages(), [])
        self.assertFalse(self.media_dir.exists() and any(self.media_dir.iterdir()))

    def test_download_is_bounded_even_when_the_declared_size_lies(self):
        self.adapter = self.make_adapter(max_media_bytes=50_000)
        self.receive(self.image("media-1", file_size=1000))
        self.adapter.process_inbound()
        self.assertEqual((self.inbound()[0]["status"], self.inbound()[0]["last_error"]), ("FAILED", "TOO_LARGE"))

    def test_slow_meta_hits_the_timeout_and_is_retried(self):
        self.adapter = self.make_adapter(http_timeout=0.3)
        self.meta.delay_seconds = 1.0
        self.receive(self.image("media-1"))
        self.adapter.process_inbound()
        self.assertEqual((self.inbound()[0]["status"], self.inbound()[0]["last_error"]), ("PENDING", "NETWORK"))

    def test_media_id_never_reaches_a_path_or_filename(self):
        self.receive(self.image("../../etc/passwd"))
        self.adapter.process_inbound()
        self.assertEqual((self.inbound()[0]["status"], self.inbound()[0]["last_error"]), ("FAILED", "BAD_MEDIA_ID"))
        self.assertEqual(self.meta.requests, [])

    def test_token_is_dropped_on_a_redirect_to_another_host(self):
        other = FakeMeta(token="never-sent")
        self.addCleanup(other.close)
        other.media["media-1"] = {"data": JPEG, "mime": "image/jpeg"}
        self.receive(self.image("media-1"))
        self.meta.redirects["media-1"] = f"{other.base_url}/download/media-1"
        self.adapter.process_inbound()
        self.assertEqual([r["authorization"] for r in other.requests], [None])


class OutboundTest(CloudTestCase):
    def test_held_messages_survive_restarts_and_are_never_sent(self):
        hold_outbound(self.service.store)
        self.receive(self.image("media-1"))
        self.adapter.process_inbound()
        self.assertEqual(self.adapter.deliver_outbound(), 0)
        self.assertEqual([row["status"] for row in self.outbound()], ["PENDING"])

        environ = {"WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN, "WHATSAPP_APP_SECRET": APP_SECRET,
                   "WHATSAPP_ACCESS_TOKEN": TOKEN, "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
                   "WHATSAPP_API_VERSION": "v26.0", "WHATSAPP_MEDIA_DIR": str(self.media_dir)}
        with mock.patch.dict(os.environ, environ), mock.patch("sys.stderr", io.StringIO()) as stderr, \
                self.assertRaises(SystemExit):
            server_main(["--db", str(self.db_path), "--whatsapp-mode", "cloud"])
        self.assertIn("--hold-outbound", stderr.getvalue())
        self.assertEqual(self.make_adapter().deliver_outbound(), 0)

        self.assertEqual(discard_held(self.service.store), 1)
        self.assertEqual(self.adapter.deliver_outbound(), 0)
        [held] = self.outbound()
        self.assertEqual((held["status"], held["last_error"], held["wa_message_id"]), ("FAILED", "DISCARDED_HELD", None))
        self.assertEqual(self.meta.sent, [])

    def test_transient_send_failure_is_retried_without_touching_review(self):
        document_id, page_id = self.cloud_document()
        before = self.service.get_document(document_id)["document"]
        self.service.request_retake(page_id, reviewer=REVIEWER)
        revision = self.service.get_document(document_id)["document"]["revision"]

        self.meta.send_script.append((503, {"error": {"code": 131016}}))
        self.adapter.deliver_outbound()
        retake = self.outbound()[-1]
        self.assertEqual((retake["status"], retake["attempts"], retake["last_error"]), ("PENDING", 1, "HTTP_503_131016"))
        self.clock.advance(31)
        self.adapter.deliver_outbound()
        retake = self.outbound()[-1]
        self.assertEqual((retake["status"], retake["attempts"]), ("SENT", 2))
        self.assertEqual(self.meta.sent[-1]["text"]["body"], "Merci de reprendre la photo de la page 1.")
        self.assertEqual(self.meta.sent[-1]["context"], {"message_id": "wamid.in-photo"})

        after = self.service.get_document(document_id)
        self.assertEqual((after["document"]["status"], after["document"]["revision"], after["next_step"]),
                         (before["status"], revision, "WAITING_RETAKE"))

    def test_permanent_send_failure_is_not_retried(self):
        self.receive(self.image("media-1"))
        self.adapter.process_inbound()
        self.meta.send_script.append((400, {"error": {"code": 131047}}))
        self.adapter.deliver_outbound()
        self.assertEqual((self.outbound()[0]["status"], self.outbound()[0]["last_error"]), ("FAILED", "HTTP_400_131047"))
        self.clock.advance(7200)
        requests = len(self.meta.requests)
        self.adapter.deliver_outbound()
        self.assertEqual(len(self.meta.requests), requests)

    def test_status_webhooks_advance_delivery_and_failed_status_requeues_only_transient_codes(self):
        self.receive(self.image("media-1"), self.image("media-2"))
        self.clock.advance(20)
        self.adapter.process_inbound()
        self.adapter.deliver_outbound()
        first, second = (row["wa_message_id"] for row in self.outbound())
        self.adapter.record_webhook(self.payload(self.change(statuses=[
            self.status(first, "delivered"), self.status(first, "sent"), self.status(second, "failed", 131000)])))
        self.assertEqual([row["status"] for row in self.outbound()], ["DELIVERED", "PENDING"])
        self.adapter.record_webhook(self.payload(self.change(statuses=[self.status(first, "read")])))
        self.clock.advance(31)
        self.adapter.deliver_outbound()
        resent = self.outbound()[1]["wa_message_id"]
        self.adapter.record_webhook(self.payload(self.change(statuses=[self.status(resent, "failed", 131047)])))
        self.assertEqual([row["status"] for row in self.outbound()], ["READ", "FAILED"])

    def test_simulator_senders_never_queue_cloud_messages(self):
        document_id = self.send(COVER)
        self.wait_for_draft()
        page_id = self.service.get_document(document_id)["pages"][0]["page_id"]
        self.service.request_retake(page_id, reviewer=REVIEWER)
        self.assertEqual(self.outbound(), [])

    def test_reply_quoting_the_retake_request_replaces_the_page(self):
        document_id, page_id = self.cloud_document()
        request = self.service.request_retake(page_id, reviewer=REVIEWER)
        self.adapter.deliver_outbound()
        request_wamid = self.outbound()[-1]["wa_message_id"]

        self.receive(self.image("media-2", JPEG_2, context_id=request_wamid, wamid="wamid.in-retake"))
        self.adapter.process_inbound()
        with self.service.store.read() as db:
            row = db.execute("SELECT * FROM retake_requests WHERE request_id = ?", (request["request_id"],)).fetchone()
        self.assertEqual((row["status"], row["replacement_message_id"]), ("FULFILLED", "wamid.in-retake"))
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "PENDING_AI")
        self.assertEqual(len(detail["pages"]), 1)
        self.assertEqual(self.outbound()[-1]["body"],
                         "Reçu : nouvelle photo de la page 1. Merci, le traitement est en cours.")

    def test_photo_without_quote_or_for_a_closed_request_becomes_a_normal_page(self):
        document_id, page_id = self.cloud_document()
        request = self.service.request_retake(page_id, reviewer=REVIEWER)
        self.adapter.deliver_outbound()
        request_wamid = self.outbound()[-1]["wa_message_id"]

        self.receive(self.image("media-2", JPEG_2))
        self.adapter.process_inbound()
        self.assertEqual(self.service.get_document(document_id)["next_step"], "WAITING_RETAKE")

        self.service.cancel_retake(request["request_id"], reviewer=REVIEWER)
        self.clock.advance(20)
        self.receive(self.image("media-3", PNG, mime="image/png", context_id=request_wamid))
        self.adapter.process_inbound()
        self.assertEqual(len(self.cloud_pages()), 3)
        self.assertEqual([row["status"] for row in self.inbound()], ["DONE", "DONE", "DONE"])


class ConfigAndPrivacyTest(CloudTestCase):
    def test_config_lists_missing_names_and_never_values(self):
        with self.assertRaises(ConfigError) as raised:
            load_config({"WHATSAPP_APP_SECRET": APP_SECRET}, REPO_ROOT)
        message = str(raised.exception)
        self.assertIn("WHATSAPP_ACCESS_TOKEN", message)
        self.assertNotIn("WHATSAPP_APP_SECRET", message)
        self.assertNotIn(APP_SECRET, message)

        env = {"WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN, "WHATSAPP_APP_SECRET": APP_SECRET, "WHATSAPP_ACCESS_TOKEN": TOKEN,
               "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID, "WHATSAPP_API_VERSION": "v26.0"}
        config = load_config(env, REPO_ROOT)
        self.assertEqual((config.api_version, config.graph_base_url), ("v26.0", "https://graph.facebook.com"))
        for secret in (TOKEN, APP_SECRET, VERIFY_TOKEN):
            self.assertNotIn(secret, repr(config))
        for bad in ({"WHATSAPP_API_VERSION": "26"}, {"WHATSAPP_PHONE_NUMBER_ID": "+212600000000"},
                    {"WHATSAPP_GRAPH_BASE_URL": "http://graph.example.com"}, {"WHATSAPP_MAX_MEDIA_BYTES": "lots"}):
            with self.assertRaises(ConfigError):
                load_config({**env, **bad}, REPO_ROOT)

    def test_held_outbound_worker_processes_inbound_but_never_sends(self):
        calls: list[str] = []

        class Adapter:
            def process_inbound(self):
                calls.append("inbound")

            def deliver_outbound(self):
                calls.append("outbound")

        stop = threading.Event()
        worker = threading.Thread(target=_run_worker, args=(self.service, stop, Adapter()),
                                  kwargs={"interval": POLL, "send_outbound": False})
        worker.start()
        time.sleep(POLL * 10)
        stop.set()
        worker.join(2)
        self.assertIn("inbound", calls)
        self.assertNotIn("outbound", calls)

    def test_hybrid_tool_webhook_is_accepted_and_the_tool_refuses_unsafe_targets(self):
        spec = importlib.util.spec_from_file_location("hybrid_media_test", REPO_ROOT / "tools" / "hybrid_media_test.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.meta.media["media-9"] = {"data": PNG, "mime": "image/png"}
        body = tool.build_webhook(PHONE_NUMBER_ID, PHONE, "media-9", "image/png", PNG, "wamid.SIMULATED-1", 1759514400)
        self.assertTrue(self.adapter.signature_valid(body, tool.sign(APP_SECRET, body)))
        self.assertEqual(self.adapter.record_webhook(json.loads(body))["queued"], 1)
        self.adapter.process_inbound()
        [page] = self.cloud_pages()
        self.assertEqual(page["media_sha256"], hashlib.sha256(PNG).hexdigest())

        image = Path(self.tmp.name) / "specimen.png"
        image.write_bytes(PNG)
        env_file = Path(self.tmp.name) / "hybrid.env"
        env_file.write_text(f"WHATSAPP_VERIFY_TOKEN={VERIFY_TOKEN}\nWHATSAPP_APP_SECRET={APP_SECRET}\n"
                            f"WHATSAPP_ACCESS_TOKEN={TOKEN}\nWHATSAPP_PHONE_NUMBER_ID={PHONE_NUMBER_ID}\n"
                            f"WHATSAPP_API_VERSION=v26.0\nWHATSAPP_GRAPH_BASE_URL={self.meta.base_url}\n", encoding="utf-8")
        common = ["--image", str(image), "--db", str(self.db_path), "--env-file", str(env_file)]
        for extra, reason in ((["--webhook-url", "https://example.org/webhooks/whatsapp"], "--webhook-url"),
                              ([], "--hold-outbound")):
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stderr", io.StringIO()) as stderr, \
                    self.assertRaises(SystemExit):
                tool.main(common + extra)
            self.assertIn(reason, stderr.getvalue())
        self.assertFalse([r for r in self.meta.requests if r.get("method") == "POST"])

    def test_unfilled_env_example_is_refused(self):
        environ: dict[str, str] = {}
        load_env_file(REPO_ROOT / ".env.example", environ)
        with self.assertRaises(ConfigError) as raised:
            load_config(environ, REPO_ROOT)
        for name in ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"):
            self.assertIn(name, str(raised.exception))
        self.assertNotIn("WHATSAPP_API_VERSION", str(raised.exception))

    def test_env_file_does_not_override_the_environment(self):
        path = Path(self.tmp.name) / ".env"
        path.write_text("# comment\nWHATSAPP_API_VERSION=v26.0\nWHATSAPP_ACCESS_TOKEN='from-file'\nbad line\n",
                        encoding="utf-8")
        environ = {"WHATSAPP_ACCESS_TOKEN": "from-environment"}
        self.assertEqual(load_env_file(path, environ), ["WHATSAPP_API_VERSION"])
        self.assertEqual(environ, {"WHATSAPP_ACCESS_TOKEN": "from-environment", "WHATSAPP_API_VERSION": "v26.0"})

    def test_logs_never_contain_tokens_signatures_sender_numbers_or_image_data(self):
        records: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Collect(level=logging.DEBUG)
        logger = logging.getLogger("dayone")
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, previous)

        webhook = serve(ThreadingHTTPServer(("127.0.0.1", 0), make_webhook_handler(self.adapter)))
        port = webhook.server_address[1]
        body = json.dumps(self.payload(self.change(messages=[self.image("media-1")]))).encode()
        signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        try:
            for method, path, data, headers in (
                ("GET", f"/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=7&hub.verify_token={VERIFY_TOKEN}", None, {}),
                ("GET", "/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=7&hub.verify_token=guess", None, {}),
                ("POST", "/webhooks/whatsapp", body, {"X-Hub-Signature-256": "sha256=" + "f" * 64}),
                ("POST", "/webhooks/whatsapp", body, {"X-Hub-Signature-256": signature}),
                ("PUT", f"/webhooks/whatsapp?hub.verify_token={VERIFY_TOKEN}", None, {}),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(method, path, body=data, headers=headers)
                connection.getresponse().read()
                connection.close()
        finally:
            webhook.shutdown()
            webhook.server_close()
        self.meta.send_script.append((503, {"error": {"code": 131016}}))
        self.adapter.process_inbound()
        self.adapter.deliver_outbound()
        self.receive(self.image("media-2", file_size=10 * 1024 * 1024))
        self.adapter.process_inbound()

        text = "\n".join(records)
        self.assertTrue(records)
        for forbidden in (TOKEN, APP_SECRET, VERIFY_TOKEN, PHONE, signature[7:], b64_sha256(JPEG), "Bearer"):
            self.assertNotIn(forbidden, text)

    def test_old_database_gains_the_sender_channel_column(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("CREATE TABLE facilities (facility_id TEXT PRIMARY KEY, name TEXT NOT NULL);"
                             "CREATE TABLE senders (sender_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, label TEXT NOT NULL);"
                             "INSERT INTO facilities VALUES ('F', 'f'); INSERT INTO senders VALUES ('whatsapp:+1', 'F', 'x');")
        db.close()
        store = Store(path)
        try:
            with store.read() as conn:
                row = conn.execute("SELECT channel FROM senders").fetchone()
            self.assertEqual(row["channel"], "SIMULATOR")
        finally:
            store.close()
