"""Extraction interface. The fixture extractor stands in for the real model until it is ready."""

from __future__ import annotations

import base64
import copy
import json
import urllib.error
import urllib.request
from pathlib import Path

from . import schema


class ExtractionError(Exception):
    """Permanent failure for this page set; the document goes to PROCESSING_FAILED."""

    def __init__(self, code: str, message: str, page_ref: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.page_ref = page_ref


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


class HttpExtractor:
    """External model service: POST the page images, receive a draft that must pass the shared schema.

    Request: ``{"pages": [{"page_ref": ..., "image_base64": ...}]}`` with a bearer token.
    Response: a draft (docs/schema.md). Only pages already accepted by the platform
    are sent; the service must run where the data may go (see consignes on third-party models).
    """

    name = "http"

    def __init__(self, url: str, token: str, media, timeout: float = 120):
        self.url = url
        self.token = token
        self.media = media
        self.timeout = timeout

    def extract(self, page_refs: list[str]) -> dict:
        body = json.dumps({"pages": [{"page_ref": ref, "image_base64": base64.b64encode(self.media(ref)).decode()}
                                     for ref in page_refs]}).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                draft = json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                if error.code == 422:
                    raise ExtractionError("EXTRACTOR_REFUSED", "Le service d'extraction a refusé ces pages.") from None
            raise ConnectionError(f"extractor HTTP {error.code}") from None  # retried by the worker
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError(str(exc)) from exc  # retried by the worker
        except ValueError:
            raise ExtractionError("INVALID_EXTRACTION", "Réponse illisible du service d'extraction.") from None
        errors = schema.validate_draft(draft)
        if errors:
            raise ExtractionError("INVALID_EXTRACTION", errors[0])
        if sorted(page["page_ref"] for page in draft["pages"]) != sorted(page_refs):
            raise ExtractionError("INVALID_EXTRACTION", "Le brouillon ne correspond pas aux pages envoyées.")
        return draft
