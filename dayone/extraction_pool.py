"""Background extraction for the server's worker loop (docs/ocr.md).

The worker loop handles WhatsApp inbound and outbound jobs every second. Extraction (seconds to minutes with
local OCR) runs here instead, on at most `workers` threads, so those jobs are never delayed. A document is
extracted by one thread at a time. A stale result (pages replaced or sections changed meanwhile) is extracted
again at once; an unavailable extractor is retried after a pause. The service's own checks decide what is saved.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("dayone")

RETRY_SECONDS = 30.0


class ExtractionPool:
    def __init__(self, service, *, workers: int = 1, retry_seconds: float = RETRY_SECONDS,
                 monotonic=time.monotonic):
        self.service = service
        self.retry_seconds = retry_seconds
        self._monotonic = monotonic
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dayone-extract")
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._idle = threading.Condition(self._lock)

    def submit(self, document_ids: list[str]) -> list[str]:
        """Queue documents that are not already being extracted or waiting for a retry; returns those queued."""
        now = self._monotonic()
        queued = []
        with self._lock:
            for document_id in document_ids:
                if document_id in self._running or self._retry_at.get(document_id, 0) > now:
                    continue
                self._retry_at.pop(document_id, None)
                self._running.add(document_id)
                queued.append(document_id)
        for document_id in queued:
            self._executor.submit(self._run, document_id)
        return queued

    def _run(self, document_id: str) -> None:
        outcome = "RETRY"
        try:
            while True:
                outcome = self.service.extract_document(document_id)
                if outcome != "STALE":
                    break
                # Pages or sections changed during extraction; extract again only if still queued.
                if document_id not in self.service.queued_documents():
                    break
        except Exception as exc:
            # The type only: a message or traceback may quote page text or draft values.
            log.error("extraction of %s failed (%s); will retry", document_id, type(exc).__name__)
        finally:
            with self._lock:
                self._running.discard(document_id)
                if outcome == "RETRY":
                    self._retry_at[document_id] = self._monotonic() + self.retry_seconds
                self._idle.notify_all()

    def running(self) -> set[str]:
        with self._lock:
            return set(self._running)

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no document is being extracted (tests and shutdown)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
            return True

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
