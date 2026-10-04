"""Local OCR behind our extraction contract (docs/ocr.md, docs/extractor-contract.md).

Page files come from ``DayOneService.extraction_media(page_refs)``: registry images, WhatsApp media and encrypted
``secure/`` media are copied to a temporary folder for the duration of one extraction, and the draft keeps the
original page refs. The files are read by content, whatever their name. The server reads pages through
dayone/ocr_process.py (one long-lived OCR process with a timeout); tools and tests can read them in-process.
"""

from __future__ import annotations

import logging
import time
from contextlib import AbstractContextManager
from typing import Callable
from pathlib import Path

from . import live_ocr
from .extraction import ExtractionCancelled, ExtractionError, ExtractorUnavailable, check_live_draft
from .service import ServiceError

log = logging.getLogger("dayone")


class LiveOcrAdapter:
    """FixtureExtractor's interface, plus the reviewer's section choices and a check that the pages are current.

    extraction_media(page_refs) is a context manager yielding {page_ref: Path}; every page is read before it exits.
    reader has read(path, force=..., timeout=...) returning a live_ocr.PageScan, and optionally close().
    document_timeout bounds the reading of all the pages of one document, in seconds (None: no bound).
    """

    accepts_page_context = True

    def __init__(self, extraction_media: Callable[[list[str]], AbstractContextManager[dict[str, Path]]], engine, *,
                 reader=None, document_timeout: float | None = None, monotonic=time.monotonic):
        self._media = extraction_media
        self.engine = engine
        self.reader = reader or live_ocr.InProcessReader(engine)
        self.document_timeout = document_timeout
        self._monotonic = monotonic

    @property
    def name(self) -> str:
        return self.engine.name

    def extract(self, page_refs: list[str], *, section_hints: list[str | None] | None = None,
                is_current: Callable[[], bool] | None = None, progress=None) -> dict:
        try:
            with self._media(list(page_refs)) as paths:
                draft = self._read(page_refs, paths, section_hints, is_current, progress)
        except ServiceError as exc:
            # A page that cannot be found or decrypted: the photo, not a temporary outage.
            raise ExtractionError(exc.code, exc.message) from None
        return check_live_draft(draft, list(page_refs))

    def _read(self, page_refs, paths, section_hints, is_current, progress) -> dict:
        pages = [(ref, paths[ref]) for ref in page_refs]
        deadline = None if self.document_timeout is None else self._monotonic() + self.document_timeout
        try:
            return live_ocr.extract_draft(self.engine, pages, section_hints=section_hints,
                                          read_page=self.reader.read, is_current=is_current, deadline=deadline,
                                          progress=progress, monotonic=self._monotonic)
        except ExtractionError as exc:
            # A missing OCR install is our setup, not the photo: keep the document queued.
            if exc.code == "OCR_DEPENDENCY_MISSING":
                raise ExtractorUnavailable(exc.code) from None
            raise
        except (ExtractorUnavailable, ExtractionCancelled):
            raise
        except Exception as exc:
            # Retrying would rerun the OCR on the same pages every tick. The type only: messages may quote page text.
            log.error("local OCR failed (%s)", type(exc).__name__)
            raise ExtractionError("OCR_FAILED", "L'OCR local a échoué sur ces pages : saisie manuelle "
                                                "ou reprise de la photo.") from None

    def close(self) -> None:
        close = getattr(self.reader, "close", None)
        if close is not None:
            close()
