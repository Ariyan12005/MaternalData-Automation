"""Local OCR extractor for the specimen registry pages.

Only the specimen pictures in ``data/Paper Registry/`` (``dossiers_specimen_*``) are
read; any other image, including camera photos, fails extraction with ``NOT_A_SPECIMEN_PAGE`` so the reviewer enters it by hand.
The page layout and the agreement rules live in ``specimen_ocr``. Nothing is
inferred from handwriting: every uncertain value goes to the review screen.
"""

from __future__ import annotations

import io
import math
import os
import re
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import schema, specimen_ocr
from .extraction import ExtractionError

MIN_IMAGE_SIDE = 600
MIN_IMAGE_BYTES = 15_000
SPECIMEN_PREFIX = "data/Paper Registry/dossiers_specimen_"
PII_LABELS = {"cin": "cin", "nom": "nom", "prénom": "prénom", "nom/prénom": "nom", "adresse": "adresse",
              "téléphone": "téléphone", "mari": "mari"}


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    left: int = 0
    top: int = 0
    page_ref: str | None = None
    width: int = 0
    height: int = 0


def _image_input(image) -> tuple[str, bytes | None]:
    """Tesseract reads a path, or PNG/JPEG bytes from stdin (decrypted media never touches the disk)."""
    return ("stdin", bytes(image)) if isinstance(image, (bytes, bytearray)) else (str(image), None)


class TesseractOcrReader:
    """Default local OCR: fast, small, word-level output with page coordinates."""

    name, version = "tesseract", "tesseract-specimen-consensus-v2"

    def _run(self, image, args: list[str]) -> list[OcrLine]:
        source, data = _image_input(image)
        try:
            result = subprocess.run(
                ["tesseract", source, "stdout", *args, "tsv"],
                input=data, check=True, capture_output=True, timeout=30,
                # One OpenMP thread per process: pages and cells are read in parallel instead.
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "Tesseract n'est pas disponible.") from exc
        lines: list[OcrLine] = []
        for row in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = row.split("\t")
            if len(parts) != 12 or not parts[11].strip():
                continue
            try:
                confidence = float(parts[10]) / 100
            except ValueError:
                continue
            lines.append(OcrLine(parts[11].strip(), max(0.0, min(1.0, confidence)), int(parts[6]), int(parts[7]),
                                 width=int(parts[8]), height=int(parts[9])))
        return lines

    def read(self, image, lang: str = "fra+eng") -> list[OcrLine]:
        """Whole page, sparse text (handwriting scattered in printed boxes)."""
        return self._run(image, ["-l", lang, "--psm", "11"])

    def read_line(self, image, whitelist: str | None = None) -> list[OcrLine]:
        """One cell or one handwritten line, optionally limited to the characters the field can hold."""
        args = ["-l", "eng", "--psm", "7"]
        if whitelist:
            args += ["-c", f"tessedit_char_whitelist={whitelist}"]
        return self._run(image, args)


class PaddleOcrReader:
    """Optional PP-OCR engine for comparison; slower and much larger than Tesseract."""

    name, version = "paddleocr", "paddleocr-ppocrv6-v1"

    def __init__(self, engine=None):
        self._engine = engine
        self._lock = threading.Lock()

    def _load_engine(self):
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "PaddleOCR nécessite l'environnement Python 3.12 du projet.") from exc
        try:
            return PaddleOCR(
                text_detection_model_name="PP-OCRv6_medium_det",
                text_recognition_model_name="PP-OCRv6_medium_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="cpu",
                enable_mkldnn=False,
            )
        except Exception as exc:
            raise ExtractionError("OCR_INITIALIZATION_FAILED", "Impossible de charger les modèles locaux PaddleOCR.") from exc

    @staticmethod
    def _source(image):
        if isinstance(image, (bytes, bytearray)):
            import numpy
            from PIL import Image
            with Image.open(io.BytesIO(image)) as decoded:
                return numpy.array(decoded.convert("RGB"))[:, :, ::-1]
        return str(image)

    def read(self, image, lang: str | None = None) -> list[OcrLine]:
        # Paddle predictors are shared across requests and must not run concurrently.
        with self._lock:
            if self._engine is None:
                self._engine = self._load_engine()
            try:
                results = list(self._engine.predict(self._source(image)))
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError("OCR_FAILED", "PaddleOCR n'a pas pu lire cette page.") from exc
        lines: list[OcrLine] = []
        try:
            for result in results:
                texts, scores, boxes = result["rec_texts"], result["rec_scores"], result["rec_polys"]
                if not (len(texts) == len(scores) == len(boxes)):
                    raise ValueError("OCR result lengths differ")
                for text, score, box in zip(texts, scores, boxes):
                    confidence = float(score)
                    if not math.isfinite(confidence) or not 0 <= confidence <= 1 or not text.strip():
                        continue
                    left, top = int(min(p[0] for p in box)), int(min(p[1] for p in box))
                    right, bottom = int(max(p[0] for p in box)), int(max(p[1] for p in box))
                    lines.append(OcrLine(text.strip(), confidence, left, top, width=right - left, height=bottom - top))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ExtractionError("OCR_INVALID_RESULT", "Résultat PaddleOCR invalide; vérification manuelle requise.") from exc
        return lines

    def read_line(self, image, whitelist: str | None = None) -> list[OcrLine]:
        return self.read(image)


