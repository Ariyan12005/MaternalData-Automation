# Local OCR: engine choice, setup and measured results

**Status (2026-10-04).** `--extractor paddle` runs PaddleOCR locally on the CPU and turns the specimen pregnancy grid into one encounter per visit column. It has been run on renders of the synthetic specimen PDF and on simulated phone-photo copies, **not on real phone photos**. The fixture extractor stays the default. No page leaves the machine.

**Evaluation** ([ocr-evaluation.md](./ocr-evaluation.md), checked ground truth of 80 pages, development patients 1–5 and held-out patients 6–10): on clean renders, 0 wrong values marked KNOWN out of 667 written values, and 72 % (development) and 67 % (held-out) correct KNOWN. On three simulated photo conditions, 8 wrong KNOWN out of 979 (0.8 %) and 37–66 % correct KNOWN. Every visit column was found, with no extra or shifted column; no blank was filled in. Median 6–13 s per page, no page failed. Scores are not calibrated.

| Part | File |
|------|------|
| Engines (PaddleOCR primary, Tesseract optional) and model download | `dayone/ocr_engines.py` |
| Parser: page sections, visit grid, cell reading, cross-checks, privacy | `dayone/live_ocr.py` (started from Fatma's extractor on `origin/pinar`, `ff04285` to `1e4643c`) |
| Extraction contract: media paths, errors, draft checks | `dayone/live_ocr_adapter.py`, [extractor-contract.md](./extractor-contract.md) |
| OCR process with page timeout, image preparation, extraction thread | `dayone/ocr_process.py`, `dayone/ocr_preprocess.py`, `dayone/extraction_pool.py` |
| Evaluation against the checked ground truth (development / held-out) | `tools/ocr_evaluate.py`, `eval/specimen-ground-truth.json`, [ocr-evaluation.md](./ocr-evaluation.md) |
| Earlier evaluation against the PDF text layer (tuning) | `tools/ocr_specimen_eval.py` |

## Engine choice

PaddleOCR is the primary engine. Tesseract stays optional. One engine reads every field; the two are never combined per field.

| | PaddleOCR | Tesseract |
|---|---|---|
| Install on this laptop | Works in a Python **3.12** `.venv` (no PaddlePaddle wheel exists for Python 3.14, the system Python). `paddlepaddle` 3.3.1 crashes on this CPU in oneDNN (`ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`); 3.2.2 works and is pinned | Not installed: it is an external program, with no pip wheel. Not run here |
| Results on the specimen | Measured (below) | Not measured |
| History on `origin/pinar` | `ff04285` (the version Fatma's service runs) used PaddleOCR plus Tesseract | Her later commits (`aa1d937` to `1e4643c`) use Tesseract alone |

The choice rests on what installs and what was measured here, not on the latest commit. Tesseract can be enabled later with `--extractor tesseract` once installed. It uses the strictest KNOWN threshold (0.97) until it has been measured.

### PaddleOCR model sets

| `DAYONE_OCR_MODELS` | Detection + recognition | Size | Time per page | KNOWN threshold |
|---|---|---|---|---|
| `medium` (default) | `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec` | 59 + 73 MB | 7 to 17 s | 0.97 |
| `mobile` | `PP-OCRv5_mobile_det` + `latin_PP-OCRv5_mobile_rec` | 5 + 8 MB | 2.5 to 6 s | 0.90 |

Times are for a 1654 × 2339 page on the test laptop (AMD Ryzen 5 8645HS, 13.8 GB RAM, Windows, CPU only, oneDNN on). The first page also loads the models. `medium` reads more handwriting correctly at its threshold (308 against 262 correct KNOWN values on the same 14 pages). `mobile` is the fallback for a slower machine.

## Setup (Windows, PowerShell)

Everything goes into the project `.venv`; no global Python package is changed. uv installs a private Python 3.12 if needed.

```powershell
$env:UV_LINK_MODE = "copy"                      # OneDrive rejects the hardlinks uv uses by default
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-ocr.txt
.venv\Scripts\python -m dayone.ocr_engines download            # medium models, about 133 MB
.venv\Scripts\python -m dayone.ocr_engines download --models all   # optional: mobile models as well
.venv\Scripts\python -m dayone.ocr_engines check
.venv\Scripts\python -m dayone --extractor paddle --db var/dayone-ocr.sqlite3
```

- **Tested versions.** Direct dependencies are pinned in `requirements-ocr.txt`: paddlepaddle 3.2.2, paddleocr 3.7.0, paddlex 3.7.2, numpy 2.3.5, opencv-contrib-python 4.10.0.84, pillow 12.3.0, and pypdfium2 5.13.0 (evaluation only). Every package of the tested environment is in `requirements-ocr.lock.txt`. Python 3.12.14, uv 0.12.19.
- **Models.** `download` fetches the official PaddleOCR models from Hugging Face (PaddlePaddle organisation, Apache-2.0) into `var/ocr-models/paddlex/official_models/<model>/`. Set `DAYONE_OCR_MODELS_DIR` to use another folder. `var/` is git-ignored. This is the only step that needs the network.
- **No download at runtime.** The server loads the models from those folders, sets `HF_HUB_OFFLINE=1`, and refuses to start if a model or package is missing (`check` lists what is missing).
- **OneDrive.** Keep `UV_LINK_MODE=copy`. If OneDrive locks a file during an upgrade ("Access is denied"), delete `.venv` and recreate it.
- **The tests need none of this.** `python -m unittest discover -s tests` runs on the system Python with mocked OCR.

### Optional: Tesseract

```powershell
winget install --id UB-Mannheim.TesseractOCR -e
```

Then put `fra.traineddata` (from [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)) in `var/ocr-models/tessdata/`, or select French during installation. If `tesseract.exe` is not on `PATH` or in `C:\Program Files\Tesseract-OCR\`, set `DAYONE_TESSERACT_CMD`. Run with `--extractor tesseract`. It has not been measured on the specimen.

## How a page becomes a draft

1. **Photo check before OCR** (Fatma's rules): too small, dark, overexposed or unreadable gives `RETAKE_REQUIRED`, which goes to the reviewer.
2. **OCR** returns text boxes with confidences. Text is normalised (NFKC), and the box positions are straightened using the column headers (photos are rarely level).
3. **Page section** from the printed text, never from the file name: the visit grid means `current_pregnancy`. Otherwise printed labels are counted for each section (`cover`, `identification`, `current_pregnancy`, `delivery`, `postpartum`, `newborn`). Two labels identify a page; one label or a tie is `SECTION_UNCERTAIN`, and no label is `UNKNOWN_LAYOUT`. Both go to the reviewer, who can choose the section.
4. **Grid.** The 9 column headers (Visite 1–3 of trimesters 1 and 2, then 7th–9th month) give the slots `T1_V1` … `M9`. Header misreads such as "Viste 3" or "Béme mois" are tolerated. The row labels give the rows. Each text box goes to the cell that contains its centre. A box that overlaps two columns, each by more than a quarter of its width (e.g. "62 64" read as one line), is split into words placed by position, and both cells are flagged `OCR_TEXT_SPANS_COLUMNS`: values of two visits are never joined. A column is a visit when OCR read something other than a dash in any of its rows. A column with ink but no text is not a visit: the page goes to review (`GRID_COLUMN_UNREAD`), because the uncalibrated blank-cell check also sees ink in some empty cells. Each visit becomes one encounter, dated or not, and each field's `source` carries the row label, column and box. A visit-like table on a page whose labels say delivery or postpartum is uncertain and gives no visit.
5. **Cell reading.** A field is `KNOWN` only when its text gives exactly one valid value, nothing was substituted or left over, and the confidence reaches the model set's threshold. Otherwise it is `NEEDS_REVIEW`, with the suggested value when there is exactly one reading:
   - `|` is either the cell border or a handwritten 1, so both readings are tried (`|75.2` could be 75.2 or 175.2: no suggestion; `|139/89`: 139/89 suggested).
   - Letters read as digits (`3l`, `7º.7`, `202b`) and a slash read as 1 (`05106/2025`) are corrected, but always reviewed.
   - TA in cmHg (`12/7`, `12/7,5`) becomes 120/70 (120/75) mmHg, recorded in `source.normalization`. `13/60` mixes cmHg and mmHg (a lost digit), so it gets no value. A date is never read as a TA: `10/05/2025`, and pairs with a leading zero such as `10/05`, are `BP_LOOKS_LIKE_DATE`. A diastolic that is not below the systolic (`11/12`) is `BP_ORDER_IMPLAUSIBLE`.
   - Gestational age keeps its days (`28 SA + 3 j`, `28SA3j`, `28+3` → 199 days, recorded in `source.normalization`). `283`, `28 SA 9 j` and decimal weeks (`28.5`, `GA_FORMAT_AMBIGUOUS`) get no value.
   - A dash is `NOT_PROVIDED` (`MARKED_DASH`); an empty cell is `NOT_PROVIDED`, except the visit date, which is required.
   - An unclear date (`1?/05/2025`, `10/05` without a year, `31/02/2025`) gets no value; the visit stays, and its date blocks registration.
6. **Several pages.** Each grid page is read on its own, then the same slot on several pages becomes one encounter. Agreeing readings keep the best one; a different value, or a value against a verified blank cell, is `CONFLICT_ACROSS_PAGES` with no value. Every page's reading stays in `source.readings`. Cover fields and the DDR are merged the same way. Cover, history, delivery, postpartum and newborn pages never give a visit.
7. **Cross-checks** demote values to review: visit dates must increase; dates and gestational ages must agree with the DDR; each visit implies a conception date (date minus gestational age), and a visit far from the others is flagged even without a DDR.
8. **Cover.** File number, facility name and midwife code are always `NEEDS_REVIEW` (`OCR_CONFIRM_REQUIRED`): no rule can catch a misread digit in a file number or a letter in a name. A link key is read only after its printed label (`N° de la fiche`, or a line starting with `CM :` or `Code … :`); it is never taken from another page or from a file name.
9. **Privacy.** Names, phone numbers, ID numbers, addresses and the staff row ("Examen fait par") are never read; their labels are listed in `pii_detected` as categories. Recognised text is never logged.
10. **Extended fields** (`dayone/ocr_extended.py`, [schema.md](./schema.md#extended-fields-optional-extended-key-catalog-excel-ext-1)). They are read from the same OCR boxes and never block registration:
    - **After a printed label** (age, education, gestation, parity, living children, height, delivery date and gestational age, newborn sex, birth weight and head circumference, consultation date): the value on the same line, with the writing line's underscores and dots removed. Nothing read after the label is `OCR_NO_TEXT` (review), never `NOT_PROVIDED`: on page `-58`, OCR stretched the label box of *Nombre d'enfants vivants* over a handwritten `1` it did not recognise, so the paper right of the box was blank while the value was not.
    - **Table cells.** *Avortement* × *Nombre* (columns bounded by the next header); *Accouch. 1–5* × *Date* / *Modalité d'extraction* (one item per column with writing); lab rows *Hémoglobine*, *Bilan glycémique*, *Albuminurie* of the visit grid, per column. These use the blank-cell ink check, so an empty cell can be `NOT_PROVIDED`. Lab rows never make a column a visit; writing in a non-visit column is `LAB_WITHOUT_VISIT`.
    - **Checkboxes.** The label is found by OCR; the printed square is searched just left of where the label text starts (a window of −2.6 to +1.8 label heights across and ±1 height down, on the page thumbnail). Dark pixels are those clearly below the window's paper level; the best square outline of 0.5–1.3 label heights, with every side at least 80 % dark, is the box. Its interior ink share (border excluded) gives `MARKED` (≥ 0.18), `UNMARKED` (≤ 0.04) or `CHECKBOX_UNCLEAR`. No square: `CHECKBOX_NOT_FOUND`. A choice (delivery mode, feeding) is `KNOWN` only with exactly one marked box and every other box clearly empty; several marked boxes are `CHECKBOX_MULTIPLE_MARKED`, none is `NOT_PROVIDED` with `CHECKBOX_NONE_MARKED`. Box values have no OCR confidence; each box's state and ink are kept in `source.checkboxes`.
    - **Always reviewed:** handwritten counts (gravidity, parity, living children, abortions) and free text (education, previous delivery mode). Counts are single digits with nothing to check them against, and `1` was read as `7` at confidence 0.977 on page `-02`.
    - Newborn consultation pages are kept only when their title gives the period (*précoce* / *tardif*). The delivery page's single newborn block is newborn 1.

## How the server runs OCR

- **In the background.** The server's worker loop handles WhatsApp inbound and outbound jobs every second and queues documents whose grouping window has closed. Extraction runs on a separate extraction thread (`dayone/extraction_pool.py`), one document at a time, so WhatsApp jobs and the back-office never wait for OCR. A document is never extracted twice at once. The document shows its progress (page N of M).
- **In a separate process.** One OCR process (`dayone/ocr_process.py`) loads the models once, at startup, within `DAYONE_OCR_START_TIMEOUT_SECONDS` (default 300), then reads one page at a time. Each page is bounded by `DAYONE_OCR_TIMEOUT_SECONDS` (default 120, minimum 5) and each document by `DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS` (default 600); pages not started in time are `OCR_TIMEOUT`. A page over its time is stopped by killing the process; a new process starts, and loads the models again, for the next page. An engine crash marks the page `OCR_FAILED` and the process is replaced.
- **Pages not read.** An unread page is flagged on the draft (`review_reason`) and the other pages are still read. When no page can be read, the document goes to `PROCESSING_FAILED` with each page's reason, then to a retake or manual entry.
- **Retries and stale results.** A missing install or an OCR process that cannot start leaves the document `PENDING_AI`; it is tried again after 30 s. If a page is replaced or a page section is changed during extraction, the result is discarded and the document is extracted again.
- **Work files.** Each page's prepared image is written to a work folder in the local temporary directory (`DAYONE_OCR_TMP_DIR`, default `%TEMP%\dayone-ocr`), never under the repository, and deleted after the page. Each server has its own run folder there, with an owner file locked while it runs. At start, a server removes only the run folders of servers that have stopped; another running server's files are never touched.
- **Restart.** Queued documents are stored and are extracted again after a restart. An OCR process left behind by a killed server exited on its own within about 18 s in the live check ([ocr-evaluation.md](./ocr-evaluation.md#checks-performed)).
- **Logs.** Page counts, durations, outcome codes and exception type names only: no recognised text, value or traceback.

## Tuning measurements (synthetic specimen only)

These runs chose the thresholds and rules, on the same pages they report, so they are not an independent result. The evaluation against the checked ground truth, with a held-out split, is in [ocr-evaluation.md](./ocr-evaluation.md); it found wrong KNOWN values on simulated photos that these runs did not have.

Measured on 2026-10-03 with `tools/ocr_specimen_eval.py`, which scores the draft against the text layer of `dossiers_specimen_10_patientes.pdf`. The pages tested are the two covers `-01` and `-09`, the pregnancy grid of each of the ten fictitious patients (`-03`, `-11`, … `-75`), and simulated phone-photo copies of `-03` and `-11` (rotated 1.5°, blurred, darkened, downscaled to 1280 px, JPEG quality 60). Reports are written to `var/ocr-eval/` (git-ignored). They show clinical values only; file numbers and facility names are reported as right or wrong, and names are never read.

| Outcome per field (880 checks) | `medium`, threshold 0.97 | `mobile`, threshold 0.90 |
|---|---|---|
| Correct value, `KNOWN` | **309** | 262 |
| **Wrong value marked `KNOWN`** | **0** | **0** |
| Blank or dash on paper, not filled in | 438 | 446 |
| Blank on paper, sent to review (ink seen or OCR noise) | 8 | 0 |
| `NEEDS_REVIEW` with the right value suggested | 67 | 85 |
| `NEEDS_REVIEW` with no value or a wrong one | 58 | 87 |
| Visit column missed / extra | 0 / 0 | 0 / 0 |

The `medium` column was measured again on 2026-10-04 with parser `grid-3` (multiple visits and pages, blank-cell ink check); the `mobile` column is from parser `grid-2`. Of the 8 blank cells sent to review, 5 are faint marks that the uncalibrated ink check takes for writing, and 3 are OCR noise (`�,?`). In an earlier `grid-3` run, unread ink alone made a column a visit, and it produced 3 phantom undated visits (page `-75` column `M9`, and the photo-like copy of `-03`). Such columns now send the page to review (`GRID_COLUMN_UNREAD`) instead.

- Every expected visit column was found on all 12 grid pages, including both photo-like copies. With `medium`, the DDR was correct and KNOWN on 8 of the 12 grid pages and went to review on the other 4.
- Clear pages give 24 to 37 correct KNOWN values per page with `medium` (page `-51`, with few filled cells, gives 6). The photo-like copy of patient 2's page, whose handwriting is the messiest, gives 4 KNOWN and 40 values for review: degraded handwriting mostly goes to review.
- Recognised content that went wrong, and how it was caught: `37 SA` for 31 (flagged by the DDR and by the other visits), `37` for 31 cm and `87.1` for 81.1 kg (confidence 0.95 and 0.91, below the 0.97 threshold), `177/61` for 117/61 (0.95), `720/63` for 120/63 (not a valid TA), `04/1/2025` for 04/11/2025 (out of order and inconsistent with the DDR), `13/60` for 113/60 (mixed units).
- **Why 0.97.** At the schema's floor (0.75, or 0.85 as Fatma used), `medium` gave 5 confident errors on these pages, all with confidence between 0.88 and 0.96, while 300 of 342 correct grid values scored 0.97 or more. The threshold was chosen on these same pages, so it is a calibration on synthetic data, not a guarantee.
- On the covers, the file number of `-01` was read with one digit wrong ("1" as "7", confidence 0.96). This is why cover values are always reviewed.

Reproduce (about 4 minutes for `medium`):

```powershell
.venv\Scripts\python tools/ocr_specimen_eval.py --models medium
$env:DAYONE_REAL_OCR = "1"; .venv\Scripts\python -m unittest tests.test_real_ocr   # one page, temporary database
```

The grid numbers above are from parser `grid-3`. They were not measured again with `grid-4`, which adds the lab rows and the extended fields but does not change how visits are found.

### Extended fields

Measured on 2026-10-04 with `tools/ocr_specimen_eval.py --extended` (`medium`, threshold 0.97, parser `grid-4`). It covers the 50 pages of types 2, 3, 4, 6 and 8 of the ten fictitious patients (identification, pregnancy grid, delivery, newborn consultations) and photo-like copies of `-02`, `-04` and `-06`. Truth is the PDF text layer and checkbox drawings. Report: `var/ocr-eval/report-extended-paddle-medium.md`.

- **Wrong value marked `KNOWN`: 0** on all 22 fields. No written value was claimed blank.
- Correct `KNOWN`: checkboxes 21/21 for feeding and 11/11 for desired pregnancy, 10/11 for consanguinity and for delivery mode; albuminuria 53/55; previous delivery dates 16/18; consultation dates 14/21; hemoglobin 12 and glucose 7 of the 17 and 10 written values; maternal age 9/11; head circumference 8/11; delivery gestational age 4/11; newborn sex and birth weight 1/11 each (low confidence on `F`/`M` and `3587g`, so these go to review with the right value).
- Always-reviewed counts and free text: the right value was proposed for 54 of 59 (gravidity 11/11, parity 11/11, abortions 2/2, living children 9/11, education 10/11, previous delivery mode 16/18). The misses were `1` read as `7` (0.977), nothing read on `-58`, `Lycre`, `Joie basse` and `Voie basce`. Empty *Avortement* cells were verified blank on 9 pages, and empty lab cells on 38 (hemoglobin) and 45 (glucose) visit columns.
- **Checkbox ink.** Marked boxes n = 45: 0.224–0.816 (median 0.571). Empty boxes n = 84: 0–0.125 (median 0). The run used `MARKED` ≥ 0.12, and the 0.125 empty box on the photo-like copy of `-04` produced `CHECKBOX_MULTIPLE_MARKED` (review, not a wrong value). The threshold is now 0.18. A recheck of `-02`, `-04`, `-06` and their photo-like copies with 0.18 (`report-extended-paddle-medium-recheck.md`) gave identical results except `-04` photo-like, now `CHECKBOX_UNCLEAR` with the right value.
- **Photo-like copies.** `-02`: consanguinity box `CHECKBOX_UNCLEAR`, `Lycre`, `Voie basce`, the rest right. `-04`: delivery mode to review, the rest right. `-06`: all right.
- 4.5–18.7 s per page.
- Column-by-column results are in [excel-field-mapping.md](./excel-field-mapping.md#evidence).

```powershell
.venv\Scripts\python tools/ocr_specimen_eval.py --extended   # 53 pages, 4.5-18.7 s each
```

## Mocked tests (no OCR engine)

`tests/test_extended.py` checks the extended fields without an engine: schema validation of every section, number and unit parsing, the checkbox reader on drawn boxes (marked, empty, faint, missing, several marked), reading after labels and in table cells, nothing read being review rather than blank, family history never read as the woman's own, no identifier in the extended object, two photos that disagree, the 31-column catalog against the CSV headers (read-only) and the coverage table in `excel-field-mapping.md`, and review through the service and HTTP route (extended values stay `UNVERIFIED` after the final confirmation; live drafts cannot claim them verified). `tests/test_live_ocr.py` checks the parser on synthetic text boxes: grid detection, tilted pages, misread headers, every cell rule above, cross-checks, the cover, privacy (names absent from the draft), pages without a grid, and the photo checks. `tests/test_live_ocr_adapter.py` runs the service with a fake engine: WhatsApp media paths, retake and stale results, a missing install keeping the document queued, a crash going to manual entry without logging page text, contract violations, the server refusing an incomplete install, and startup without a database reset and with the outbound hold kept. These tests say nothing about recognition quality; the section above does.

## Limitations

- **No real photos.** The specimen values are a handwriting-style font, not handwriting, and the "photos" are simulated. Real phone photos (perspective, shadows, folds, ink) are untested. On the simulated photos, 8 of 979 written values were wrong but `KNOWN` (scores 0.973–0.996), and the final confirmation records unopened `KNOWN` values as confirmed.
- **One layout.** Only the specimen is read: its "Grossesse actuelle" grid and cover, and for extended fields its identification, delivery and newborn consultation pages. The pink booklet (`1-x.jpg`) is a different layout and was not OCR'd. The draft still declares `ma-fiche-surveillance-grossesse-v1` (decision D7).
- **Thresholds were tuned on 14 synthetic pages**, the same ones the tuning section reports on. The checkbox ink thresholds were likewise set on the 50 extended pages, from a margin of 0.125 (empty) to 0.224 (marked). Every specimen patient was among those pages, so the held-out evaluation is independent only for its two new photo conditions. Scores are not calibrated probabilities.
- **A wrong suggestion can be pre-filled** in a `NEEDS_REVIEW` field (59 such fields with `medium`). The reviewer must compare with the photo.
- **Speed.** A median of 6 to 13 s per page on the CPU, at most 21.6 s ([ocr-evaluation.md](./ocr-evaluation.md#time-and-failures)), one page at a time in a separate OCR process. WhatsApp jobs keep running meanwhile, but documents queue behind each other: an 8-page booklet takes one to two minutes. A page over the page timeout is not read, and after a timeout the next page waits for the models to load again.
- **Tesseract** has not been installed or measured.
