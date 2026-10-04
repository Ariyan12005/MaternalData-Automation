"""OCR in separate processes (docs/ocr.md), so that a hung or crashed engine is killed without stopping the server.

Each OCR process loads the engine once, then prepares and reads one page at a time (dayone/ocr_preprocess.py).
The server waits at most the page timeout, then kills the process; a new one starts for the next page. Each page
gets a work folder that the server creates and deletes, so nothing is left behind even when a process is killed.
Work folders are in the local temporary directory, never under the repository, which may be synchronised.
Each pool has its own run folder there, with an owner file locked while the pool lives; at start, a pool removes
only the run folders whose owner is gone, never those of another running server.
Only results, error codes and exception type names come back: recognised text never reaches a log.
"""

from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import ocr_preprocess
from .extraction import ExtractionError, ExtractorUnavailable, PhotoRejected

log = logging.getLogger("dayone")

# One OCR process: each engine already uses several CPU threads (DAYONE_OCR_CPU_THREADS), and a second process
# would double the memory (about 1.5 GB with the medium models) without making a single document faster.
DEFAULT_WORKERS = 1
DEFAULT_PAGE_TIMEOUT = 120.0
DEFAULT_DOCUMENT_TIMEOUT = 600.0
DEFAULT_START_TIMEOUT = 300.0
WORK_PREFIX = "page-"
RUN_PREFIX = "run-"
OWNER_FILE = ".owner"
# A run folder without an owner file may belong to a pool that is starting; it is removed only once this old.
ORPHAN_MIN_AGE_SECONDS = 600


@dataclass(frozen=True)
class EngineSpec:
    """How an OCR process builds its engine: an importable "module:function" and its keyword arguments."""

    factory: str
    kwargs: dict = field(default_factory=dict)

    def build(self):
        module, _, name = self.factory.partition(":")
        return getattr(importlib.import_module(module), name)(**self.kwargs)


def settings_from_env(environ=os.environ) -> dict:
    """OCR pool settings: DAYONE_OCR_TIMEOUT_SECONDS (one page), DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS (all pages of
    a document), DAYONE_OCR_START_TIMEOUT_SECONDS (loading the models), DAYONE_OCR_TMP_DIR. Raises ValueError for a
    value out of range."""
    def number(name, default, cast, low, high):
        raw = environ.get(name, "").strip()
        try:
            value = cast(raw) if raw else default
        except ValueError:
            raise ValueError(f"{name} must be a number") from None
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
        return value

    settings = {
        "page_timeout": number("DAYONE_OCR_TIMEOUT_SECONDS", DEFAULT_PAGE_TIMEOUT, float, 5, 3600),
        "document_timeout": number("DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS", DEFAULT_DOCUMENT_TIMEOUT, float, 10, 7200),
        "start_timeout": number("DAYONE_OCR_START_TIMEOUT_SECONDS", DEFAULT_START_TIMEOUT, float, 5, 3600),
    }
    tmp = environ.get("DAYONE_OCR_TMP_DIR", "").strip()
    settings["temp_root"] = Path(tmp) if tmp else Path(tempfile.gettempdir()) / "dayone-ocr"
    return settings


def _serve(conn, spec: EngineSpec, options: ocr_preprocess.Options) -> None:
    """OCR process: build and load the engine, say ready, then read pages until the pipe closes."""
    try:
        engine = spec.build()
        load = getattr(engine, "load", None)
        if load is not None:
            load()
    except ExtractionError as exc:
        conn.send(("failed", exc.code))
        return
    except BaseException as exc:
        conn.send(("failed", type(exc).__name__))
        return
    conn.send(("ready", None))
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        try:
            reply = ("ok", ocr_preprocess.scan_page(engine, Path(job["path"]), Path(job["work_dir"]),
                                                    force=job["force"], options=options))
        except PhotoRejected as exc:
            reply = ("rejected", exc.reason)
        except ExtractionError as exc:
            reply = ("error", exc.code)
        except Exception as exc:
            reply = ("crash", type(exc).__name__)
        conn.send(reply)


def _remove(folder: Path) -> None:
    # A killed process can keep a file open for a moment on Windows.
    for _attempt in range(10):
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    log.warning("an OCR work folder could not be deleted yet; it is removed at the next start")


def _lock(path: Path, *, create: bool):
    """Opens and locks a run folder's owner file. None when the file is missing or another live pool holds it;
    the lock goes away with its process, however that process ends."""
    try:
        handle = open(path, "a+b" if create else "r+b")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _remove_abandoned_runs(root: Path, own: Path) -> None:
    for run in root.glob(RUN_PREFIX + "*"):
        if run == own or not run.is_dir():
            continue
        owner = run / OWNER_FILE
        if owner.exists():
            handle = _lock(owner, create=False)
            if handle is None:
                continue  # another server is using it
            handle.close()
        else:
            try:
                if time.time() - run.stat().st_mtime < ORPHAN_MIN_AGE_SECONDS:
                    continue
            except OSError:
                continue
        _remove(run)