def assess_photo(data: bytes) -> None:
    """Raise a retake request before OCR when a photo is clearly unusable."""
    if len(data) < MIN_IMAGE_BYTES:
        raise ExtractionError("RETAKE_REQUIRED", "Photo trop petite ou incomplète. Reprenez la photo.")
    header = data[:24]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(header) != 24 or header[12:16] != b"IHDR":
            raise ExtractionError("RETAKE_REQUIRED", "Image illisible. Reprenez la photo.")
        width, height = struct.unpack(">II", header[16:24])
        if min(width, height) < MIN_IMAGE_SIDE:
            raise ExtractionError("RETAKE_REQUIRED", "Photo trop petite. Rapprochez-vous de la fiche.")
    try:
        from PIL import Image, ImageStat
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if min(image.size) < MIN_IMAGE_SIDE:
                raise ExtractionError("RETAKE_REQUIRED", "Photo trop petite. Rapprochez-vous de la fiche.")
            mean = ImageStat.Stat(image.convert("L").resize((128, 128))).mean[0]
            if mean < 35:
                raise ExtractionError("RETAKE_REQUIRED", "Photo trop sombre. Ajoutez de la lumière et réessayez.")
            if mean > 245:
                raise ExtractionError("RETAKE_REQUIRED", "Photo surexposée. Évitez le reflet et réessayez.")
    except ImportError:
        return  # size-based protection above still applies without Pillow
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("RETAKE_REQUIRED", "Image illisible. Reprenez la photo.") from exc


def is_specimen(page_ref: str) -> bool:
    return Path(page_ref).as_posix().startswith(SPECIMEN_PREFIX) and "/" not in page_ref[len(SPECIMEN_PREFIX):]


def _section(page_ref: str) -> str:
    """Section from the file name; used only when the printed title cannot be read."""
    name = Path(page_ref).name
    match = re.match(r"dossiers_specimen_10_patientes-(\d+)", name)
    if match:
        number = int(match.group(1))
        return {
            1: "cover", 2: "identification", 3: "current_pregnancy", 4: "delivery_not_in_v1",
            5: "postpartum_mother_not_in_v1", 6: "postpartum_newborn_not_in_v1",
            7: "postpartum_mother_not_in_v1", 8: "postpartum_newborn_not_in_v1",
        }[(number - 1) % 8 + 1]
    return "unknown"


def _field(spec: schema.FieldSpec, reading: specimen_ocr.Reading | None, page_ref: str,
           value=None, source: dict | None = None) -> dict:
    """Draft field from a combined reading: KNOWN only when the reading is agreed and consistent."""
    if reading is None:
        status, raw, confidence, flags, value = "NEEDS_REVIEW", None, None, ["OCR_LABEL_NOT_FOUND"], None
    elif reading.ink == "blank":
        status, raw, confidence, flags, value = "NOT_PROVIDED", None, None, ["BLANK_ON_PAGE"], None
    elif reading.ink == "dash":
        status, raw, confidence, flags, value = "NOT_PROVIDED", "–", None, ["MARKED_DASH"], None
    else:
        value = reading.value if value is None else value
        status = "KNOWN" if reading.agreed else "NEEDS_REVIEW"
        raw, confidence, flags = reading.raw_text, reading.confidence, list(reading.flags)
    return {
        "raw_text": raw, "value": value, "unit": spec.unit, "confidence": confidence,
        "field_status": status, "validation_flags": flags,
        "source": {"page_ref": page_ref, **(source or {})},
        "verification": {"state": "UNVERIFIED", "by": None, "at": None},
        "corrections": [],
    }


