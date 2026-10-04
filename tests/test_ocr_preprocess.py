"""Image preparation before OCR (dayone/ocr_preprocess.py) on generated pages, with a fake engine.

Every image is generated in a temporary folder; nothing is read from the dataset. Real OCR on generated copies of
the specimen pages is in tests/test_real_ocr.py (opt-in).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from PIL import Image, ImageFilter
except ImportError:  # the parser tests do not need Pillow
    Image = None

from dayone import live_ocr, ocr_preprocess
from dayone.extraction import PhotoRejected
from tests.ocr_fakes import MarkerEngine, make_page, text_lines


@unittest.skipIf(Image is None, "Pillow is not installed")
class ScanPageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.work = self.dir / "work"
        self.work.mkdir()
        self.engine = MarkerEngine()

    def save(self, image, name: str, **kwargs) -> Path:
        path = self.dir / name
        image.save(path, **kwargs)
        return path

    def scan(self, path, **kwargs):
        return ocr_preprocess.scan_page(self.engine, path, self.work, **kwargs)

    def rejected(self, path, **kwargs) -> str:
        with self.assertRaises(PhotoRejected) as caught:
            self.scan(path, **kwargs)
        return caught.exception.reason

    def test_file_names_and_extensions_do_not_matter(self):
        page = make_page()
        results = [self.scan(self.save(page, name, format=fmt))
                   for name, fmt in (("page.png", "PNG"), ("IMG-20250101-WA0001.jpg", "PNG"),
                                     ("f3a9c1d2e4b5", "JPEG"), ("scan.jpeg", "JPEG"))]
        self.assertEqual({len(r.lines) for r in results}, {6})
        self.assertEqual({(r.width, r.height, r.rotated_ccw) for r in results}, {(900, 1200, 0)})

    def test_large_photos_are_shrunk_and_small_ones_kept_unless_asked(self):
        large = self.scan(self.save(make_page().resize((2700, 3600)), "large.jpg", format="JPEG", quality=90))
        self.assertEqual((large.width, large.height, large.scale), (1800, 2400, 2400 / 3600))
        small_path = self.save(make_page().resize((675, 900)), "small.png")
        small = self.scan(small_path)
        self.assertEqual((small.width, small.height, small.scale), (675, 900, 1.0))
        upscaled = self.scan(small_path, options=ocr_preprocess.Options(min_long_side=1800))
        self.assertEqual((upscaled.width, upscaled.height, upscaled.scale), (1350, 1800, 2.0))
        self.assertEqual(upscaled.thumbnail[:2], (750, 1000))

    def test_rotated_photos_are_turned_upright_before_reading(self):
        page = make_page()
        for turn in (90, 180, 270):
            with self.subTest(turn=turn):
                scan = self.scan(self.save(page.rotate(turn, expand=True), f"r{turn}.png"))
                self.assertEqual((scan.width, scan.height, scan.rotated_ccw), (900, 1200, (360 - turn) % 360))
                self.assertTrue(self.engine.seen[-1]["upright"])

    def test_exif_orientation_is_applied_first(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # stored turned a quarter anticlockwise; viewers turn it back clockwise
        path = self.save(make_page().rotate(90, expand=True), "exif.jpg", format="JPEG", quality=92, exif=exif)
        scan = self.scan(path)
        self.assertEqual((scan.exif_transposed, scan.rotated_ccw, scan.width, scan.height), (True, 0, 900, 1200))
        self.assertTrue(self.engine.seen[-1]["upright"])

    def test_orientation_can_be_switched_off(self):
        scan = self.scan(self.save(make_page().rotate(180), "r180.png"),
                         options=ocr_preprocess.Options(orientation=False))
        self.assertEqual((scan.rotated_ccw, self.engine.seen[-1]["upright"]), (0, False))

    def test_unusable_photos_get_an_actionable_retake_reason(self):
        page = make_page()
        cases = [
            ("garbage bytes", lambda: self.dir.joinpath("bad.jpg").write_bytes(os.urandom(40_000)) and "bad.jpg",
             "IMAGE_UNREADABLE"),
            ("tiny file", lambda: self.dir.joinpath("tiny.jpg").write_bytes(b"\xff\xd8" * 100) and "tiny.jpg",
             "IMAGE_TOO_SMALL"),
            ("too few pixels", lambda: self.save(page.resize((420, 560)), "few.png").name, "IMAGE_TOO_SMALL"),
            ("dark", lambda: self.save(page.point(lambda v: v // 10), "dark.png").name, "IMAGE_TOO_DARK"),
            ("overexposed", lambda: self.save(page.point(lambda v: 250 + v // 50), "white.png").name,
             "IMAGE_OVEREXPOSED"),
            ("faded", lambda: self.save(page.point(lambda v: 200 + v // 10), "faded.png").name,
             "IMAGE_LOW_CONTRAST"),
            ("blurred", lambda: self.save(page.filter(ImageFilter.GaussianBlur(8)), "blur.png").name,
             "IMAGE_BLURRY"),
        ]
        for label, make, reason in cases:
            with self.subTest(label):
                self.assertEqual(self.rejected(self.dir / make()), reason)
                self.assertIn(reason, live_ocr.RETAKE_REASONS)
        self.assertEqual(self.engine.seen, [], "no unusable photo reaches OCR")

    def test_a_photo_with_almost_no_text_is_a_retake(self):
        self.engine.lines = text_lines(2)
        path = self.save(make_page(), "page.png")
        self.assertEqual(self.rejected(path), "NO_TEXT_FOUND")
        self.assertEqual(self.scan(path, force=True).warnings, ["NO_TEXT_FOUND"])

    def test_forced_reading_keeps_the_problem_as_a_warning(self):
        scan = self.scan(self.save(make_page().filter(ImageFilter.GaussianBlur(8)), "blur.png"), force=True)
        self.assertEqual((scan.warnings, len(scan.lines)), (["IMAGE_BLURRY"], 6))

    def test_contrast_stretching_is_off_by_default(self):
        faded = make_page().point(lambda v: 120 + v // 3)
        with mock.patch.object(ocr_preprocess, "MIN_CONTRAST", 0):
            plain = self.scan(self.save(faded, "faded.png"))
            stretched = self.scan(self.dir / "faded.png", options=ocr_preprocess.Options(contrast=True))
        self.assertLess(max(plain.thumbnail[2]) - min(plain.thumbnail[2]),
                        max(stretched.thumbnail[2]) - min(stretched.thumbnail[2]))

    def test_metrics_do_not_depend_on_photo_size_above_the_working_scale(self):
        page = make_page()
        small, large = (ocr_preprocess.photo_metrics(page.resize(size)) for size in ((1800, 2400), (2700, 3600)))
        for name in ("mean", "contrast", "sharpness"):
            self.assertAlmostEqual(small[name], large[name], delta=max(2.0, 0.15 * small[name]), msg=name)


@unittest.skipIf(Image is None, "Pillow is not installed")
class InProcessReaderTest(unittest.TestCase):
    def test_work_folder_is_removed_after_success_and_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            page = root / "page.png"
            make_page().save(page)
            dark = root / "dark.png"
            make_page().point(lambda v: v // 10).save(dark)
            reader = live_ocr.InProcessReader(MarkerEngine())
            with mock.patch.object(tempfile, "tempdir", str(root / "tmp")):
                (root / "tmp").mkdir()
                self.assertEqual(len(reader.read(page).lines), 6)
                with self.assertRaises(PhotoRejected):
                    reader.read(dark)
                self.assertEqual(list((root / "tmp").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
