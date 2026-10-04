# Live extractor integration: adapter contract and open questions

**Status:** Fatma's extraction boundary is **in-process Python, not HTTP**. We now own local OCR: `dayone/live_ocr.py` started from her extractor and has been rewritten around one engine at a time. PaddleOCR is the primary engine; Tesseract is optional. It runs behind `--extractor paddle` (or `tesseract`) through `dayone/live_ocr_adapter.py`, and has been run on real specimen pages; see [ocr.md](./ocr.md) for setup and results. The fixture extractor stays the default. The HTTP adapter (`dayone/extraction_http.py`) stays prepared but unused, because her service has no extraction endpoint.

The sections below up to "Adapter contract" describe her service as observed on 2026-10-03.

## Fatma's running service (observed 2026-10-03, read-only)

Her service at `http://10.201.10.1:8001` was checked with `GET` requests only: `/api/system`, `/`, `/app.js` and `/style.css`. Nothing was posted, so her database was not written to.

| Observation | Evidence |
|-------------|----------|
| It is a complete DayOne instance, not an extraction service | `/` is "DayOne – prototype". Her `app.js` calls only DayOne routes: documents, review, confirmation, patients, WhatsApp simulator, `POST /api/demo/reset` and `POST /api/system/ai`. There is no extraction route |
| `/api/system` is status, not extraction | `{"ai_available": true, "grouping_window_seconds": 8, "extractor": "paddleocr", "catalog": …}`. `extractor` is `LiveOcrExtractor.name`. The catalog is identical to our `schema.catalog()` (12 fields, v1.0) |
| It runs commit `ff04285` on `origin/pinar` ("Default demo selector to specimen PNG pages", 20:33) | Served `index.html`, `app.js` and `style.css` are byte-identical (line endings aside) to that commit and its successors. Of those, only `ff04285` still reports `paddleocr`; from `aa1d937` on, the extractor is named `tesseract` |
| Caveat | Python code cannot be seen over HTTP, so uncommitted local edits to her `live_ocr.py` cannot be ruled out |

### The extraction boundary

In `ff04285`, `python -m dayone --extractor paddle` builds `LiveOcrExtractor(REPO_ROOT)` (`dayone/live_ocr.py`) and passes it to `DayOneService` in place of `FixtureExtractor`. The queue worker calls `extractor.extract(page_refs)` in-process, which is **the same interface as our extractors**. The only HTTP route that leads to an extraction is `POST /api/whatsapp/messages`, which ingests a photo into her database. That is not an extraction interface and must not be used.

| Topic | `LiveOcrExtractor` in `ff04285` |
|-------|---------------------------------|
| Input | `extract(page_refs: list[str])`. Each page is opened as `repo_root / page_ref` |
| Pipeline per page | `assess_photo` (size, darkness, overexposure → `RETAKE_REQUIRED`), OpenCV crop/deskew/contrast, then PaddleOCR v3 (`lang="fr"`) and Tesseract (`fra+eng`, 30 s per page) |
| Output | One schema v1.0 draft, checked by her with `schema.validate_draft` (`INVALID_LIVE_OCR_DRAFT` otherwise). `extractor` = `paddleocr`, `extractor_version` = `paddleocr-v3-conservative` |
| Values read | Only blood pressure (`KNOWN` when PaddleOCR and Tesseract agree, confidence ≥ 0.85, in range; `12/7` read as 120/70) and the registry number on a cover page. Every other field on a clinical page is `NEEDS_REVIEW` with `OCR_VALUE_UNCLEAR` |
| Page type | Decided from the **file name** only: `dossiers_specimen_10_patientes-01…06.png` and `1-1…1-5.jpg`. Any other name is `unknown` |
| Encounters | Always one encounter, slot `MANUAL`, type `ANTENATAL` |
| PII | Labels such as `nom`, `cin` and `téléphone` are listed in `pii_detected` as `NOT_EXTRACTED`. Their values are not extracted |
| Errors | `ExtractionError`: `RETAKE_REQUIRED`, `OCR_DEPENDENCY_MISSING`, `INVALID_LIVE_OCR_DRAFT` |
| Startup | Her server calls `reset_demo(with_history=False)` whenever `--extractor paddle` starts, which wipes the database |

