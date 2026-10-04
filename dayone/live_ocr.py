"""Conservative local OCR adapter for real registry photos.

This module deliberately recognises only a small number of high-confidence,
labelled values.  Everything else is sent to the existing review screen as
``NEEDS_REVIEW``; it never infers clinical information from handwriting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

from . import schema
from .extraction import ExtractionError


MIN_IMAGE_SIDE = 600
MIN_IMAGE_BYTES = 15_000
MIN_KNOWN_CONFIDENCE = 0.85


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    left: int = 0
    top: int = 0
    page_ref: str | None = None


class TesseractOcrReader:
    """The single fast local OCR engine used by the hackathon demo."""

    def read(self, image_path: Path) -> list[OcrLine]:
        try:
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "fra+eng", "--psm", "11", "tsv"],
                check=True, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "Tesseract n'est pas disponible.") from exc
        lines: list[OcrLine] = []
        for row in result.stdout.splitlines()[1:]:
            parts = row.split("\t")
            if len(parts) != 12 or not parts[11].strip():
                continue
            try:
                confidence = float(parts[10]) / 100
            except ValueError:
                continue
            lines.append(OcrLine(parts[11].strip(), max(0.0, min(1.0, confidence)), int(parts[6]), int(parts[7])))
        return lines


def assess_photo(image_path: Path) -> None:
    """Raise a retake request before OCR when a photo is clearly unusable."""
    if image_path.stat().st_size < MIN_IMAGE_BYTES:
        raise ExtractionError("RETAKE_REQUIRED", "Photo trop petite ou incomplète. Reprenez la photo.")
    try:
        from PIL import Image, ImageStat
        with Image.open(image_path) as image:
            image.load()
            width, height = image.size
            if min(width, height) < MIN_IMAGE_SIDE:
                raise ExtractionError("RETAKE_REQUIRED", "Photo trop petite. Rapprochez-vous de la fiche.")
            grayscale = image.convert("L").resize((128, 128))
            mean = ImageStat.Stat(grayscale).mean[0]
            if mean < 35:
                raise ExtractionError("RETAKE_REQUIRED", "Photo trop sombre. Ajoutez de la lumière et réessayez.")
            if mean > 245:
                raise ExtractionError("RETAKE_REQUIRED", "Photo surexposée. Évitez le reflet et réessayez.")
    except ImportError:
        # Tesseract remains usable without Pillow; size-based retake protection
        # above still applies on a bare Python installation.
        return
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("RETAKE_REQUIRED", "Image illisible. Reprenez la photo.") from exc


def _field(spec: schema.FieldSpec, *, raw_text: str | None = None, value=None,
           confidence: float | None = None, status: str = "NOT_PROVIDED", flags: list[str] | None = None,
           page_ref: str | None = None) -> dict:
    return {
        "raw_text": raw_text,
        "value": value,
        "unit": spec.unit,
        "confidence": confidence,
        "field_status": status,
        "validation_flags": flags or [],
        "source": {"page_ref": page_ref} if page_ref else None,
        "verification": {"state": "UNVERIFIED", "by": None, "at": None},
        "corrections": [],
    }


def _review_field(spec: schema.FieldSpec, page_ref: str) -> dict:
    return _field(spec, status="NEEDS_REVIEW", flags=["OCR_VALUE_UNCLEAR"], page_ref=page_ref)


def _section(page_ref: str) -> str:
    name = Path(page_ref).name
    if name.startswith("dossiers_specimen_10_patientes-"):
        page_number = name.split("-")[-1].split(".")[0]
        return {
            "01": "cover",
            "02": "identification",
            "03": "current_pregnancy",
            "04": "delivery_not_in_v1",
            "05": "postpartum_mother_not_in_v1",
            "06": "postpartum_newborn_not_in_v1",
        }.get(page_number, "unknown")
    return {
        "1-1.jpg": "cover",
        "1-2.jpg": "identification",
        "1-3.jpg": "obstetric_history",
        "1-4.jpg": "current_pregnancy",
        "1-5.jpg": "current_pregnancy",
    }.get(name, "unknown")


def _candidate(lines: list[OcrLine], pattern: str) -> OcrLine | None:
    expression = re.compile(pattern, re.IGNORECASE)
    candidates = [line for line in lines if line.confidence >= MIN_KNOWN_CONFIDENCE and expression.search(line.text)]
    return candidates[0] if candidates else None


def _parse_document_fields(lines: list[OcrLine], page_refs: list[str]) -> dict:
    fields = {spec.name: _field(spec) for spec in schema.DOCUMENT_FIELDS}
    page_ref = next((ref for ref in page_refs if _section(ref) == "cover"), None)
    if page_ref:
        candidate = _candidate(lines, r"(?:n[°o]|num[eé]ro)\s*(?:de\s*)?(?:la\s*)?fiche\s*[:#-]?\s*\d{3,12}")
        if candidate:
            digits = re.findall(r"\d{3,12}", candidate.text)
            if digits:
                spec = schema.FIELDS["registry_file_number"]
                fields[spec.name] = _field(spec, raw_text=candidate.text, value=digits[-1],
                                           confidence=candidate.confidence, status="KNOWN", page_ref=page_ref)
    return fields


def _parse_encounter(lines: list[OcrLine], page_refs: list[str]) -> dict:
    specimen_page = next((ref for ref in page_refs if Path(ref).name == "dossiers_specimen_10_patientes-03.png"), None)
    if specimen_page:
        return _parse_specimen_latest_visit(lines, specimen_page)

    clinical_ref = next((ref for ref in page_refs if _section(ref) == "current_pregnancy"), page_refs[0])
    fields = {spec.name: _field(spec) for spec in schema.ENCOUNTER_FIELDS}
    # A clinical page was received, but OCR did not safely identify a value: request review.
    if _section(clinical_ref) == "current_pregnancy":
        fields = {spec.name: _review_field(spec, clinical_ref) for spec in schema.ENCOUNTER_FIELDS}

    # Moroccan paper forms often write TA as ``12/7`` (cmHg), not ``120/70``.
    bp_pattern = r"\b(\d{2,3})\s*[/|]\s*(\d{1,3})\b"
    bp = _candidate(lines, bp_pattern)
    if bp:
        match = re.search(bp_pattern, bp.text)
        assert match is not None
        systolic, diastolic = int(match.group(1)), int(match.group(2))
        if systolic < 30:
            systolic *= 10
        if diastolic < 30:
            diastolic *= 10
        if not schema.FIELDS["systolic_bp_mmhg"].range_error(systolic) and not schema.FIELDS["diastolic_bp_mmhg"].range_error(diastolic):
            fields["systolic_bp_mmhg"] = _field(schema.FIELDS["systolic_bp_mmhg"], raw_text=bp.text,
                                                   value=systolic, confidence=bp.confidence, status="KNOWN", page_ref=clinical_ref)
            fields["diastolic_bp_mmhg"] = _field(schema.FIELDS["diastolic_bp_mmhg"], raw_text=bp.text,
                                                    value=diastolic, confidence=bp.confidence, status="KNOWN", page_ref=clinical_ref)
    return {"encounter_type": "ANTENATAL", "slot": "MANUAL", "fields": fields}


def _specimen_row(lines: list[OcrLine], page_ref: str, *, top: int, minimum_left: int) -> tuple[str, float] | None:
    """Read the 9th-month cell from the known synthetic specimen table.

    The document is a fixed demo layout, so its final column is safely located
    by coordinates. This avoids inventing a relationship between a left-column
    label and a value from another visit column.
    """
    hits = [line for line in lines if line.page_ref == page_ref and line.left >= minimum_left and abs(line.top - top) <= 28]
    if not hits:
        return None
    hits.sort(key=lambda line: line.left)
    return " ".join(line.text for line in hits), min(line.confidence for line in hits)


def _known_from_raw(name: str, raw_text: str, confidence: float, page_ref: str) -> dict | None:
    spec = schema.FIELDS[name]
    try:
        value = spec.parse(raw_text)
    except schema.InvalidValue:
        return None
    if confidence < MIN_KNOWN_CONFIDENCE or spec.range_error(value):
        return None
    return _field(spec, raw_text=raw_text, value=value, confidence=confidence, status="KNOWN", page_ref=page_ref)


def _parse_specimen_latest_visit(lines: list[OcrLine], page_ref: str) -> dict:
    fields = {spec.name: _review_field(spec, page_ref) for spec in schema.ENCOUNTER_FIELDS}
    rows = {
        "visit_date": (450, r"\d{1,2}/\d{1,2}/\d{2,4}"),
        "gestational_age_days": (550, r"\d{1,2}\s*SA"),
        "weight_kg": (650, r"\d{1,3}(?:[.,]\d+)?"),
        "fundal_height_cm": (997, r"\d{1,2}"),
    }
    for name, (top, pattern) in rows.items():
        candidate = _specimen_row(lines, page_ref, top=top, minimum_left=1400)
        if candidate is None:
            continue
        raw, confidence = candidate
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            known = _known_from_raw(name, match.group(0), confidence, page_ref)
            if known:
                fields[name] = known
    bp = _specimen_row(lines, page_ref, top=700, minimum_left=1400)
    if bp:
        raw, confidence = bp
        match = re.search(r"\b(\d{2,3})\s*[/|]\s*(\d{1,3})\b", raw)
        if match and confidence >= MIN_KNOWN_CONFIDENCE:
            systolic, diastolic = int(match.group(1)), int(match.group(2))
            if not schema.FIELDS["systolic_bp_mmhg"].range_error(systolic) and not schema.FIELDS["diastolic_bp_mmhg"].range_error(diastolic):
                fields["systolic_bp_mmhg"] = _field(schema.FIELDS["systolic_bp_mmhg"], raw_text=raw,
                                                       value=systolic, confidence=confidence, status="KNOWN", page_ref=page_ref)
                fields["diastolic_bp_mmhg"] = _field(schema.FIELDS["diastolic_bp_mmhg"], raw_text=raw,
                                                        value=diastolic, confidence=confidence, status="KNOWN", page_ref=page_ref)
    return {"encounter_type": "ANTENATAL", "slot": "M9", "fields": fields}


class LiveOcrExtractor:
    """Real-photo extractor whose output is validated by the existing schema."""

    name = "tesseract"

    def __init__(self, repo_root: Path, reader=None):
        self.repo_root = Path(repo_root).resolve()
        self.reader = reader or TesseractOcrReader()

    def extract(self, page_refs: list[str]) -> dict:
        all_lines: list[OcrLine] = []
        for ref in page_refs:
            image_path = (self.repo_root / ref).resolve()
            assess_photo(image_path)
            all_lines.extend(
                OcrLine(line.text, line.confidence, line.left, line.top, ref)
                for line in self.reader.read(image_path)
            )
        pii_labels = ("cin", "nom", "prénom", "adresse", "téléphone", "mari")
        pii_detected = [
            {"category": label, "action": "NOT_EXTRACTED"}
            for label in pii_labels if any(label in line.text.lower() for line in all_lines)
        ]
        draft = {
            "schema_version": schema.SCHEMA_VERSION,
            "layout_id": schema.LAYOUT_ID,
            "notes": "OCR local : toute valeur incertaine nécessite une validation humaine.",
            "extraction": {
                "extractor": self.name, "extractor_version": "tesseract-fast-v1",
                "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
            "pages": [{"page_ref": ref, "section": _section(ref)} for ref in page_refs],
            "pii_detected": pii_detected,
            "document_fields": _parse_document_fields(all_lines, page_refs),
            "encounters": [_parse_encounter(all_lines, page_refs)],
        }
        errors = schema.validate_draft(draft)
        if errors:
            raise ExtractionError("INVALID_LIVE_OCR_DRAFT", errors[0])
        return draft