class LiveOcrExtractor:
    """Specimen-page extractor whose output is validated by the shared schema."""

    def __init__(self, repo_root: Path, reader=None, media=None, workers: int | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.reader = reader or TesseractOcrReader()
        self.media = media or (lambda ref: (self.repo_root / ref).read_bytes())
        self.workers = workers or max(2, min(16, os.cpu_count() or 2))
        self.name = getattr(self.reader, "name", "live-ocr")
        self.version = getattr(self.reader, "version", "live-ocr-v1")

    def extract(self, page_refs: list[str]) -> dict:
        others = [ref for ref in page_refs if not is_specimen(ref)]
        if others:
            raise ExtractionError("NOT_A_SPECIMEN_PAGE",
                                  "L'OCR ne lit que les pages spécimen de data/Paper Registry : "
                                  f"{', '.join(Path(ref).name for ref in others)}. Saisissez ce dossier à la main.")
        images = [self.media(ref) for ref in page_refs]
        for data in images:
            assess_photo(data)

        jobs = [(index, lang) for index in range(len(images)) for lang in ("fra", "eng")]
        with ThreadPoolExecutor(min(self.workers, len(jobs) or 1)) as pool:
            results = list(pool.map(lambda job: self.reader.read(images[job[0]], job[1]), jobs))
        words = {(index, lang): lines for (index, lang), lines in zip(jobs, results)}

        sections = {}
        for index, ref in enumerate(page_refs):
            section, _ = specimen_ocr.classify(words[(index, "fra")] + words[(index, "eng")])
            sections[index] = section or _section(ref)
        by_section: dict[str, list[int]] = {}
        for index, section in sections.items():
            by_section.setdefault(section, []).append(index)
        if len(by_section.get("cover", [])) > 1 or len(by_section.get("current_pregnancy", [])) > 1:
            raise ExtractionError("SEVERAL_BOOKLETS",
                                  "Ces pages semblent venir de plusieurs carnets : séparez-les avant l'extraction.")

        reader = specimen_ocr.SpecimenReader(self.reader, workers=self.workers)
        pages = {}

        def page_image(index):
            if index not in pages:
                from PIL import Image
                with Image.open(io.BytesIO(images[index])) as image:
                    pages[index] = image.convert("L")
            return pages[index]

        def labelled(name):
            section = specimen_ocr.DOCUMENT_LABELS[name][0]
            index = next(iter(by_section.get(section, [])), None)
            if index is None:
                return None, None
            return reader.read_labelled(name, page_image(index), words[(index, "fra")], words[(index, "eng")]), page_refs[index]

        document = {name: labelled(name) for name in ("registry_file_number", "facility_name", "last_menstrual_period")}

        grid, grid_ref = None, None
        grid_index = next(iter(by_section.get("current_pregnancy", [])), None)
        if grid_index is not None:
            grid_ref = page_refs[grid_index]
            grid = reader.read_grid(page_image(grid_index), words[(grid_index, "fra")], words[(grid_index, "eng")])
        if grid:
            specimen_ocr.check_booklet(grid, document["last_menstrual_period"][0])

        first_ref = page_refs[0]
        document_fields = {}
        for spec in schema.DOCUMENT_FIELDS:
            if spec.name == "midwife_patient_code":
                # The specimen layout has no place for this code.
                document_fields[spec.name] = _field(spec, specimen_ocr.Reading(ink="blank"), first_ref)
                document_fields[spec.name]["validation_flags"] = ["NOT_ON_THIS_LAYOUT"]
                continue
            reading, ref = document[spec.name]
            document_fields[spec.name] = _field(spec, reading, ref or first_ref)
            if reading is not None and reading.ink == "ink" and not reading.agreed:
                document_fields[spec.name]["field_status"] = "NEEDS_REVIEW"

        encounters = []
        for slot, rows in (grid or {}).items():
            column = schema.SLOTS[slot]
            fields = {}
            for spec in schema.ENCOUNTER_FIELDS:
                row = "bp" if spec.name in ("systolic_bp_mmhg", "diastolic_bp_mmhg") else spec.name
                reading = rows[row]
                value = None
                if row == "bp" and reading.value is not None:
                    value = reading.value[0] if spec.name == "systolic_bp_mmhg" else reading.value[1]
                fields[spec.name] = _field(spec, reading, grid_ref, value=value,
                                           source={"row": specimen_ocr.GRID_ROWS[row][2], "column": column})
            encounters.append({"encounter_type": "ANTENATAL", "slot": slot, "fields": fields})
        if not encounters:
            raise ExtractionError("NO_VISIT_FOUND",
                                  "Aucune visite lisible : envoyez la page « Grossesse actuelle » ou saisissez à la main.")

        found = set()
        for lines in words.values():
            for line in lines:
                token = line.text.lower().strip(":.,")
                if token in PII_LABELS:
                    found.add(PII_LABELS[token])
        draft = {
            "schema_version": schema.SCHEMA_VERSION,
            "layout_id": schema.LAYOUT_ID,
            "notes": "OCR local (pages spécimen) : toute valeur incertaine nécessite une validation humaine.",
            "extraction": {
                "extractor": self.name, "extractor_version": self.version,
                "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
            "pages": [{"page_ref": ref, "section": sections[index]} for index, ref in enumerate(page_refs)],
            "pii_detected": [{"category": label, "action": "NOT_EXTRACTED"} for label in sorted(found)],
            "document_fields": document_fields,
            "encounters": encounters,
        }
        errors = schema.validate_draft(draft)
        if errors:
            raise ExtractionError("INVALID_LIVE_OCR_DRAFT", errors[0])
        return draft
