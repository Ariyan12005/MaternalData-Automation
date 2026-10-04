"""Background extraction (dayone/extraction_pool.py) and the server worker loop, with a fake service."""

import threading
import time
import unittest

from dayone import server
from dayone.extraction_pool import ExtractionPool


class FakeService:
    """extract_document blocks until released; outcomes are scripted per document."""

    def __init__(self, outcomes=None):
        self.outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
        self.queued = []
        self.calls = []
        self.active = 0
        self.peak = 0
        self.release = threading.Event()
        self.lock = threading.Lock()

    def queued_documents(self):
        return list(self.queued)

    def extract_document(self, document_id):
        with self.lock:
            self.calls.append(document_id)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.release.wait(10)
            script = self.outcomes.get(document_id)
            outcome = script.pop(0) if script else "DONE"
            if outcome == "DONE" and document_id in self.queued:
                self.queued.remove(document_id)
            if outcome == "BOOM":
                raise RuntimeError("database is locked")
            return outcome
        finally:
            with self.lock:
                self.active -= 1


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ExtractionPoolTest(unittest.TestCase):
    def pool(self, service, **kwargs) -> ExtractionPool:
        pool = ExtractionPool(service, **kwargs)
        self.addCleanup(pool.shutdown)
        self.addCleanup(service.release.set)
        return pool

    def test_concurrency_is_bounded_and_a_document_is_never_extracted_twice_at_once(self):
        service = FakeService()
        service.queued = ["DOC-1", "DOC-2", "DOC-3"]
        pool = self.pool(service, workers=2)
        self.assertEqual(pool.submit(service.queued_documents()), ["DOC-1", "DOC-2", "DOC-3"])
        self.assertEqual(pool.submit(service.queued_documents()), [], "still running or waiting for a thread")
        time.sleep(0.2)
        self.assertEqual(service.active, 2)
        service.release.set()
        self.assertTrue(pool.wait_idle(5))
        self.assertEqual((sorted(service.calls), service.peak), (["DOC-1", "DOC-2", "DOC-3"], 2))

    def test_stale_result_is_extracted_again_while_the_document_is_queued(self):
        service = FakeService({"DOC-1": ["STALE", "STALE", "DONE"], "DOC-2": ["STALE"]})
        service.queued = ["DOC-1"]
        service.release.set()
        pool = self.pool(service)
        pool.submit(["DOC-1", "DOC-2"])  # DOC-2 is no longer queued (e.g. registered meanwhile)
        self.assertTrue(pool.wait_idle(5))
        self.assertEqual(service.calls, ["DOC-1", "DOC-1", "DOC-1", "DOC-2"])

    def test_unavailable_extractor_and_errors_wait_before_the_next_attempt(self):
        clock = Clock()
        service = FakeService({"DOC-1": ["RETRY"], "DOC-2": ["BOOM"]})
        service.queued = ["DOC-1", "DOC-2"]
        service.release.set()
        pool = self.pool(service, retry_seconds=30, monotonic=clock)
        with self.assertLogs("dayone", "ERROR") as logs:
            pool.submit(service.queued_documents())
            self.assertTrue(pool.wait_idle(5))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("database is locked", "\n".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records), "no traceback is logged")
        clock.now = 29
        self.assertEqual(pool.submit(service.queued_documents()), [])
        clock.now = 31
        self.assertEqual(pool.submit(service.queued_documents()), ["DOC-1", "DOC-2"])
        self.assertTrue(pool.wait_idle(5))
        self.assertEqual(service.queued, [])


class WorkerLoopTest(unittest.TestCase):
    def test_whatsapp_jobs_keep_running_while_a_page_is_being_read(self):
        service = FakeService()
        service.queued = ["DOC-1"]
        pool = ExtractionPool(service)
        ticks = {"inbound": 0, "outbound": 0}

        class Adapter:
            def process_inbound(self):
                ticks["inbound"] += 1

            def deliver_outbound(self):
                ticks["outbound"] += 1

        stop = threading.Event()
        worker = threading.Thread(target=server._run_worker, args=(service, stop, Adapter()),
                                  kwargs={"interval": 0.02, "pool": pool})
        worker.start()
        try:
            time.sleep(0.5)
            self.assertEqual((service.active, service.calls), (1, ["DOC-1"]), "extraction is blocked")
            self.assertGreater(ticks["inbound"], 10)
            self.assertGreater(ticks["outbound"], 10)
        finally:
            service.release.set()
            stop.set()
            worker.join(5)
            self.assertTrue(pool.wait_idle(5))
            pool.shutdown()
        self.assertEqual(service.calls, ["DOC-1"], "the running document was not submitted again")


if __name__ == "__main__":
    unittest.main()
