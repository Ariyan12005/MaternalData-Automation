"""Local OCR behind our extraction contract (docs/ocr.md, docs/extractor-contract.md).

Page refs, including WhatsApp media (``whatsapp-media/<sha256>.<ext>``), are resolved by
``DayOneService.resolve_media``; the files are read by content, whatever their name. The server reads pages
through dayone/ocr_process.py (separate processes with a timeout); tools and tests can read them in-process.
"""

from __future__ import annotations

import logging
import time
from typing import Callable
from pathlib import Path

from . import live_ocr
from .extraction import ExtractionCancelled, ExtractionError, ExtractorUnavailable, check_live_draft
from .service import ServiceError

log = logging.getLogger("dayone")


class LiveOcrAdapter:
    """FixtureExtractor's interface, plus the reviewer's section choices and a check that the pages are current.

    reader has read(path, force=..., timeout=...) returning a live_ocr.PageScan, and optionally close().
    document_timeout bounds the reading of all the pages of one document, in seconds (None: no bound).
    """

    accepts_page_context = True

    def __init__(self, resolve_media: Callable[[str], Path], engine, *, reader=None,
                 document_timeout: float | None = None, monotonic=time.monotonic):
        self._resolve = resolve_media
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
            pages = [(ref, self._resolve(ref)) for ref in page_refs]
        except ServiceError as exc:
            raise ExtractionError(exc.code, exc.message) from None
        deadline = None if self.document_timeout is None else self._monotonic() + self.document_timeout
        try:
            draft = live_ocr.extract_draft(self.engine, pages, section_hints=section_hints,
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
        return check_live_draft(draft, list(page_refs))

    def close(self) -> None:
        close = getattr(self.reader, "close", None)
        if close is not None:
            close()
