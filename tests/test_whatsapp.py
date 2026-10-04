import hashlib
import hmac
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dayone.crypto import Cipher
from dayone.extraction import FixtureExtractor
from dayone.media import MediaStore
from dayone.service import DayOneService
from dayone.store import Store
from dayone.whatsapp import NOT_A_PHOTO_REPLY, WhatsAppChannel, WhatsAppConfig
from tests.test_flow import FlowTestCase
from tests.test_media_retake import photo

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = WhatsAppConfig(verify_token="verify-me", app_secret="app-secret", access_token="token",
                        phone_number_id="1234", graph_url="https://graph.test/v21.0")
NUMBER = "212600000001"  # the seeded demo sender


class FakeGraph:
    def __init__(self, image: bytes):
        self.image = image
        self.calls = []
        self.fail_sends = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if headers.get("Authorization") != "Bearer token":
            return 401, b"{}"
        if url == "https://graph.test/v21.0/MEDIA-1":
            return 200, json.dumps({"url": "https://lookaside.test/MEDIA-1"}).encode()
        if url == "https://lookaside.test/MEDIA-1":
            return 200, self.image
        if url == "https://graph.test/v21.0/1234/messages":
            if self.fail_sends:
                self.fail_sends -= 1
                return 500, b"{}"
            return 200, json.dumps({"messages": [{"id": f"wamid.out-{len(self.calls)}"}]}).encode()
        return 404, b"{}"


def image_message(message_id="wamid.in-1", sender=NUMBER, media="MEDIA-1"):
    return {"from": sender, "id": message_id, "timestamp": "1791043200", "type": "image",
            "image": {"id": media, "mime_type": "image/jpeg"}}


def webhook(*messages):
    return {"object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {"messages": list(messages)}}]}]}


class WhatsAppTest(FlowTestCase):
    def make_service(self) -> DayOneService:
        service = DayOneService(Store(self.db_path), FixtureExtractor(REPO_ROOT / "fixtures"), REPO_ROOT,
                                grouping_window_seconds=8, clock=self.clock,
                                media=MediaStore(Path(self.tmp.name) / "media", Cipher(bytes(32))))
        service.outbound_enabled = True
        return service

    def setUp(self):
        super().setUp()
        self.graph = FakeGraph(photo())
        self.channel = WhatsAppChannel(CONFIG, self.service, transport=self.graph)

    def test_subscription_handshake_needs_the_verify_token(self):
        self.assertEqual(self.channel.verify_subscription("subscribe", "verify-me", "42"), "42")
        self.assertIsNone(self.channel.verify_subscription("subscribe", "wrong", "42"))

    def test_image_is_downloaded_encrypted_and_ingested_once(self):
        first = self.channel.handle_webhook(webhook(image_message()))
        self.assertEqual(first[0]["outcome"], "PAGE")
        replay = self.channel.handle_webhook(webhook(image_message()))
        self.assertEqual(replay[0]["outcome"], "DUPLICATE")
        page = self.service.get_document(first[0]["document_id"])["pages"][0]
        self.assertTrue(page["media_ref"].startswith("upload/"))
        self.assertEqual(page["captured_at"], "2026-10-03T16:00:00+00:00")
        stored = next((Path(self.tmp.name) / "media").iterdir()).read_bytes()
        self.assertNotIn(self.graph.image[:32], stored)

    def test_unknown_numbers_are_refused_and_text_gets_a_reply(self):
        with self.assertLogs("dayone.whatsapp", "WARNING"):
            refused = self.channel.handle_webhook(webhook(image_message(sender="212699999999")))
        self.assertEqual((refused[0]["outcome"], refused[0]["code"]), ("REFUSED", "UNKNOWN_SENDER"))
        result = self.channel.handle_webhook(webhook({"from": NUMBER, "id": "wamid.t1", "type": "text",
                                                      "text": {"body": "bonjour"}}))
        self.assertEqual(result[0]["outcome"], "IGNORED")
        self.assertEqual(self.service.thread(f"whatsapp:+{NUMBER}")[-1]["body"], NOT_A_PHOTO_REPLY)

    def test_wrong_phone_clock_does_not_lose_the_photo(self):
        message = {**image_message(), "timestamp": "946684800"}  # year 2000
        result = self.channel.handle_webhook(webhook(message))
        self.assertEqual(result[0]["outcome"], "PAGE")
        self.assertIsNone(self.service.get_document(result[0]["document_id"])["pages"][0]["captured_at"])

    def test_fin_closes_the_open_group_of_pages(self):
        document_id = self.channel.handle_webhook(webhook(image_message()))[0]["document_id"]
        result = self.channel.handle_webhook(webhook({"from": NUMBER, "id": "wamid.t2", "type": "text",
                                                      "text": {"body": "Fin"}}))
        self.assertEqual((result[0]["outcome"], result[0]["document_id"]), ("CLOSED", document_id))
        self.assertEqual(self.service.get_document(document_id)["document"]["status"], "PENDING_AI")

    def test_acknowledgments_are_sent_and_retried(self):
        self.channel.handle_webhook(webhook(image_message()))
        self.graph.fail_sends = 1
        self.assertEqual(self.channel.deliver_pending(), 0)
        self.assertEqual(self.channel.deliver_pending(), 1)
        sent = [json.loads(call[3]) for call in self.graph.calls if call[1].endswith("/messages")]
        self.assertEqual(sent[-1], {"messaging_product": "whatsapp", "to": NUMBER, "type": "text",
                                    "text": {"body": "Reçu : page 1. Merci, le traitement est en cours."}})
        self.assertEqual(self.service.thread(f"whatsapp:+{NUMBER}")[-1]["delivery_status"], "SENT")
        self.assertEqual(self.channel.deliver_pending(), 0)


class WebhookHttpTest(unittest.TestCase):
    def test_webhook_rejects_unsigned_calls(self):
        import tempfile
        from dayone.server import Api, build_service, make_handler
        env = {"WHATSAPP_APP_SECRET": "app-secret", "WHATSAPP_VERIFY_TOKEN": "verify-me",
               "DAYONE_DATA_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env):
            service = build_service(Path(tmp) / "db.sqlite3")
            service.reset_demo(with_history=False)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(service)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}/webhook/whatsapp"
            try:
                with urllib.request.urlopen(base + "?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=77") as r:
                    self.assertEqual(r.read(), b"77")
                body = json.dumps(webhook({"from": NUMBER, "id": "wamid.x", "type": "text", "text": {"body": "fin"}})).encode()
                for signature, expected in (("sha256=bad", 401),
                                            ("sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest(), 200)):
                    request = urllib.request.Request(base, data=body, method="POST",
                                                     headers={"X-Hub-Signature-256": signature})
                    try:
                        with urllib.request.urlopen(request) as response:
                            status = response.status
                    except urllib.error.HTTPError as error:
                        with error:
                            status = error.code
                    self.assertEqual(status, expected)
            finally:
                server.shutdown()
                server.server_close()
                service.store.close()


if __name__ == "__main__":
    unittest.main()
