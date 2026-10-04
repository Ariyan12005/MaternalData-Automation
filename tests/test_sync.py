import hashlib
import hmac
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dayone.crypto import Cipher
from dayone.service import Invalid
from dayone.sync import CentralUnavailable, HttpCentralRegistry, LocalCentralRegistry, retry_delay
from tests.test_flow import REVIEWER, SAMPLE, FlowTestCase


class SyncTest(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.central = LocalCentralRegistry(Path(self.tmp.name) / "central.log", Cipher(bytes(32)),
                                            available=self.service.central_reachable)
        self.service.central = self.central

    def register_sample(self) -> str:
        document_id = self.send(*SAMPLE)
        self.wait_for_draft()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        self.service.confirm(document_id, reviewer=REVIEWER)
        return document_id

    def status(self, document_id):
        return self.service.get_document(document_id)["document"]["status"]

    def test_registered_document_is_synced_with_an_anonymized_payload(self):
        document_id = self.register_sample()
        self.service.tick()
        self.assertEqual(self.status(document_id), "SYNCED")
        detail = self.service.get_document(document_id)
        self.assertTrue(detail["sync_receipt"]["receipt_id"].startswith("CEN-"))
        records = self.central._records()
        payload = next(r["payload"] for r in records if r["payload"]["document_id"] == document_id)
        text = json.dumps(payload)
        self.assertNotIn("2026823001", text, "link keys stay local")
        self.assertNotIn("raw_text", text)
        self.assertEqual(len(payload["visits"]), 6)
        self.assertNotIn(b"PAT-000001", (Path(self.tmp.name) / "central.log").read_bytes(), "encrypted at rest")
        self.assertEqual(self.service.confirm(document_id, reviewer=REVIEWER)["replayed"], True)

    def test_failure_is_retried_with_backoff_then_recovers(self):
        self.service.set_central_available(False)
        document_id = self.register_sample()
        self.service.tick()
        self.assertEqual(self.status(document_id), "SYNC_FAILED")
        document = self.service.get_document(document_id)["document"]
        self.assertEqual(document["sync_attempts"], 1)

        self.service.tick()  # before the retry time: nothing happens
        self.assertEqual(self.service.get_document(document_id)["document"]["sync_attempts"], 1)
        self.clock.advance(retry_delay(1) + 1)
        self.service.tick()
        self.assertEqual(self.service.get_document(document_id)["document"]["sync_attempts"], 2)

        self.service.set_central_available(True)  # recovery retries at once
        self.service.tick()
        self.assertEqual(self.status(document_id), "SYNCED")
        types = [e["detail"].get("to") for e in self.service.get_document(document_id)["events"] if e["type"] == "STATUS_CHANGED"]
        self.assertEqual(types[-2:], ["SYNC_FAILED", "SYNCED"])

    def test_central_registry_deduplicates_a_repeated_delivery(self):
        payload = {"idempotency_key": "DOC-1:t", "visits": []}
        first = self.central.deliver(payload)
        second = self.central.deliver(payload)
        self.assertEqual((first["duplicate"], second["duplicate"]), (False, True))
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(self.central.count(), 1)

    def test_capture_time_is_kept_and_validated(self):
        result = self.service.ingest_photo(sender_id=self.sender(), message_id="wamid.off-1", media_ref=SAMPLE[0],
                                           captured_at="2026-10-03T16:30:00Z")
        page = self.service.get_document(result["document_id"])["pages"][0]
        self.assertEqual(page["captured_at"], "2026-10-03T16:30:00+00:00")
        for bad in ("2026-10-03T16:30:00", "demain", "2026-12-01T00:00:00Z", "2026-01-01T00:00:00Z"):
            with self.subTest(captured_at=bad), self.assertRaises(Invalid):
                self.service.ingest_photo(sender_id=self.sender(), message_id=f"wamid.bad-{bad}", media_ref=SAMPLE[0],
                                          captured_at=bad)

    def sender(self):
        return self.service.list_senders()[0]["sender_id"]


class HttpCentralTest(unittest.TestCase):
    def test_delivery_is_signed_and_failures_raise(self):
        received = {}

        class Central(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.update(body=body, signature=self.headers["X-DayOne-Signature"],
                                key=self.headers["Idempotency-Key"])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"receipt_id": "R1"}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Central)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            registry = HttpCentralRegistry(f"http://127.0.0.1:{server.server_port}/visits", "s3cret")
            self.assertEqual(registry.deliver({"idempotency_key": "DOC-1:t", "visits": []}), {"receipt_id": "R1"})
        finally:
            server.shutdown()
            server.server_close()
        expected = hmac.new(b"s3cret", received["body"], hashlib.sha256).hexdigest()
        self.assertEqual(received["signature"], f"sha256={expected}")
        self.assertEqual(received["key"], "DOC-1:t")
        with self.assertRaises(CentralUnavailable):
            HttpCentralRegistry(f"http://127.0.0.1:{server.server_port}/visits", "s3cret", timeout=1).deliver(
                {"idempotency_key": "x", "visits": []})


if __name__ == "__main__":
    unittest.main()
