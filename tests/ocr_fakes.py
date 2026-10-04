"""Generated pages and fake OCR engines for the preprocessing and OCR process tests (needs Pillow).

A page is dark text-like bars on paper, with a black square in the top-left corner that shows which way is up.
Nothing comes from the dataset. OCR processes build their engine from "tests.ocr_fakes:<factory>".
"""

import os
import random
import time

from dayone.extraction import ExtractionError
from dayone.ocr_engines import OcrLine

# Colour of a square in the top-right corner that tells SignalEngine how to misbehave.
SIGNALS = {"hang": (255, 0, 0), "crash": (0, 255, 0), "exit": (0, 0, 255)}


def make_page(width: int = 900, height: int = 1200, *, seed: int = 1, signal: str | None = None):
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    page = Image.new("L", (width, height), 235)
    draw = ImageDraw.Draw(page)
    margin = min(width, height) // 6
    draw.rectangle((20, 20, margin - 30, margin - 30), fill=0)
    y = margin
    while y < height - margin:
        x = margin
        while x < width - margin - 40:
            length = rng.randint(30, 120)
            draw.rectangle((x, y, min(x + length, width - margin), y + 14), fill=rng.randint(10, 60))
            x += length + rng.randint(15, 40)
        y += rng.randint(36, 52)
    noise = Image.frombytes("L", page.size, rng.randbytes(width * height))
    image = Image.blend(page, noise, 0.06).convert("RGB")
    if signal:
        ImageDraw.Draw(image).rectangle((width - 80, 20, width - 20, 80), fill=SIGNALS[signal])
    return image


def text_lines(count: int = 6) -> list[OcrLine]:
    return [OcrLine(f"ligne {i}", 0.99, 100, 100 + 40 * i, 400, 120 + 40 * i) for i in range(count)]


def marker_corner(image) -> int:
    """Clockwise quarter turns from the top-left corner to the darkest corner, in degrees."""
    from PIL import ImageStat

    grey = image.convert("L")
    side = min(grey.size) // 10
    w, h = grey.size
    corners = {0: (0, 0), 90: (w - side, 0), 180: (w - side, h - side), 270: (0, h - side)}
    means = {deg: ImageStat.Stat(grey.crop((x, y, x + side, y + side))).mean[0] for deg, (x, y) in corners.items()}
    return min(means, key=means.get), means


class MarkerEngine:
    """Finds the corner marker to say how far to turn the page; records what it was asked to read."""

    name = "fake"
    version = "fake-1"
    known_confidence = 0.9

    def __init__(self, lines: list[OcrLine] | None = None):
        self.lines = text_lines() if lines is None else lines
        self.seen = []

    def orientation(self, path):
        from PIL import Image

        with Image.open(path) as image:
            corner, means = marker_corner(image)
        runner_up = sorted(means.values())[1]
        return corner, 0.95 if runner_up - means[corner] > 60 else 0.3

    def read(self, path):
        from PIL import Image

        with Image.open(path) as image:
            self.seen.append({"size": image.size, "upright": marker_corner(image)[0] == 0})
        return list(self.lines)


class SignalEngine:
    name = "fake"
    version = "fake-1"
    known_confidence = 0.9

    def read(self, path):
        from PIL import Image

        with Image.open(path) as image:
            colour = image.convert("RGB").getpixel((image.width - 50, 50))
        if colour == SIGNALS["hang"]:
            time.sleep(600)
        if colour == SIGNALS["crash"]:
            raise RuntimeError("could not read 'Inventée Exemple'")
        if colour == SIGNALS["exit"]:
            os._exit(3)
        return text_lines()


def signal_engine() -> SignalEngine:
    return SignalEngine()


def failing_engine():
    raise RuntimeError("no model for 'Inventée Exemple'")


def missing_engine():
    raise ExtractionError("OCR_DEPENDENCY_MISSING", "OCR models are not installed")
