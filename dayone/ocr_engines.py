"""Local OCR engines. PaddleOCR is the primary engine; Tesseract is optional. Setup: docs/ocr.md.

Engines are imported lazily so that fixture mode and the tests need no OCR package. Models are read from
explicit local folders; nothing is downloaded while the server runs. Recognised text is never logged.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata, util
from pathlib import Path

from .extraction import ExtractionError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_DIR = REPO_ROOT / "var" / "ocr-models"
# (detection model, recognition model, lowest OCR confidence for KNOWN). The thresholds come from the specimen
# evaluation in docs/ocr.md: every misread value seen there scored below them.
PADDLE_MODEL_SETS = {
    "medium": ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec", 0.97),
    "mobile": ("PP-OCRv5_mobile_det", "latin_PP-OCRv5_mobile_rec", 0.90),
}
DEFAULT_PADDLE_MODEL_SET = "medium"
# Page orientation (0/90/180/270) from the image content, used by both model sets (docs/ocr.md).
ORIENTATION_MODEL = "PP-LCNet_x1_0_doc_ori"
TESSERACT_LANGUAGE = "fra"
# Not measured here (Tesseract was not installed during the evaluation), so the strictest threshold applies.
TESSERACT_KNOWN_CONFIDENCE = 0.97
TESSERACT_TIMEOUT_SECONDS = 120
WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
# CPU threads per OCR engine (DAYONE_OCR_CPU_THREADS). Unlimited, PaddlePaddle and oneDNN take every core.
DEFAULT_CPU_THREADS = 4
CPU_THREADS_RANGE = (2, 4)
THREAD_VARIABLES = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")


@dataclass(frozen=True)
class OcrLine:
    """One recognised text line with its box in image pixels.

    source_box keeps the box as recognised when the parser moves it (deskew), so field locations stay true.
    """

    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float
    source_box: tuple[float, float, float, float] | None = field(default=None, compare=False)

    @property
    def box(self) -> tuple[float, float, float, float]:
        return self.source_box or (self.x0, self.y0, self.x1, self.y1)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def models_dir_from_env(environ=os.environ) -> Path:
    configured = environ.get("DAYONE_OCR_MODELS_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    if DEFAULT_MODELS_DIR.exists():
        return DEFAULT_MODELS_DIR
    parent_models = REPO_ROOT.parent / "ocr-models"
    if parent_models.exists():
        return parent_models
    return DEFAULT_MODELS_DIR


def model_set_from_env(environ=os.environ) -> str:
    return environ.get("DAYONE_OCR_MODELS", "").strip() or DEFAULT_PADDLE_MODEL_SET


def cpu_threads_from_env(environ=os.environ) -> int:
    raw = environ.get("DAYONE_OCR_CPU_THREADS", "").strip()
    low, high = CPU_THREADS_RANGE
    try:
        threads = int(raw) if raw else DEFAULT_CPU_THREADS
    except ValueError:
        raise ValueError("DAYONE_OCR_CPU_THREADS must be a number") from None
    if not low <= threads <= high:
        raise ValueError(f"DAYONE_OCR_CPU_THREADS must be between {low} and {high}")
    return threads


def limit_threads(threads: int) -> None:
    """Caps the math libraries of this process; call before PaddlePaddle or OpenCV start their thread pools."""
    for name in THREAD_VARIABLES:
        os.environ[name] = str(threads)
    if util.find_spec("cv2") is not None:
        import cv2

        cv2.setNumThreads(threads)


class PaddleEngine:
    """PaddleOCR 3 text detection + recognition, CPU only, on at most cpu_threads threads."""

    name = "paddleocr"

    def __init__(self, models_dir: Path, *, model_set: str = DEFAULT_PADDLE_MODEL_SET,
                 cpu_threads: int = DEFAULT_CPU_THREADS):
        if model_set not in PADDLE_MODEL_SETS:
            raise ValueError(f"DAYONE_OCR_MODELS must be one of {', '.join(PADDLE_MODEL_SETS)}")
        self.cache_home = Path(models_dir) / "paddlex"
        self.model_set = model_set
        self.cpu_threads = cpu_threads
        self.detection_model, self.recognition_model, self.known_confidence = PADDLE_MODEL_SETS[model_set]
        self._pipeline = None
        self._orientation = None

    def model_dir(self, model: str) -> Path:
        return self.cache_home / "official_models" / model

    @property
    def models(self) -> tuple[str, ...]:
        return self.detection_model, self.recognition_model, ORIENTATION_MODEL

    @property
    def version(self) -> str:
        return f"paddleocr-{_version('paddleocr')}+{self.detection_model}+{self.recognition_model}"

    def missing(self) -> list[str]:
        problems = [name for name in ("paddleocr", "paddle", "cv2", "PIL") if util.find_spec(name) is None]
        for model in self.models:
            if not (self.model_dir(model) / "inference.pdiparams").is_file():
                problems.append(f"model {model} (python -m dayone.ocr_engines download)")
        return problems

    def _options(self) -> dict:
        options = dict(text_detection_model_name=self.detection_model,
                       text_recognition_model_name=self.recognition_model,
                       use_doc_orientation_classify=False, use_doc_unwarping=False,
                       use_textline_orientation=False, **self._device())
        return options

    def _device(self) -> dict:
        options = dict(device="cpu", cpu_threads=self.cpu_threads)
        # PaddlePaddle 3.3 crashes in oneDNN on CPU (ConvertPirAttribute2RuntimeAttribute); 3.2.2 is pinned.
        if (_version("paddlepaddle") or "").startswith("3.3"):
            options["enable_mkldnn"] = False
        return options

    def _environment(self) -> None:
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.cache_home)
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        limit_threads(self.cpu_threads)

    def download(self) -> None:
        self._environment()
        from paddleocr import DocImgOrientationClassification, PaddleOCR

        PaddleOCR(**self._options())
        DocImgOrientationClassification(model_name=ORIENTATION_MODEL, **self._device())

    def load(self) -> None:
        """Loads the models from their local folders (slow: done once per OCR process)."""
        if self._pipeline is not None:
            return
        missing = self.missing()
        if missing:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "PaddleOCR incomplet : " + ", ".join(missing))
        self._environment()
        os.environ["HF_HUB_OFFLINE"] = "1"
        from paddleocr import DocImgOrientationClassification, PaddleOCR

        self._orientation = DocImgOrientationClassification(
            model_name=ORIENTATION_MODEL, model_dir=str(self.model_dir(ORIENTATION_MODEL)), **self._device())
        self._pipeline = PaddleOCR(**self._options(),
                                   text_detection_model_dir=str(self.model_dir(self.detection_model)),
                                   text_recognition_model_dir=str(self.model_dir(self.recognition_model)))

    def orientation(self, image_path: Path) -> tuple[int, float]:
        """(degrees counter-clockwise that make the page upright, classifier score)."""
        self.load()
        result = next(iter(self._orientation.predict(str(image_path))))
        return int(result["label_names"][0]), float(result["scores"][0])

    def read(self, image_path: Path) -> list[OcrLine]:
        self.load()
        result = next(iter(self._pipeline.predict(str(image_path))))
        return [OcrLine(str(text).strip(), float(score), *(float(v) for v in box))
                for text, score, box in zip(result["rec_texts"], result["rec_scores"], result["rec_boxes"])
                if str(text).strip()]


class TesseractEngine:
    """Optional engine: the Tesseract 5 program with French data, read through its TSV output."""

    name = "tesseract"
    known_confidence = TESSERACT_KNOWN_CONFIDENCE

    def __init__(self, models_dir: Path, *, command: str | None = None, language: str = TESSERACT_LANGUAGE):
        self.command = (command or os.environ.get("DAYONE_TESSERACT_CMD", "").strip() or shutil.which("tesseract")
                        or (str(WINDOWS_TESSERACT) if WINDOWS_TESSERACT.is_file() else None))
        tessdata = Path(models_dir) / "tessdata"
        self.tessdata_dir = tessdata if (tessdata / f"{language}.traineddata").is_file() else None
        self.language = language

    def _run(self, *args: str, timeout: float = 30) -> str:
        extra = ["--tessdata-dir", str(self.tessdata_dir)] if self.tessdata_dir else []
        return subprocess.run([self.command, *args, *extra], check=True, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout).stdout

    @property
    def version(self) -> str:
        try:
            first = subprocess.run([self.command, "--version"], capture_output=True, text=True, timeout=30)
            number = (first.stdout or first.stderr).split()[1]
        except (OSError, subprocess.SubprocessError, IndexError, TypeError):
            number = "unknown"
        return f"tesseract-{number}+{self.language}"

    def missing(self) -> list[str]:
        if not self.command:
            return ["tesseract program (docs/ocr.md)"]
        try:
            languages = self._run("--list-langs").split()
        except (OSError, subprocess.SubprocessError):
            return ["tesseract program (docs/ocr.md)"]
        return [] if self.language in languages else [f"tesseract language data {self.language}.traineddata"]

    def read(self, image_path: Path) -> list[OcrLine]:
        if not self.command:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "Tesseract n'est pas installé.")
        try:
            output = self._run(str(image_path), "stdout", "-l", self.language, "--psm", "11", "tsv",
                               timeout=TESSERACT_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise ExtractionError("OCR_DEPENDENCY_MISSING", "Tesseract n'est pas installé.") from exc
        return _tesseract_lines(output)


def _tesseract_lines(tsv: str) -> list[OcrLine]:
    """Joins Tesseract words of one line into OcrLine objects, splitting at wide gaps (table cells)."""
    lines: list[OcrLine] = []
    current: list[tuple[str, float, int, int, int, int]] = []
    key = None

    def flush():
        if current:
            lines.append(OcrLine(" ".join(w[0] for w in current), min(w[1] for w in current),
                                 min(w[2] for w in current), min(w[3] for w in current),
                                 max(w[4] for w in current), max(w[5] for w in current)))
            current.clear()

    for row in tsv.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) != 12 or parts[0] != "5" or not parts[11].strip():
            continue
        try:
            left, top, width, height = (int(v) for v in parts[6:10])
            confidence = max(0.0, min(1.0, float(parts[10]) / 100))
        except ValueError:
            continue
        word_key = tuple(parts[1:5])
        if current and (word_key != key or left - current[-1][4] > 1.5 * height):
            flush()
        key = word_key
        current.append((parts[11].strip(), confidence, left, top, left + width, top + height))
    flush()
    return lines


ENGINES = {"paddle": PaddleEngine, "tesseract": TesseractEngine}


def create_engine(choice: str, models_dir: Path | None = None, environ=os.environ):
    models_dir = models_dir or models_dir_from_env(environ)
    if choice == "paddle":
        return PaddleEngine(models_dir, model_set=model_set_from_env(environ),
                            cpu_threads=cpu_threads_from_env(environ))
    return ENGINES[choice](models_dir)


def installed_versions() -> dict[str, str | None]:
    names = ("paddleocr", "paddlepaddle", "paddlex", "opencv-contrib-python", "numpy", "pillow", "pypdfium2")
    return {"python": sys.version.split()[0], **{name: _version(name) for name in names}}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local OCR engines (docs/ocr.md)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="show installed versions and what each engine is missing")
    download = sub.add_parser("download", help="download PaddleOCR models into DAYONE_OCR_MODELS_DIR (var/ocr-models)")
    download.add_argument("--models", choices=(*PADDLE_MODEL_SETS, "all"), default=model_set_from_env())
    args = parser.parse_args(argv)
    if args.command == "download":
        for model_set in PADDLE_MODEL_SETS if args.models == "all" else (args.models,):
            engine = PaddleEngine(models_dir_from_env(), model_set=model_set)
            engine.download()
            for model in engine.models:
                size = sum(f.stat().st_size for f in engine.model_dir(model).rglob("*") if f.is_file())
                print(f"{model}: {size / 1e6:.1f} MB in {engine.model_dir(model)}")
        return
    for name, version in installed_versions().items():
        print(f"{name}: {version or 'not installed'}")
    for choice in ENGINES:
        engine = create_engine(choice)
        missing = engine.missing()
        print(f"engine {choice}: {'ready, ' + engine.version if not missing else 'missing ' + ', '.join(missing)}")


if __name__ == "__main__":
    main()
