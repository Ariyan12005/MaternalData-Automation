"""HTTP extractor adapter against a local fake service. No external call is made.

The fake answers with drafts from fixtures/, i.e. the shared schema v1.0 contract (docs/schema.md). The request
encoder below is a test stub only: the real request format is not agreed (docs/extractor-contract.md).
"""

import io
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from dayone.extraction import FixtureExtractor
from dayone.extraction_http import ExtractorConfig, ExtractorConfigError, HttpExtractor, load_extractor_config
from dayone.server import build_service
from dayone.server import main as server_main

from tests.test_flow import COVER, REPO_ROOT, REVIEWER, SENDER, T1_GRID, T2_T3_GRID, FlowTestCase

FIXTURES = FixtureExtractor(REPO_ROOT / "fixtures")


def draft_for(*refs: str) -> dict:
    draft = FIXTURES.extract(list(refs))
    draft["extraction"].update(extractor="fake-live", extractor_version="test-0", processed_at=None)
    return draft


def stub_encoder(pages):
    """Test stub, not a proposal: sends the page refs and sizes so the fake can be inspected."""
    body = {"page_refs": [ref for ref, _ in pages], "sizes": [path.stat().st_size for _, path in pages]}
    return json.dumps(body).encode(), {"Content-Type": "application/json"}


class Monotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeExtractorService:
    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake.requests.append({"path": self.path, "content_type": self.headers.get("Content-Type"),
                                      "body": json.loads(body)})
                reply = fake.replies.pop(0)
                if reply.get("before"):
                    reply["before"]()
                time.sleep(reply.get("delay", 0))
                payload = reply.get("raw")
                if payload is None:
                    payload = json.dumps(reply.get("draft", {})).encode()
                self.send_response(reply.get("status", 200))
                for name, value in reply.get("headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda request, client_address: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/extract"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HttpExtractorTestCase(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeExtractorService()
        self.addCleanup(self.fake.close)
        self.monotonic = Monotonic()
        self.service.extractor = self.make_extractor(self.fake.url)

    def make_extractor(self, url: str, timeout: float = 2.0) -> HttpExtractor:
        return HttpExtractor(ExtractorConfig(url=url, timeout=timeout), resolve_media=self.service.resolve_media,
                             encode_request=stub_encoder, monotonic=self.monotonic)

    def status(self, document_id: str) -> tuple:
        document = self.service.get_document(document_id)["document"]
        return document["status"], document["failure_reason"]


class ValidResponseTest(HttpExtractorTestCase):
    def test_valid_response_becomes_a_reviewable_draft(self):
        self.fake.replies.append({"draft": draft_for(COVER, T2_T3_GRID)})
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()

        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertEqual(detail["draft"]["extraction"]["extractor"], "fake-live")
        self.assertEqual(detail["draft"]["extraction"]["processed_at"], self.clock.now.isoformat())
        self.assertEqual(len(detail["review"]["blocking"]), 2, "uncertain fields still go to the reviewer")
        [request] = self.fake.requests
        self.assertEqual((request["path"], request["content_type"]), ("/extract", "application/json"))
        self.assertEqual(request["body"]["page_refs"], [COVER, T2_T3_GRID], "pages are sent in page order")

    def test_multiple_visits_register_as_separate_visits(self):
        self.fake.replies.append({"draft": draft_for(COVER, T2_T3_GRID)})
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        self.assertEqual([e["slot"] for e in self.service.get_document(document_id)["draft"]["encounters"]],
                         ["T2_V1", "M8", "M9"])

        before = self.visit_count()
        self.review_sample(document_id)
        self.service.select_patient(document_id, reviewer=REVIEWER, choice="EXISTING", patient_id="PAT-000001")
        result = self.service.confirm(document_id, reviewer=REVIEWER)
        self.assertEqual([v["outcome"] for v in result["visits"]], ["CREATED"] * 3)
        self.assertEqual(self.visit_count(), before + 3)
        self.assertEqual(len(self.service.patient_timeline("PAT-000001")["visits"]), 6)


class RejectedResponseTest(HttpExtractorTestCase):
    def test_schema_mismatch_and_other_unusable_answers_go_to_manual_entry(self):
        missing_field = draft_for(COVER, T2_T3_GRID)
        del missing_field["document_fields"]["facility_name"]
        excel_field = draft_for(COVER, T2_T3_GRID)
        excel_field["encounters"][0]["fields"]["hemoglobin_g_dl"] = dict(excel_field["encounters"][0]["fields"]["weight_kg"])
        newer_schema = {**draft_for(COVER, T2_T3_GRID), "schema_version": "1.1"}
        human_claim = draft_for(COVER, T2_T3_GRID)
        human_claim["encounters"][0]["fields"]["weight_kg"]["verification"] = {"state": "CONFIRMED", "by": "model",
                                                                              "at": "2026-10-03T18:00:00+00:00"}
        cases = [
            ("missing field", {"draft": missing_field}, "EXTRACTOR_SCHEMA_MISMATCH"),
            ("field outside v1.0", {"draft": excel_field}, "EXTRACTOR_SCHEMA_MISMATCH"),
            ("schema 1.1", {"draft": newer_schema}, "EXTRACTOR_SCHEMA_MISMATCH"),
            ("not JSON", {"raw": b"<html>oops</html>"}, "EXTRACTOR_INVALID_JSON"),
            ("other pages", {"draft": draft_for(COVER, T1_GRID)}, "EXTRACTOR_PAGE_MISMATCH"),
            ("claims verification", {"draft": human_claim}, "EXTRACTOR_CLAIMS_VERIFICATION"),
            ("refused", {"status": 422}, "EXTRACTOR_HTTP_422"),
            ("asynchronous", {"status": 202}, "EXTRACTOR_HTTP_202"),
            ("redirect", {"status": 302, "headers": {"Location": "https://elsewhere.invalid/extract"}},
             "EXTRACTOR_HTTP_302"),
        ]
        for label, reply, code in cases:
            with self.subTest(label):
                self.fake.replies.append(reply)
                document_id = self.send(COVER, T2_T3_GRID)
                self.wait_for_draft()
                self.assertEqual(self.status(document_id), ("PROCESSING_FAILED", code))
                manual = self.service.start_manual_entry(document_id, reviewer=REVIEWER)
                self.assertEqual(manual["document"]["status"], "NEEDS_REVIEW")
                self.assertEqual(manual["draft"]["extraction"]["extractor"], "manual")
        self.assertEqual(len(self.fake.requests), len(cases), "permanent answers are not retried")


class TemporaryFailureTest(HttpExtractorTestCase):
    def test_timeout_keeps_the_document_waiting_then_recovers(self):
        self.service.extractor = self.make_extractor(self.fake.url, timeout=0.2)
        self.fake.replies += [{"draft": draft_for(COVER, T2_T3_GRID), "delay": 1.0},
                              {"draft": draft_for(COVER, T2_T3_GRID)}]
        document_id = self.send(COVER, T2_T3_GRID)
        with self.assertLogs("dayone.service", "WARNING") as logs:
            self.wait_for_draft()
        self.assertIn("EXTRACTOR_TIMEOUT", logs.output[0])
        self.assertEqual(self.status(document_id), ("PENDING_AI", None))
        self.assertIsNone(self.service.get_document(document_id)["draft"])

        self.monotonic.now += 5
        self.assertEqual(self.service.tick(), [document_id])
        self.assertEqual(self.status(document_id), ("NEEDS_REVIEW", None))
        self.assertEqual(len(self.fake.requests), 2)

    def test_temporary_outage_backs_off_and_recovers(self):
        self.fake.replies += [{"status": 503}, {"status": 502}, {"draft": draft_for(COVER, T2_T3_GRID)}]
        document_id = self.send(COVER, T2_T3_GRID)
        self.wait_for_draft()
        self.assertEqual(self.status(document_id), ("PENDING_AI", None))

        self.assertEqual(self.service.tick(), [], "no call during the 5 s backoff")
        self.monotonic.now += 5
        self.service.tick()
        self.monotonic.now += 5
        self.assertEqual(self.service.tick(), [], "backoff doubled to 10 s")
        self.assertEqual(len(self.fake.requests), 2)

        self.monotonic.now += 5
        self.assertEqual(self.service.tick(), [document_id])
        self.assertEqual(self.status(document_id), ("NEEDS_REVIEW", None))
        self.assertEqual(len(self.fake.requests), 3)

    def test_unreachable_extractor_is_temporary(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        # Windows retries a refused loopback connection for about 2 s before reporting it.
        self.service.extractor = self.make_extractor(f"http://127.0.0.1:{closed_port}/extract", timeout=10)
        document_id = self.send(COVER, T2_T3_GRID)
        with self.assertLogs("dayone.service", "WARNING") as logs:
            self.wait_for_draft()
        self.assertIn("EXTRACTOR_UNREACHABLE", logs.output[0])
        self.assertEqual(self.status(document_id), ("PENDING_AI", None))


class RetakeStalenessTest(HttpExtractorTestCase):
    def test_result_for_pages_replaced_by_a_retake_is_discarded(self):
        document_id = self.send(COVER, T2_T3_GRID)
        self.service.set_ai_available(False)
        self.wait_for_draft()
        page_id = self.service.get_document(document_id)["pages"][1]["page_id"]
        request_id = self.service.request_retake(page_id, reviewer=REVIEWER)["request_id"]
        self.service.set_ai_available(True)

        def replace_while_extracting():
            self.service.ingest_photo(sender_id=SENDER, message_id="wamid.retake-1", media_ref=T2_T3_GRID,
                                      retake_request_id=request_id)

        self.fake.replies += [{"draft": draft_for(COVER, T2_T3_GRID), "before": replace_while_extracting},
                              {"draft": draft_for(COVER, T2_T3_GRID)}]
        self.assertEqual(self.service.tick(), [], "result for the replaced page set must not be saved")
        detail = self.service.get_document(document_id)
        self.assertEqual((detail["document"]["status"], detail["draft"]), ("PENDING_AI", None))

        self.assertEqual(self.service.tick(), [document_id])
        detail = self.service.get_document(document_id)
        self.assertEqual(detail["document"]["status"], "NEEDS_REVIEW")
        self.assertNotEqual(detail["pages"][1]["page_id"], page_id, "the draft belongs to the replacement page")
        self.assertEqual(len(self.fake.requests), 2)


class ConfigurationTest(HttpExtractorTestCase):
    def test_endpoint_settings_are_validated(self):
        config = load_extractor_config({"DAYONE_EXTRACTOR_URL": "https://ocr.example.org/v1/extract",
                                        "DAYONE_EXTRACTOR_TIMEOUT_SECONDS": "45"})
        self.assertEqual((config.url, config.timeout), ("https://ocr.example.org/v1/extract", 45.0))
        self.assertEqual(load_extractor_config({"DAYONE_EXTRACTOR_URL": "http://127.0.0.1:9000/x"}).timeout, 30.0)
        for environ in ({}, {"DAYONE_EXTRACTOR_URL": "http://ocr.example.org/extract"},
                        {"DAYONE_EXTRACTOR_URL": "https://user:secret@ocr.example.org/extract"},
                        {"DAYONE_EXTRACTOR_URL": "https://ocr.example.org/extract?key=secret"},
                        {"DAYONE_EXTRACTOR_URL": "ftp://ocr.example.org/extract"},
                        {"DAYONE_EXTRACTOR_URL": "https://ocr.example.org/x", "DAYONE_EXTRACTOR_TIMEOUT_SECONDS": "0"},
                        {"DAYONE_EXTRACTOR_URL": "https://ocr.example.org/x", "DAYONE_EXTRACTOR_TIMEOUT_SECONDS": "soon"}):
            with self.subTest(environ), self.assertRaises(ExtractorConfigError):
                load_extractor_config(environ)

    def test_fixture_stays_the_default_and_http_refuses_until_the_contract_is_agreed(self):
        default = build_service(Path(self.tmp.name) / "default.sqlite3")
        try:
            self.assertIsInstance(default.extractor, FixtureExtractor)
        finally:
            default.store.close()
        for environ, expected in (({"DAYONE_EXTRACTOR_URL": self.fake.url}, "not agreed"),
                                  ({"DAYONE_EXTRACTOR_URL": "http://ocr.example.org/extract"}, "https")):
            with self.subTest(expected), mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch("sys.stderr", io.StringIO()) as stderr, self.assertRaises(SystemExit):
                server_main(["--db", str(self.db_path), "--extractor", "http"])
            self.assertIn(expected, stderr.getvalue())
        self.assertEqual(self.fake.requests, [], "nothing is sent while the contract is open")