class _Worker:
    def __init__(self, pool: OcrProcessPool):
        self.pool = pool
        self.process = None
        self.conn = None

    def _start(self) -> None:
        pool = self.pool
        parent, child = pool.context.Pipe()
        process = pool.context.Process(target=_serve, args=(child, pool.spec, pool.options), name="dayone-ocr",
                                       daemon=True)
        process.start()
        child.close()
        self.process, self.conn = process, parent
        try:
            ready = parent.poll(pool.start_timeout)
            kind, detail = parent.recv() if ready else ("timeout", None)
        except (EOFError, OSError):
            kind, detail = "failed", "EOFError"
        if kind != "ready":
            self.stop()
            log.error("OCR process did not start (%s)", detail or "timeout")
            raise ExtractorUnavailable("OCR_DEPENDENCY_MISSING" if detail == "OCR_DEPENDENCY_MISSING"
                                       else "OCR_START_FAILED")

    def stop(self) -> None:
        process, conn, self.process, self.conn = self.process, self.conn, None, None
        if conn is not None:
            conn.close()
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(10)

    def ensure_started(self) -> None:
        if self.process is None or not self.process.is_alive():
            self.stop()
            self._start()

    def read(self, path: Path, force: bool, timeout: float):
        pool = self.pool
        work_dir = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=pool.work_root))
        try:
            self.ensure_started()
            conn = self.conn  # close() may clear self.conn from another thread while this one waits
            try:
                conn.send({"path": str(path), "work_dir": str(work_dir), "force": force})
                if not conn.poll(timeout):
                    self.stop()
                    log.warning("OCR page timed out after %.0f s; OCR process stopped", timeout)
                    raise ExtractionError("OCR_TIMEOUT", f"La lecture de la page a dépassé {timeout:.0f} s.")
                kind, detail = conn.recv()
            except (EOFError, OSError):
                self.stop()
                if pool.closed:
                    raise ExtractorUnavailable("OCR_STOPPED") from None
                log.error("OCR process ended unexpectedly")
                raise ExtractionError("OCR_FAILED", "Le processus OCR s'est arrêté.") from None
        finally:
            _remove(work_dir)
        if kind == "ok":
            return detail
        if kind == "rejected":
            raise PhotoRejected(detail)
        if kind == "error":
            raise ExtractionError(detail, "Erreur du moteur OCR.")
        log.error("local OCR failed on one page (%s)", detail)
        raise ExtractionError("OCR_FAILED", "L'OCR local a échoué sur cette page.")


class OcrProcessPool:
    """At most `workers` OCR processes; read(path, force=...) is a page reader for live_ocr.extract_draft."""

    def __init__(self, spec: EngineSpec, *, workers: int = DEFAULT_WORKERS, page_timeout: float = DEFAULT_PAGE_TIMEOUT,
                 start_timeout: float = DEFAULT_START_TIMEOUT, temp_root: Path | None = None,
                 options: ocr_preprocess.Options = ocr_preprocess.DEFAULT_OPTIONS):
        self.spec = spec
        self.page_timeout = page_timeout
        self.start_timeout = start_timeout
        self.options = options
        self.context = multiprocessing.get_context("spawn")
        self.temp_root = Path(temp_root or Path(tempfile.gettempdir()) / "dayone-ocr")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.work_root = self.temp_root / f"{RUN_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.work_root.mkdir()
        self._owner = _lock(self.work_root / OWNER_FILE, create=True)
        if self._owner is None:
            raise ExtractorUnavailable("OCR_START_FAILED")
        # Leftovers of servers that were stopped abruptly; run folders of running servers are kept.
        _remove_abandoned_runs(self.temp_root, self.work_root)
        self.closed = False
        self._workers = [_Worker(self) for _ in range(workers)]
        self._idle: queue.Queue[_Worker] = queue.Queue()
        for worker in self._workers:
            self._idle.put(worker)

    def read(self, path: Path, *, force: bool = False, timeout: float | None = None):
        """Reads one page within min(timeout, page_timeout) seconds once a process is ready (loading the models
        is bounded by start_timeout instead)."""
        if self.closed:
            raise ExtractorUnavailable("OCR_STOPPED")
        limit = self.page_timeout if timeout is None else max(0.0, min(timeout, self.page_timeout))
        worker = self._idle.get()
        try:
            return worker.read(Path(path), force, limit)
        finally:
            self._idle.put(worker)

    def warm_up(self) -> threading.Thread:
        """Starts the OCR processes in the background so that the models are loaded before the first page."""
        def start():
            for _ in self._workers:
                worker = self._idle.get()
                try:
                    if not self.closed:
                        worker.ensure_started()
                except ExtractorUnavailable:
                    pass  # logged by _start; the first page tries again and reports it
                finally:
                    self._idle.put(worker)

        thread = threading.Thread(target=start, name="dayone-ocr-warm-up", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self.closed = True
        for worker in self._workers:
            worker.stop()
        if self._owner is not None:
            self._owner.close()
            self._owner = None
            _remove(self.work_root)
