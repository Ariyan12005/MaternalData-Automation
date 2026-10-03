"""Extraction interface. The fixture extractor stands in for the real model until it is ready."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from . import schema


class ExtractionError(Exception):
    """Permanent failure for this page set; the document goes to PROCESSING_FAILED."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FixtureExtractor:
    """Returns the fixture whose page set equals the document's pages (order-insensitive)."""

    name = "fixture"

    def __init__(self, fixtures_dir: Path):
        self._by_pages: dict[frozenset[str], dict] = {}
        for path in sorted(Path(fixtures_dir).glob("*_extraction.json")):
            draft = json.loads(path.read_text(encoding="utf-8"))
            errors = schema.validate_draft(draft)
            if errors:
                raise ValueError(f"{path.name} does not follow the schema: {errors[0]}")
            self._by_pages[frozenset(page["page_ref"] for page in draft["pages"])] = draft

    def extract(self, page_refs: list[str]) -> dict:
        draft = self._by_pages.get(frozenset(page_refs))
        if draft is None:
            raise ExtractionError(
                "NO_FIXTURE_FOR_PAGE_SET",
                "Aucune extraction disponible pour ce groupe de pages (prototype à fixtures).",
            )
        return copy.deepcopy(draft)