Her later commits (`aa1d937` to `1e4643c`, latest at 21:11) replace PaddleOCR with Tesseract alone and add specimen-table parsing (slot `M9`). The boundary stays the same: `LiveOcrExtractor(repo_root, reader=…)` and `extract(page_refs)`, with the same error codes.

Not treated as Fatma's contract: Ariyan's pull-based job API on `origin/ariyan-backend` (`docs/backend-part3.md`) and any multipart `/extract` endpoint. Her service exposes neither.

## Adapter contract (implemented in `dayone/live_ocr_adapter.py`)

`LiveOcrAdapter(resolve_media, engine)` has the same interface as the other extractors. `extract(page_refs)` returns a draft, raises `ExtractionError` (permanent: `PROCESSING_FAILED`, then manual entry or a reviewer's retake request), or raises `ExtractorUnavailable` (temporary: the document stays `PENDING_AI`). The engines and the parser are described in [ocr.md](./ocr.md).

| Aspect | Behaviour |
|--------|-----------|
| Mode | `--extractor paddle` (primary) or `--extractor tesseract` (optional), or `DAYONE_EXTRACTOR`. `fixture` stays the default. One engine reads every field; the two engines are never combined |
| Startup check | Refuses to start while the chosen engine is incomplete: packages missing from the running Python, models not downloaded (`python -m dayone.ocr_engines download`), or the Tesseract program or its French data missing. Nothing is downloaded at startup |
| Database | Opened like in fixture mode (`ensure_seed`). **No reset at startup**; demo data and the WhatsApp outbound hold are kept |
| Page files | Each `page_ref` is resolved by `DayOneService.resolve_media`, so WhatsApp media (`whatsapp-media/<sha256>.<ext>`) and registry images both work. Page types come from the printed text that OCR finds, never from file names |
| Engine | One engine object per server, so the models load once (on the first page) |
| Checks on the draft | `schema.validate_draft` (`INVALID_LIVE_OCR_DRAFT` otherwise), then `check_live_draft`: `pages[].page_ref` equals the refs given; every field is `UNVERIFIED` with no `corrections` |
| `processed_at` | Overwritten with DayOne's clock |
| Stale results | Existing service rule: discarded if the active page IDs changed during OCR (retake replacement, page move) |
| Duration | One OCR process for the server (models loaded at startup, and again after a timeout kills the process), 2 to 4 CPU threads (`DAYONE_OCR_CPU_THREADS`, default 4). Each page is bounded by `DAYONE_OCR_TIMEOUT_SECONDS` (default 120) and each document by `DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS` (default 600). With the `medium` models on the test laptop, the evaluation measured a median of 6 to 13 s per page depending on image quality, at most 21.6 s ([ocr-evaluation.md](./ocr-evaluation.md#time-and-failures)); in live checks a one-page document reached review 23 s after its grouping window closed, and a two-page document 31 s after. OCR runs on its own extraction thread, so WhatsApp inbound and outbound keep running; progress (page N of M) is shown on the document. Outbound messages stay held regardless |

| Code | Kind | Meaning |
|------|------|---------|
| `RETAKE_REQUIRED` | Permanent | Photo too small, dark, overexposed or unreadable (checked before OCR). The French message is shown to the reviewer, who can request a retake. Nothing is sent automatically |
| `INVALID_LIVE_OCR_DRAFT` | Permanent | The draft built from the OCR failed `schema.validate_draft` |
| `OCR_DEPENDENCY_MISSING` | **Temporary** | Our installation, not the photo: logged, the document stays queued |
| `OCR_TIMEOUT` | Permanent | Every page that could not be read went over the page or document timeout. The OCR process is killed and a new one starts for the next page; a document with at least one readable page gets a draft, with the unread page flagged |
| `OCR_FAILED` | Permanent | Any other exception during OCR. Only the exception type is logged, because messages may quote page text. Not retried, because it would rerun the OCR on the same pages every second |
| `MEDIA_NOT_FOUND` | Permanent | A page file could not be resolved |
| `EXTRACTOR_SCHEMA_MISMATCH`, `EXTRACTOR_PAGE_MISMATCH`, `EXTRACTOR_CLAIMS_VERIFICATION` | Permanent | The draft failed our checks |

**Schema stays at v1.0.** A field outside the 12 agreed fields is rejected as `EXTRACTOR_SCHEMA_MISMATCH`, unless it is in the optional `extended` object (catalog `excel-ext-1`, [schema.md](./schema.md#extended-fields-optional-extended-key-catalog-excel-ext-1)), which `validate_draft` checks field by field. An extractor may omit `extended`; it may not claim an extended field was verified (`EXTRACTOR_CLAIMS_VERIFICATION`). The other v1.0-compatible extensions (optional `source.readings` and `source.normalization`, empty `encounters`, `pages[].grid`) are described in [schema.md](./schema.md#several-visits-and-several-pages).

## Explicitly unmapped

- **Layout ID.** Specimen pages declare `ma-fiche-surveillance-grossesse-v1`, although the specimen is a different template (decision D7 in `docs/excel-field-mapping.md`). The draft `notes` say so.
- **Pages other than the cover and the pregnancy grid** give no v1.0 values and no encounter: a document of cover, history or delivery pages has `encounters: []`. Identification, delivery and newborn post-partum pages give only `extended` values; maternal post-partum pages give nothing. A grid page whose table is not found goes to review (`GRID_NOT_FOUND`), and gets its own `MANUAL` encounter, sourced to that page, even when other pages gave visits. Several visits per page and several pages per document follow [schema.md](./schema.md#several-visits-and-several-pages): one encounter per populated column, the same slot on two pages merged, and disagreements flagged `CONFLICT_ACROSS_PAGES` with every page's reading in `source.readings`.
- **No automatic retake.** `RETAKE_REQUIRED` goes to the reviewer.
- **HTTP.** `dayone/extraction_http.py` is kept for a future remote extractor, but nothing uses it. `http://10.201.10.1:8001` must not be set as `DAYONE_EXTRACTOR_URL`: it is a DayOne instance, and the URL is rejected anyway because plain `http` is accepted only on `127.0.0.1`.

## Questions still open

Answered by our implementation: engine (PaddleOCR primary, Tesseract optional, never both per field), unknown pages (sections from OCR text; unread fields need review), multiple visits (one encounter per visit column with its slot), versioning (`extractor_version` names the engine, the models and the parser version), PII entries (carry `page_ref`), database reset (none).

1. **Specimen layout (D7).** Should specimen pages get their own layout ID?
2. **Retake.** Should `RETAKE_REQUIRED` send the retake request to the midwife automatically, or stay a reviewer decision?
3. **Confidence.** The schema's `KNOWN` floor is 0.75. Local OCR uses 0.97 (medium models) or 0.90 (mobile models), tuned on the specimen ([ocr.md](./ocr.md)). The scores are **not calibrated**: in the held-out evaluation, wrong values scored 0.97 or more in every condition, and 8 of 979 written values on simulated photos were wrong but `KNOWN` ([ocr-evaluation.md](./ocr-evaluation.md#scores)). Should KNOWN values from photos be shown for confirmation one by one, rather than only in the summary?
4. **Real photos.** The evaluation used renders of the synthetic specimen PDF and simulated photo copies. Can we get consented, synthetic handwritten photos to measure real handwriting?

## To run it

Install as in [ocr.md](./ocr.md), then use a separate database:

```powershell
.venv\Scripts\python -m dayone --extractor paddle --db var/dayone-ocr.sqlite3
```

Use synthetic specimen pages only. The OCR runs locally, and no page leaves the machine.
