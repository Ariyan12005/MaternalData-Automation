"""Image preparation before OCR (docs/ocr.md). Runs in the OCR process, next to the engine; needs Pillow.

Order: open the file by its content (never its name), apply the EXIF orientation, check that the photo is usable,
bring it to the working scale, turn the page upright from its content, then OCR. Only the adjustments that the
evaluation in docs/ocr.md showed to help are on by default; contrast stretching is available but off.
The prepared image is written only inside the work folder given by the caller, which deletes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .extraction import PhotoRejected
from .live_ocr import PageScan

MIN_IMAGE_BYTES = 15_000
MIN_IMAGE_SIDE = 600
DARK_MEAN, BRIGHT_MEAN = 35, 245
# Measured on a greyscale copy whose long side is METRIC_LONG_SIDE, so that photo size does not change them.
METRIC_LONG_SIDE = 1600
MIN_CONTRAST = 35  # spread between the 0.5th and 99.5th brightness percentiles
MIN_SHARPNESS = 20.0  # variance of the Laplacian
MIN_TEXT_LINES = 5
ORIENTATION_MIN_SCORE = 0.6
THUMBNAIL_LONG_SIDE = 1000


@dataclass(frozen=True)
class Options:
    """Working scale: images are shrunk above max_long_side and enlarged below min_long_side (0: never)."""

    min_long_side: int = 0
    max_long_side: int = 2400
    contrast: bool = False
    orientation: bool = True


DEFAULT_OPTIONS = Options()


def _open(path: Path):
    from PIL import Image, ImageOps

    try:
        if path.stat().st_size < MIN_IMAGE_BYTES:
            raise PhotoRejected("IMAGE_TOO_SMALL")
        with Image.open(path) as image:
            image.load()
            transposed = image.getexif().get(0x0112, 1) not in (None, 1)  # EXIF Orientation tag
            return ImageOps.exif_transpose(image).convert("RGB"), transposed
    except PhotoRejected:
        raise
    except Exception:
        raise PhotoRejected("IMAGE_UNREADABLE") from None


def photo_metrics(image) -> dict:
    """Brightness, contrast and sharpness of a photo, independent of its pixel size."""
    from PIL import ImageFilter, ImageStat

    grey = image.convert("L")
    grey.thumbnail((METRIC_LONG_SIDE, METRIC_LONG_SIDE))
    histogram = grey.histogram()
    total = sum(histogram)

    def percentile(share):
        seen = 0
        for value, count in enumerate(histogram):
            seen += count
            if seen >= share * total:
                return value
        return 255

    laplacian = grey.filter(ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128))
    laplacian = laplacian.crop((1, 1, laplacian.width - 1, laplacian.height - 1))  # the border is left unfiltered
    return {"mean": ImageStat.Stat(grey).mean[0], "contrast": percentile(0.995) - percentile(0.005),
            "sharpness": ImageStat.Stat(laplacian).var[0]}


def photo_problem(image) -> str | None:
    """The first reason this photo cannot give a reliable reading, or None."""
    if min(image.size) < MIN_IMAGE_SIDE:
        return "IMAGE_TOO_SMALL"
    metrics = photo_metrics(image)
    if metrics["mean"] < DARK_MEAN:
        return "IMAGE_TOO_DARK"
    if metrics["mean"] > BRIGHT_MEAN:
        return "IMAGE_OVEREXPOSED"
    if metrics["contrast"] < MIN_CONTRAST:
        return "IMAGE_LOW_CONTRAST"
    if metrics["sharpness"] < MIN_SHARPNESS:
        return "IMAGE_BLURRY"
    return None


def _scaled(image, options: Options):
    from PIL import Image

    long_side = max(image.size)
    target = long_side
    if options.max_long_side and long_side > options.max_long_side:
        target = options.max_long_side
    elif options.min_long_side and long_side < options.min_long_side:
        target = options.min_long_side
    if target == long_side:
        return image, 1.0
    scale = target / long_side
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.LANCZOS), scale


def _thumbnail(image) -> tuple[int, int, bytes]:
    grey = image.convert("L")
    grey.thumbnail((THUMBNAIL_LONG_SIDE, THUMBNAIL_LONG_SIDE))
    return grey.width, grey.height, grey.tobytes()


def scan_page(engine, path: Path, work_dir: Path, *, force: bool = False,
              options: Options = DEFAULT_OPTIONS) -> PageScan:
    """Prepares one photo and reads it. Raises PhotoRejected with an actionable reason, unless force is set,
    in which case quality problems become warnings and the fields of the page go to review."""
    from PIL import ImageOps

    image, transposed = _open(path)
    warnings = []
    problem = photo_problem(image)
    if problem and not force:
        raise PhotoRejected(problem)
    if problem:
        warnings.append(problem)
    image, scale = _scaled(image, options)
    target = Path(work_dir) / "page.png"
    rotated = 0
    if options.orientation and hasattr(engine, "orientation"):
        image.save(target)
        degrees, score = engine.orientation(target)
        if score < ORIENTATION_MIN_SCORE:
            warnings.append("ORIENTATION_UNCERTAIN")
        elif degrees % 360:
            image = image.rotate(degrees, expand=True)
            rotated = degrees % 360
    if options.contrast:
        image = ImageOps.autocontrast(image, cutoff=1)
    image.save(target)
    lines = engine.read(target)
    if sum(1 for l in lines if l.confidence >= 0.5) < MIN_TEXT_LINES:
        if not force:
            raise PhotoRejected("NO_TEXT_FOUND")
        warnings.append("NO_TEXT_FOUND")
    return PageScan(lines, width=image.width, height=image.height, exif_transposed=transposed, rotated_ccw=rotated,
                    scale=scale, thumbnail=_thumbnail(image), warnings=warnings)
