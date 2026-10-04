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
import tempfile

from . import schema
from .extraction import ExtractionError


MIN_IMAGE_SIDE = 600
MIN_IMAGE_BYTES = 15_000
MIN_KNOWN_CONFIDENCE = 0.85


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


class PaddleOcrReader:
    """Lazy PaddleOCR v3 reader, so the fixture-only demo has no ML dependency."""

    def __init__(self):
        self._engine = None

    def read(self, image_path: Path) -> list[OcrLine]:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise ExtractionError(
                "OCR_DEPENDENCY_MISSING",
                "PaddleOCR n'est pas installé. Démarrez le serveur avec l'environnement OCR.",
            ) from exc
        if self._engine is None:
            self._engine = PaddleOCR(lang="fr")
        lines: list[OcrLine] = []
        for result in self._engine.predict(str(image_path)):
            for text, score in zip(result["rec_texts"], result["rec_scores"]):
                cleaned = str(text).strip()
                if cleaned:
                    lines.append(OcrLine(cleaned, float(score)))
        return lines


class TesseractDigitsReader:
    """Local Tesseract reader used only as an independent numeric cross-check."""

    def read(self, image_path: Path) -> list[OcrLine]:
        try:
            result = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "fra+eng", "--psm", "6", "tsv"],
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
            lines.append(OcrLine(parts[11].strip(), max(0.0, min(1.0, confidence))))
        return lines


def preprocess_photo(image_path: Path, output_path: Path) -> None:
    """Apply a scanner-like crop, deskew and contrast enhancement with OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ExtractionError("OCR_DEPENDENCY_MISSING", "OpenCV n'est pas installé.") from exc
    image = cv2.imread(str(image_path))
    if image is None:
        raise ExtractionError("RETAKE_REQUIRED", "Image illisible. Reprenez la photo.")
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    page = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(contour) < width * height * 0.2:
            break
        approximation = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approximation) == 4:
            page = approximation.reshape(4, 2).astype("float32")
            break
    if page is not None:
        sums, diffs = page.sum(axis=1), np.diff(page, axis=1).reshape(-1)
        ordered = np.array([page[np.argmin(sums)], page[np.argmin(diffs)], page[np.argmax(sums)], page[np.argmax(diffs)]])
        target_width = int(max(np.linalg.norm(ordered[1] - ordered[0]), np.linalg.norm(ordered[2] - ordered[3])))
        target_height = int(max(np.linalg.norm(ordered[3] - ordered[0]), np.linalg.norm(ordered[2] - ordered[1])))
        if target_width > 100 and target_height > 100:
            destination = np.array([[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]], dtype="float32")
            image = cv2.warpPerspective(image, cv2.getPerspectiveTransform(ordered, destination), (target_width, target_height))
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    enhanced_l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_channel)
    enhanced = cv2.cvtColor(cv2.merge((enhanced_l, a_channel, b_channel)), cv2.COLOR_LAB2BGR)
    if not cv2.imwrite(str(output_path), enhanced):
        raise ExtractionError("RETAKE_REQUIRED", "Prétraitement de l'image impossible. Reprenez la photo.")


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


def _parse_encounter(lines: list[OcrLine], numeric_lines: list[OcrLine], page_refs: list[str]) -> dict:
    clinical_ref = next((ref for ref in page_refs if _section(ref) == "current_pregnancy"), page_refs[0])
    fields = {spec.name: _field(spec) for spec in schema.ENCOUNTER_FIELDS}
    # A clinical page was received, but OCR did not safely identify a value: request review.
    if _section(clinical_ref) == "current_pregnancy":
        fields = {spec.name: _review_field(spec, clinical_ref) for spec in schema.ENCOUNTER_FIELDS}

    # Moroccan paper forms often write TA as ``12/7`` (cmHg), not ``120/70``.
    bp_pattern = r"\b(\d{2,3})\s*[/|]\s*(\d{1,3})\b"
    paddle_bp = _candidate(lines, bp_pattern)
    tesseract_bp = _candidate(numeric_lines, bp_pattern)
    # Two independent readers must agree before a handwritten clinical value becomes KNOWN.
    if paddle_bp and tesseract_bp:
        paddle_match = re.search(bp_pattern, paddle_bp.text)
        tesseract_match = re.search(bp_pattern, tesseract_bp.text)
        assert paddle_match is not None and tesseract_match is not None
        if paddle_match.groups() != tesseract_match.groups():
            return {"encounter_type": "ANTENATAL", "slot": "MANUAL", "fields": fields}
        systolic, diastolic = int(paddle_match.group(1)), int(paddle_match.group(2))
        if systolic < 30:
            systolic *= 10
        if diastolic < 30:
            diastolic *= 10
        if not schema.FIELDS["systolic_bp_mmhg"].range_error(systolic) and not schema.FIELDS["diastolic_bp_mmhg"].range_error(diastolic):
            confidence = min(paddle_bp.confidence, tesseract_bp.confidence)
            raw_text = f"Paddle: {paddle_bp.text}; Tesseract: {tesseract_bp.text}"
            fields["systolic_bp_mmhg"] = _field(schema.FIELDS["systolic_bp_mmhg"], raw_text=raw_text,
                                                   value=systolic, confidence=confidence, status="KNOWN", page_ref=clinical_ref)
            fields["diastolic_bp_mmhg"] = _field(schema.FIELDS["diastolic_bp_mmhg"], raw_text=raw_text,
                                                    value=diastolic, confidence=confidence, status="KNOWN", page_ref=clinical_ref)
    return {"encounter_type": "ANTENATAL", "slot": "MANUAL", "fields": fields}


class LiveOcrExtractor:
    """Real-photo extractor whose output is validated by the existing schema."""

    name = "paddleocr"

    def __init__(self, repo_root: Path, reader=None, numeric_reader=None):
        self.repo_root = Path(repo_root).resolve()
        self.reader = reader or PaddleOcrReader()
        self.numeric_reader = numeric_reader or TesseractDigitsReader()

    def extract(self, page_refs: list[str]) -> dict:
        all_lines: list[OcrLine] = []
        numeric_lines: list[OcrLine] = []
        with tempfile.TemporaryDirectory(prefix="dayone-ocr-") as temp_dir:
            for position, ref in enumerate(page_refs):
                image_path = (self.repo_root / ref).resolve()
                assess_photo(image_path)
                cleaned_path = Path(temp_dir) / f"page-{position}.png"
                preprocess_photo(image_path, cleaned_path)
                all_lines.extend(self.reader.read(cleaned_path))
                numeric_lines.extend(self.numeric_reader.read(cleaned_path))
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
                "extractor": self.name, "extractor_version": "paddleocr-v3-conservative",
                "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
            "pages": [{"page_ref": ref, "section": _section(ref)} for ref in page_refs],
            "pii_detected": pii_detected,
            "document_fields": _parse_document_fields(all_lines, page_refs),
            "encounters": [_parse_encounter(all_lines, numeric_lines, page_refs)],
        }
        errors = schema.validate_draft(draft)
        if errors:
            raise ExtractionError("INVALID_LIVE_OCR_DRAFT", errors[0])
        return draft
