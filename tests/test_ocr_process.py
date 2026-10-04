"""OCR in separate processes (dayone/ocr_process.py) with fake engines: timeouts, crashes and clean-up.

Real processes are started (spawn), so these tests take a few seconds. Pages are generated; the fake engine hangs,
crashes or exits depending on a coloured square on the page (tests/ocr_fakes.py).
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    import PIL  # noqa: F401  (the OCR process prepares images with Pillow)
except ImportError:
    PIL = None

from dayone import ocr_process
from dayone.extraction import ExtractionError, ExtractorUnavailable, PhotoRejected
from dayone.ocr_process import EngineSpec, OcrProcessPool

SIGNAL_ENGINE = EngineSpec("tests.ocr_fakes:signal_engine")


@unittest.skipIf(PIL is None, "Pillow is not installed")
class OcrProcessPoolTest(unittest.TestCase):
    def setUp(self):
        from tests.ocr_fakes import make_page

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.temp_root = self.dir / "ocr-work"
        self.pages = {}
        for signal in (None, "hang", "crash", "exit"):
            path = self.dir / f"{signal or 'clean'}-{len(self.pages)}.jpg"  # the name means nothing to the reader
            make_page(signal=signal).save(path, format="PNG")
            self.pages[signal] = path

    def pool(self, spec=SIGNAL_ENGINE, **kwargs) -> OcrProcessPool:
        kwargs.setdefault("page_timeout", 10)
        kwargs.setdefault("start_timeout", 60)
        pool = OcrProcessPool(spec, temp_root=self.temp_root, **kwargs)
        self.addCleanup(pool.close)
        self.last_pool = pool
        return pool

    def assertNoWorkFolders(self):
        self.assertEqual(list(self.last_pool.work_root.glob(ocr_process.WORK_PREFIX + "*")), [])

    def test_page_is_read_and_its_work_folder_removed(self):
        pool = self.pool()
        scan = pool.read(self.pages[None])
        self.assertEqual((len(scan.lines), scan.width, scan.height), (6, 900, 1200))
        self.assertNoWorkFolders()

    def test_hung_engine_is_killed_and_the_next_page_gets_a_new_process(self):
        pool = self.pool(page_timeout=3)
        pool.read(self.pages[None])
        first = pool._workers[0].process.pid
        started = time.monotonic()
        with self.assertLogs("dayone", "WARNING"), self.assertRaises(ExtractionError) as caught:
            pool.read(self.pages["hang"])
        self.assertEqual(caught.exception.code, "OCR_TIMEOUT")
        self.assertLess(time.monotonic() - started, 20)
        self.assertIsNone(pool._workers[0].process, "the hung process was stopped")
        self.assertEqual(len(pool.read(self.pages[None]).lines), 6)
        self.assertNotEqual(pool._workers[0].process.pid, first)
        self.assertNoWorkFolders()

    def test_a_shorter_time_left_for_the_document_caps_the_page(self):
        pool = self.pool(page_timeout=60)
        pool.warm_up().join(60)
        started = time.monotonic()
        with self.assertLogs("dayone", "WARNING"), self.assertRaises(ExtractionError) as caught:
            pool.read(self.pages["hang"], timeout=2)
        self.assertEqual(caught.exception.code, "OCR_TIMEOUT")
        self.assertLess(time.monotonic() - started, 15)

    def test_models_are_loaded_once_at_warm_up_and_reused_for_every_page(self):
        pool = self.pool()
        pool.warm_up().join(60)
        process = pool._workers[0].process
        self.assertTrue(process.is_alive(), "the OCR process is ready before the first page")
        for _ in range(3):
            pool.read(self.pages[None])
        self.assertIs(pool._workers[0].process, process)

    def test_engine_crash_reports_the_error_type_only(self):
        pool = self.pool()
        with self.assertLogs("dayone", "ERROR") as logs, self.assertRaises(ExtractionError) as caught:
            pool.read(self.pages["crash"])
        self.assertEqual(caught.exception.code, "OCR_FAILED")
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("Inventée", "\n".join(logs.output) + caught.exception.message)
        self.assertEqual(len(pool.read(self.pages[None]).lines), 6, "the same process keeps working")
        self.assertNoWorkFolders()

    def test_process_that_dies_is_replaced(self):
        pool = self.pool()
        with self.assertLogs("dayone", "ERROR"), self.assertRaises(ExtractionError) as caught:
            pool.read(self.pages["exit"])
        self.assertEqual(caught.exception.code, "OCR_FAILED")
        self.assertEqual(len(pool.read(self.pages[None]).lines), 6)
        self.assertNoWorkFolders()

    def test_unusable_photo_is_rejected_in_the_process(self):
        bad = self.dir / "bad.jpg"
        bad.write_bytes(b"not an image" * 2000)
        with self.assertRaises(PhotoRejected) as caught:
            self.pool().read(bad)
        self.assertEqual(caught.exception.reason, "IMAGE_UNREADABLE")
        self.assertNoWorkFolders()

    def test_engine_that_cannot_start_makes_ocr_unavailable(self):
        cases = [("tests.ocr_fakes:failing_engine", "OCR_START_FAILED"),
                 ("tests.ocr_fakes:missing_engine", "OCR_DEPENDENCY_MISSING")]
        for factory, code in cases:
            with self.subTest(factory), self.assertLogs("dayone", "ERROR") as logs:
                with self.assertRaises(ExtractorUnavailable) as caught:
                    self.pool(EngineSpec(factory)).read(self.pages[None])
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("Inventée", "\n".join(logs.output))
                self.assertNoWorkFolders()

    def test_pages_are_read_by_at_most_the_configured_number_of_processes(self):
        pool = self.pool(workers=2)
        results, errors = [], []

        def read():
            try:
                results.append(pool.read(self.pages[None]))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=read) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual((len(results), errors), (5, []))
        self.assertEqual(sum(1 for w in pool._workers if w.process is not None), 2)
        self.assertNoWorkFolders()

    def test_closing_stops_the_processes_and_refuses_new_pages(self):
        pool = self.pool()
        pool.read(self.pages[None])
        process = pool._workers[0].process
        pool.close()
        self.assertFalse(process.is_alive())
        with self.assertRaises(ExtractorUnavailable):
            pool.read(self.pages[None])

    def test_leftovers_of_a_stopped_server_are_removed_at_start(self):
        abandoned = self.temp_root / "run-1-abandoned"
        (abandoned / "page-x").mkdir(parents=True)
        (abandoned / "page-x" / "page.png").write_bytes(b"x")
        (abandoned / ocr_process.OWNER_FILE).write_bytes(b"")
        self.pool()
        self.assertFalse(abandoned.exists())

    def test_another_running_server_keeps_its_work_files(self):
        first = self.pool()
        work = first.work_root / "page-in-use"
        work.mkdir()
        (work / "page.png").write_bytes(b"x")
        second = self.pool()
        self.assertNotEqual(second.work_root, first.work_root)
        self.assertTrue((work / "page.png").exists(), "the first server's page was not touched")
        self.assertEqual(len(second.read(self.pages[None]).lines), 6)
        self.assertTrue((work / "page.png").exists())

    def test_a_run_folder_without_owner_is_removed_only_when_old(self):
        starting, old = self.temp_root / "run-2-starting", self.temp_root / "run-3-old"
        starting.mkdir(parents=True)
        old.mkdir()
        long_ago = time.time() - ocr_process.ORPHAN_MIN_AGE_SECONDS - 60
        os.utime(old, (long_ago, long_ago))
        self.pool()
        self.assertTrue(starting.exists(), "it may belong to a server that is starting")
        self.assertFalse(old.exists())

    def test_closing_removes_only_its_own_run_folder(self):
        first, second = self.pool(), self.pool()
        first.read(self.pages[None])
        first.close()
        self.assertFalse(first.work_root.exists())
        self.assertTrue(second.work_root.exists())


class SettingsTest(unittest.TestCase):
    def test_defaults_and_limits(self):
        settings = ocr_process.settings_from_env({})
        self.assertEqual((settings["page_timeout"], settings["document_timeout"]), (120.0, 600.0))
        self.assertEqual(settings["temp_root"], Path(tempfile.gettempdir()) / "dayone-ocr")
        custom = ocr_process.settings_from_env({"DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS": "300",
                                                "DAYONE_OCR_TIMEOUT_SECONDS": "45", "DAYONE_OCR_TMP_DIR": "D:/ocr"})
        self.assertEqual((custom["document_timeout"], custom["page_timeout"], custom["temp_root"]),
                         (300.0, 45.0, Path("D:/ocr")))
        for name, value in (("DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS", "5"), ("DAYONE_OCR_TIMEOUT_SECONDS", "many"),
                            ("DAYONE_OCR_TIMEOUT_SECONDS", "1")):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                ocr_process.settings_from_env({name: value})


if __name__ == "__main__":
    unittest.main()
