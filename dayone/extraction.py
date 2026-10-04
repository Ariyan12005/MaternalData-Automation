"""Extraction interface. The fixture extractor stands in for the real model until it is ready."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

from . import extended, schema


# Why a photo cannot be read, as told to the midwife after "Merci de reprendre la photo de la page N : ".
RETAKE_REASONS = {
    "IMAGE_UNREADABLE": "le fichier reçu ne s'ouvre pas ; renvoyez la photo.",
    "IMAGE_TOO_SMALL": "la photo est trop petite ; rapprochez-vous pour que la page remplisse l'écran.",
    "IMAGE_TOO_DARK": "la photo est trop sombre ; photographiez près d'une fenêtre ou avec plus de lumière.",
    "IMAGE_OVEREXPOSED": "la photo est trop claire ; évitez le flash et les reflets.",
    "IMAGE_LOW_CONTRAST": "l'écriture ne ressort pas ; améliorez l'éclairage, sans reflet.",
    "IMAGE_BLURRY": "la photo est floue ; tenez le téléphone immobile et attendez la mise au point.",
    "NO_TEXT_FOUND": "aucun texte n'a pu être lu ; photographiez la page entière, à plat et bien éclairée.",
}


class ExtractionError(Exception):
    """Permanent failure for this page set; the document goes to PROCESSING_FAILED.

    pages lists per-page causes as {"index": position in page_refs, "reason": code}, e.g. a retake reason.
    """

    def __init__(self, code: str, message: str, *, pages: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.pages = pages or []


class PhotoRejected(ExtractionError):
    """A photo that cannot give a reliable reading; reason is a RETAKE_REASONS key."""

    def __init__(self, reason: str):
        super().__init__("RETAKE_REQUIRED", "Photo inutilisable : " + RETAKE_REASONS[reason])
        self.reason = reason


class ExtractorUnavailable(Exception):
    """Temporary failure (timeout, outage): the document stays PENDING_AI and is retried, like an AI outage."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ExtractionCancelled(Exception):
    """The document's pages or section choices changed during extraction; the result would be stale."""


def check_live_draft(draft, page_refs: list[str]) -> dict:
    """Checks a live extractor's draft against the shared contract; raises ExtractionError otherwise."""
    problems = schema.validate_draft(draft)
    if problems:
        raise ExtractionError("EXTRACTOR_SCHEMA_MISMATCH",
                              f"Réponse de l'extracteur non conforme au schéma partagé : {problems[0]}")
    if Counter(page["page_ref"] for page in draft["pages"]) != Counter(page_refs):
        raise ExtractionError("EXTRACTOR_PAGE_MISMATCH",
                              "La réponse de l'extracteur ne porte pas sur les pages envoyées.")
    for *_where, name, fv in [*schema.iter_fields(draft), *extended.iter_fields(draft)]:
        if fv["verification"]["state"] != "UNVERIFIED" or fv["corrections"]:
            raise ExtractionError("EXTRACTOR_CLAIMS_VERIFICATION",
                                  f"L'extracteur ne peut pas marquer « {name} » comme vérifié par une personne.")
    return draft


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
