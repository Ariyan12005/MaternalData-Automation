# Uncommitted changes for review (Fatma, Ariyan)

Nothing here is committed or pushed. This lists what changed in the working tree since the last commit, who should review it, and what to look at. Results of the OCR evaluation are in [ocr-evaluation.md](./ocr-evaluation.md).

**In short.** Fixture mode is unchanged and stays the default. Local OCR (`--extractor paddle`) is demo-ready on clean renders of the synthetic specimen: 0 wrong KNOWN values of 667 written, every visit column found, no page failed. It is not reliable on photos: on simulated photos 8 of 979 written values were wrong but KNOWN, and confirmation records unopened KNOWN values as confirmed. No real phone photo has been tested, and scores are not calibrated.

**Not to be staged:** the three untracked files with odd names (`App adapter…`, `ersmahmi…app.js…`, `ersmahmi…store.py…`), `.env`, `.venv/`, `var/` (databases, OCR models, reports, WhatsApp media).

## Fatma: extraction, schema, evaluation

| File | Change | Please check |
|------|--------|--------------|
| `dayone/live_ocr.py` (new) | Parser started from your extractor on `origin/pinar` (`ff04285` to `1e4643c`): page sections from printed text, visit grid (one encounter per visit column), cell rules, cross-checks (DDR, gestational age, date order), cover keys, privacy (names and the staff row are never read). Every grid page whose table cannot be read gets its own `MANUAL` visit, even next to pages that were read | Cell rules in [ocr.md](./ocr.md#how-a-page-becomes-a-draft); the KNOWN threshold (0.97 for `medium`) |
| `dayone/ocr_extended.py`, `dayone/extended.py` (new) | Optional extended fields (identification, delivery, newborn, lab rows, checkboxes); never block registration | Which fields are always reviewed; checkbox ink thresholds |
| `dayone/ocr_engines.py`, `dayone/ocr_preprocess.py` (new) | PaddleOCR primary (Tesseract optional, unmeasured), model download, image checks before OCR (`RETAKE_REQUIRED` reasons) | Photo-check thresholds (`MIN_IMAGE_SIDE` 600) |
| `dayone/extraction.py` | `RETAKE_REASONS` (French text for the midwife), `ExtractionError(pages=…)`, `PhotoRejected`, `ExtractorUnavailable` (temporary), `ExtractionCancelled` (stale), `check_live_draft` | That a live extractor can never claim a field was verified |
| `dayone/schema.py` | Page sections, `gestational_age_parts`, `plain()`, choice fields, `extended` catalog, `source` validation (`readings`, `normalization`, `bbox`) | Still schema v1.0: the extensions are optional |
| `docs/schema.md`, `docs/extractor-contract.md`, `docs/ocr.md`, `docs/excel-field-mapping.md` | Contracts and results | Open questions at the end of `extractor-contract.md` (layout ID D7, automatic retake, threshold, real photos) |
| `eval/specimen-ground-truth.json`, `tools/ocr_ground_truth.py`, `tools/ocr_evaluate.py`, `docs/ocr-evaluation.md` (new) | Checked ground truth of the 80 specimen pages (patient keys hashed), development/held-out split, scoring | **The ground truth was checked by the AI assistant, not yet by a person.** A spot-check of a few pages by you would close that gap |
| `tests/test_live_ocr.py`, `tests/test_extended.py`, `tests/test_ocr_preprocess.py`, `tests/test_real_ocr.py` (new), `tests/test_schema.py` | Mocked parser tests; opt-in real-OCR test (`DAYONE_REAL_OCR=1`) scored against the ground truth | |
| `tools/ocr_specimen_eval.py` (new) | The earlier text-layer evaluation, still used for the extended-field details | |

## Ariyan: service, storage, server

| File | Change | Please check |
|------|--------|--------------|
| `dayone/store.py` | New column `pages.section_hint` (reviewer's page section), added to existing databases by `_migrate()` with `ALTER TABLE` | Migration on an old database; no reset |
| `dayone/service.py` | `extract_document` replaces the body of `process_document` and returns `DONE` / `STALE` / `RETRY`. Stale check on active page IDs **and** section choices (`_page_state`), also asked between pages (`is_current`). `ExtractorUnavailable` and unexpected errors leave the document `PENDING_AI` and record the attempt in memory (`extraction_retry`; the pool waits 30 s before the next try); failures keep per-page reasons (`failure_pages`). `set_page_section` (reviewer chooses a page's section, document re-extracted; checks `expected_revision` and moves the revision). Unexpected extraction errors are logged by exception type only, without traceback. Progress per document (`extraction_progress`, `extracting`, `retrying` in the document list). Retake message names the photo problem (`_retake_reason`). `tick` split into `queued_documents` + `extract_document` | Transaction boundaries in `extract_document` (OCR runs outside any transaction; the result is saved only if the page state is unchanged) |
| `dayone/extraction_pool.py` (new) | Extraction on its own thread(s), so the WhatsApp worker loop is never blocked by OCR | One document at a time; no document extracted twice concurrently |
| `dayone/ocr_process.py` (new) | OCR in a separate process with page timeout (default 120 s), document timeout (600 s) and start timeout (300 s); on timeout the process is killed and a new one starts for the next page; work folders cleaned. Each server has its own run folder with a locked owner file; startup removes only the run folders of stopped servers | Behaviour when the server is killed: in the live check the orphaned OCR process exited on its own within about 18 s (see in [ocr-evaluation.md](./ocr-evaluation.md#checks-performed)) |
| `dayone/live_ocr_adapter.py` (new) | Adapter behind `--extractor paddle` / `tesseract`: media paths, error mapping, draft checks | |
| `dayone/server.py`, `dayone/__main__.py` | `--extractor` option and the OCR pool; route `POST /api/pages/{id}/section` (body `section`, `expected_revision`); errors logged by exception type only; `__main__` guarded because OCR processes re-import it | No database reset at startup; outbound hold kept |
| `dayone/extraction_http.py` (new), `tests/test_extraction_http.py` | Prepared HTTP adapter; `--extractor http` refuses to start | |
| `dayone/export_columns.py` (new) | The 31 CSV columns and what blocks each; computes nothing | |
| `tests/test_live_ocr_adapter.py`, `tests/test_ocr_process.py`, `tests/test_extraction_pool.py`, `tests/ocr_fakes.py` (new) | Service path with a fake engine: stale results, retries, timeouts, restart without reset | |

## Aymane's area (for information)

`dayone/static/app.js`, `index.html`, `style.css`: back-office rewrite (visit tabs, comparison table, page preview with the source box, manual entry, page sections, extraction progress and retry state, wording). `README.md`, `tasks.md`, `docs/demo-script.md`, `docs/operating-model.md`: updated wording and results. `.env.example`, `.gitignore` (`.venv/`), `requirements-ocr.txt`, `requirements-ocr.lock.txt`.

## How it was checked

- Regression suite: `python -m unittest discover -s tests`: 216 tests pass in the project `.venv` (none skipped, real OCR included) and with the system Python 3.14 without OCR packages (24 OCR tests skipped).
- Real OCR: the evaluation (170 page reads, 0 failures) and `tests/test_real_ocr.py` (2 pass).
- Live server on a temporary database, outbound held: restart persistence, crash during extraction, retake during extraction (stale result discarded), page timeout (5 s). All passed; details in [ocr-evaluation.md](./ocr-evaluation.md#checks-performed).
- Browser: fixture flow, manual entry, keyboard handlers (DOM audit), 390 px and 1100 px widths, and the source box on a live OCR draft. Real Tab/Enter navigation by a person, screen readers and other browsers were not checked.
