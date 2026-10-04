"""Prototype: read ticked / circled options by locating the printed option labels with OCR
and looking for pen ink around them. Pillow + Tesseract only.

A mark is a cluster of blue-ink pixels. Each cluster is assigned to the NEAREST printed label
(distance along the row). If two labels are almost equally near, the answer is AMBIGUOUS
and a human decides. No expected value is known to this script.

    python3 eval/mark_detection_demo.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "Paper Registry"

# Printed structure of the forms (labels per option group) -- never values.
GROUPS = [
    ("cover_facility_type_row1", "dossiers_specimen_10_patientes-01.png", ["DR", "CSC", "CSU"]),
    ("cover_facility_type_row2", "dossiers_specimen_10_patientes-01.png", ["CSCA", "CSUA"]),
    ("cover_coverage_mode", "dossiers_specimen_10_patientes-01.png", ["Fixe", "Mobile"]),
    ("blood_group", "dossiers_specimen_10_patientes-03.png", ["A", "B", "O", "AB"]),
    ("rhesus", "dossiers_specimen_10_patientes-03.png", ["Rh-", "Rh+"]),
]


def ocr_words(path: Path) -> list[dict]:
    out = subprocess.run(["tesseract", str(path), "stdout", "-l", "fra+eng", "--psm", "11", "tsv"],
                         capture_output=True, text=True, check=True).stdout
    words = []
    for row in out.splitlines()[1:]:
        p = row.split("\t")
        if len(p) == 12 and p[11].strip():
            words.append({"text": p[11].strip(), "left": int(p[6]), "top": int(p[7]), "width": int(p[8]),
                          "height": int(p[9]), "conf": float(p[10])})
    return words


def find_label(words: list[dict], label: str, near_top: float | None = None) -> dict | None:
    pattern = re.compile(re.escape(label).replace(r"\-", "[-—–]?").replace(r"\+", r"[+t]?"), re.I)
    hits = [w for w in words if pattern.fullmatch(w["text"].strip(" :.,;()[]|"))]
    if near_top is not None:
        hits = [w for w in hits if abs(w["top"] - near_top) <= 40]
    return min(hits, key=lambda w: w["top"]) if hits else None


def is_blue_ink(pixel) -> bool:
    r, g, b = pixel[:3]
    # Pen ink is bluer than red; printed black text and paper are not. A low margin keeps dull,
    # purple-looking ink from phone photos; the cluster-size rules below reject JPEG noise.
    return b - r > 15 and b > 60 and g < b


def clusters(image: Image.Image, box: tuple[int, int, int, int], gap: int) -> list[dict]:
    x0, y0, x1, y1 = [max(0, v) for v in box]
    crop = image.crop((x0, y0, x1, y1)).convert("RGB")
    w, h = crop.size
    px = crop.load()
    columns = {}
    for x in range(w):
        ys = [y for y in range(h) if is_blue_ink(px[x, y])]
        if ys:
            columns[x] = ys
    found, current = [], []
    for x in sorted(columns):
        if current and x - current[-1] > gap:
            found.append(current); current = []
        current.append(x)
    if current:
        found.append(current)
    result = []
    for xs in found:
        pixels = sum(len(columns[x]) for x in xs)
        result.append({"x0": x0 + xs[0], "x1": x0 + xs[-1], "pixels": pixels})
    return result


def decide(image: Image.Image, labels: dict[str, dict]) -> dict:
    heights = sorted(w["height"] for w in labels.values())
    h = max(10, heights[len(heights) // 2])
    top = min(w["top"] for w in labels.values()) - h
    bottom = max(w["top"] + w["height"] for w in labels.values()) + h
    left = min(w["left"] for w in labels.values()) - 3 * h
    right = max(w["left"] + w["width"] for w in labels.values()) + 3 * h
    raw = clusters(image, (left, top, right, bottom), gap=int(1.0 * h))
    strong_minimum, weak_minimum = 0.15 * h * h, max(6, 0.04 * h * h)
    marks = [c for c in raw if c["pixels"] >= weak_minimum]
    assigned: dict[str, list[dict]] = {name: [] for name in labels}
    ambiguous = False
    for mark in marks:
        centre = (mark["x0"] + mark["x1"]) / 2
        gaps = {}
        for name, w in labels.items():
            l, r = w["left"], w["left"] + w["width"]
            gaps[name] = 0 if l <= centre <= r else min(abs(centre - l), abs(centre - r))
        ordered = sorted(gaps.items(), key=lambda kv: kv[1])
        if len(ordered) > 1 and ordered[0][1] > 0 and ordered[1][1] < 1.3 * max(ordered[0][1], 1):
            ambiguous = True
            continue
        assigned[ordered[0][0]].append(mark)
    chosen = [name for name, items in assigned.items() if items]
    if ambiguous or len(chosen) > 1:
        return {"result": "AMBIGUOUS -> NEEDS_REVIEW", "chosen": chosen, "marks": len(marks)}
    if not chosen:
        return {"result": "no ink found (NOT the same as unticked: decision pending)", "chosen": [], "marks": 0}
    best = max(c["pixels"] for c in assigned[chosen[0]])
    if best >= strong_minimum:
        return {"result": f"marked: {chosen[0]}   [KNOWN candidate, ink {best}px]", "chosen": chosen, "marks": len(marks)}
    return {"result": f"weak ink at: {chosen[0]} -> NEEDS_REVIEW with candidate ({best}px)", "chosen": chosen, "marks": len(marks)}


def main() -> None:
    cache: dict[str, tuple] = {}
    for group, filename, names in GROUPS:
        path = DATA / filename
        if filename not in cache:
            cache[filename] = (ocr_words(path), Image.open(path))
        words, image = cache[filename]
        found, missing = {}, []
        anchor = None
        for name in names:
            word = find_label(words, name, anchor)
            if word is None:
                missing.append(name)
            else:
                found[name] = word
                anchor = anchor if anchor is not None else word["top"]
        if missing or len(found) < 2:
            print(f"{group:30} labels not located by OCR: {missing or names} -> NEEDS_REVIEW")
            continue
        print(f"{group:30} {decide(image, found)['result']}   (labels read: {', '.join(found)})")


if __name__ == "__main__":
    sys.exit(main())
