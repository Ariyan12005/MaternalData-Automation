# OCR evaluation against a checked ground truth

This page describes how the local OCR (`--extractor paddle`) was scored, and what came out. It covers the synthetic specimen only. **No real phone photo has been tested.**

| Part | File |
|------|------|
| Ground truth (80 pages, checked) | `eval/specimen-ground-truth.json` |
| Builds the ground truth and its verification sheets | `tools/ocr_ground_truth.py` |
| Scores the OCR against it | `tools/ocr_evaluate.py` |
| Reports (git-ignored) | `var/ocr-eval/eval-development-medium.md`, `var/ocr-eval/eval-held_out-medium.md` |

## Ground truth

**Source.** `data/Paper Registry/dossiers_specimen_10_patientes.pdf`: 10 fictitious patients × 8 pages. Its text layer gives a candidate for every written value and checkbox. The provided CSV/XLSX is **not** used: nothing shows that its rows correspond to these pages (`registry_csv_used: false`).

**Check.** Each candidate was compared by eye with crops of the rendered page (verification sheets made by `tools/ocr_ground_truth.py sheets`, written to the temp folder). This covered all 60 supported pages: every grid cell of the 10 pregnancy grids, the 10 covers, the 10 identification pages, the 10 delivery pages (digits checked again at high zoom) and the 20 newborn pages. The 20 post-partum mother pages were checked by title only, because they are not read. The check was done by the AI coding assistant (recorded in the file as `"by"`). **A human spot-check is still pending.**

Five entries are recorded in `corrections`. Two give the canonical value of `Pos +` (see the split section below). Three change a candidate: on pages 18, 58 and 66, the handwriting font has no accented letter, so the render shows a gap where a person reads `Collège` or `Césarienne`. The truth is the word a person reads; the gapped spelling is accepted as an alternative. A `1` that looks like a `7` at low resolution (page 1 file number, page 44 "31 cm") was checked at full resolution and is a `1`: that is an OCR error, not a truth correction.

**Cell states.** `value` (written), `dash` (a dash, i.e. not provided), `blank` (nothing written), `illegible` (written but unreadable by a person: none exist in the specimen), `choice` (checkbox result: `MARKED`, `UNMARKED`, an enum value, or none), `not_on_layout` (the layout has no such field, e.g. the midwife code on this cover).

**Patient keys.** File number and facility name are stored only as `sha256("dayone-gt-v1|<field>|<value>")` of the schema-normalised value (facility names lowercased without accents or spaces). No name or other identifier is in the file.

**Contents.** 80 pages; 630 grid cells (284 written, 7 dashes, 339 blank) over 9 columns × 7 rows (TA counted as one cell, scored as systolic and diastolic); 55 visit columns; lab rows for every column; the DDR; 2 cover keys per cover; 224 extended items (168 written, 48 checkbox choices, 8 blank).

### Development and held-out split

Every patient had already been used while tuning: the 0.97 KNOWN threshold on 14 pages (all ten grids, two covers, two photo-like copies) and the 0.18 checkbox ink threshold on 50 pages. A clean held-out set therefore cannot be made from these pages alone. The split is:

| Split | Patients | Conditions |
|---|---|---|
| Development | 1–5 (pages 1–40) | `clean` renders, `photo_like` (the simulated photo used while tuning: 1.5° rotation, blur, darker, 1280 px, JPEG 60) |
| Held-out | 6–10 (pages 41–80) | `clean`, and two simulated photo conditions **never used for tuning**: `perspective_shadow` (keystone 4–6 %, light falling from 100 % to 62 % across the page, blur 0.8 px, long side 1600 px, JPEG 70) and `lowres_noise` (±2.5° tilt, long side 1100 px, sensor noise σ = 6, JPEG 55) |

The held-out clean pages are **not** untouched: their five grids were among the 14 threshold-tuning pages, and their identification, delivery and newborn pages among the 50 checkbox-tuning pages. Only the two new photo conditions on patients 6–10 are unseen. The reader was frozen before either split was run: both reports carry the same code fingerprint (sha256 of `dayone/*.py`), and no reader code changed after any result was read.

Two fixes were made to the **scorer** (not the reader) during the held-out run, after it crashed: a blank neighbouring cell made the shifted-value check compare a number with nothing, and the truth text `Pos +` (page 67, albuminuria, two cells) is not accepted by the schema parser, so the ground truth now records its canonical value `POSITIVE` with the reason. Every written truth cell (599) was then checked to parse. Neither fix can change a development result: page 67 is held-out, and the first fix only affects comparisons that crashed.

## Comparison rules

| Field kind | Rule |
|---|---|
| Numbers (weight, height, fundal height, TA, counts, birth weight, head circumference, hemoglobin, glucose) | Truth text parsed by the schema (units, decimal comma). Equal within 10⁻⁶ |
| TA | The truth cell `104/74` gives two fields, systolic and diastolic, each scored on its own |
| Gestational age | Parsed to days (`28 SA + 3 j` = 199). Equal days |
| Dates (visit, DDR, delivery, previous delivery, consultation) | Parsed to ISO dates. Equal dates |
| Labs (syphilis, HIV, albuminuria) | Schema enum after parsing (`Négatif`, `-`, `+`). Equal values |
| Free text (education, previous delivery mode) | Lowercased, accents and spaces removed, then equal; or equal to an accepted alternative of the truth |
| Checkboxes and choices | Equal enum value. A truth with several marked boxes is right only as `NEEDS_REVIEW` |
| Patient keys | Hash of the read value equals the truth hash |
| Dash or blank on paper | Right when the field is `NOT_PROVIDED` (or absent, when no visit was created for that column) |

**Outcomes for a written value:** correct `KNOWN`; **wrong `KNOWN`**; `NEEDS_REVIEW` with the right value suggested; `NEEDS_REVIEW` with a wrong or no suggestion; claimed blank or illegible; not found (no field: e.g. the column was not taken as a visit).

**Outcomes for a blank or dash:** kept blank; sent to review (or marked illegible); **filled in as `KNOWN`**.

Denominators: grid and lab rows are counted in visit columns and in any column where the reader created a value; blank cells of columns that are not visits, with nothing created, are left out of the field tables (the visit-column line covers them). The cover's midwife code is not on this layout and is scored as a blank.

**Not claimed.** The reports list how many values fall in OCR score bands, but the scores are **not calibrated**: a few hundred values from one synthetic font, with thresholds tuned on the same patients, cannot support a probability claim.

## Results

Runs finished on 2026-10-04: PaddleOCR 3.7.0 with the `PP-OCRv6_medium` detection and recognition models (PaddlePaddle 3.2.2, Python 3.12), KNOWN threshold 0.97, one CPU worker, page timeout 120 s, reader fingerprint `49e90fa3dddd79bc` for both splits. Development: 70 page reads; held-out: 100. Commands: `python tools/ocr_evaluate.py --split development` and `python tools/ocr_evaluate.py --split held_out`.

### What the reviewer sees

| Split, condition | Correct KNOWN / written | Right value suggested (review) | Wrong or no suggestion (review) | Written value claimed blank | **Wrong KNOWN** | Blank kept blank / blank | Blank sent to review | **Blank filled KNOWN** |
|---|---|---|---|---|---|---|---|---|
| development, `clean` | 255/355 (72 %) | 88 | 12 | 0 | **0** | 101/110 (92 %) | 9 | **0** |
| development, `photo_like` | 133/355 (37 %) | 116 | 103 | 1 | **2** | 94/110 (85 %) | 16 | **0** |
| held-out, `clean` | 209/312 (67 %) | 81 | 22 | 0 | **0** | 86/92 (93 %) | 6 | **0** |
| held-out, `perspective_shadow` | 205/312 (66 %) | 81 | 23 | 0 | **3** | 68/92 (74 %) | 24 | **0** |
| held-out, `lowres_noise` | 136/312 (44 %) | 96 | 77 | 0 | **3** | 54/92 (59 %) | 38 | **0** |

- **Wrong KNOWN:** 0 of 667 written values on clean renders; 8 of 979 (0.8 %) on simulated photos. Every other value that was not right went to review. No written value was "not found" (every one had a field).
- **Missing and illegible status:** 1 written value of 1646 was claimed blank (development photo, page 10, previous delivery date, flag `CELL_BLANK`). No blank or dash was ever filled in as KNOWN (0 of 496). No value was marked illegible, and the specimen has no illegible cell, so **illegible detection was not measured**.
- **Review load:** the share of written values a person must still look at grows from about a third (clean) to more than half (`lowres_noise`).

The 8 wrong KNOWN values (the clean render of each cell was read right, at score 1.0):

| Condition | Page, column | Field | Truth → read | Score |
|---|---|---|---|---|
| development `photo_like` | 26 | previous delivery date | 2023-07-10 → 2023-01-10 | 0.991 |
| development `photo_like` | 35, T2_V1 | TA systolic | 109 → 104 | 0.983 |
| held-out `perspective_shadow` | 51, T2_V1 | gestational age | 16 SA (112 days) → `6 SA` (42 days) | 0.986 |
| held-out `perspective_shadow` | 75, T1_V1 | TA systolic | 103 → 105 | 0.973 |
| held-out `perspective_shadow` | 75, T2_V1 | TA systolic | 113 → 115 | 0.991 |
| held-out `lowres_noise` | 51, T2_V1 | weight | 63.4 → 134 | 0.981 |
| held-out `lowres_noise` | 74 | consanguinity box | unmarked → marked | (no score) |
| held-out `lowres_noise` | 75, T1_V1 | TA systolic | 103 → 105 | 0.996 |

**These are saved unless someone catches them.** At the final confirmation, every visit field still `UNVERIFIED`, including KNOWN values the reviewer never opened, is recorded as `CONFIRMED` by that reviewer (`Service.confirm`). Extended fields are not (they stay `UNVERIFIED`). The summary step shows every value before confirmation; nothing else would catch a wrong KNOWN value.

### By field group

Correct KNOWN / written; wrong KNOWN in brackets.

| Group | dev `clean` | dev `photo_like` | held-out `clean` | `perspective_shadow` | `lowres_noise` |
|---|---|---|---|---|---|
| Visit grid (8 fields) | 158/185 (0) | 65/185 (1) | 123/154 (0) | 123/154 (3) | 90/154 (2) |
| Lab rows (syphilis, HIV, albuminuria) | 38/44 (0) | 17/44 (0) | 34/38 (0) | 26/38 (0) | 17/38 (0) |
| DDR | 4/5 (0) | 1/5 (0) | 4/5 (0) | 4/5 (0) | 3/5 (0) |
| Cover keys (file number, facility) | 0/10 (0) | 0/10 (0) | 0/10 (0) | 0/10 (0) | 0/10 (0) |
| Extended fields (20) | 55/111 (0) | 50/111 (1) | 48/105 (0) | 52/105 (0) | 26/105 (1) |

Cover keys are never KNOWN by design (always confirmed by a person); see patient keys below.

### By grid field

Correct KNOWN / written; wrong KNOWN in brackets.

| Field | dev `clean` | dev `photo_like` | held-out `clean` | `perspective_shadow` | `lowres_noise` |
|---|---|---|---|---|---|
| Visit date | 25/30 (0) | 13/30 (0) | 21/25 (0) | 23/25 (0) | 16/25 (0) |
| Gestational age | 24/30 (0) | 2/30 (0) | 20/25 (0) | 16/25 (1) | 7/25 (0) |
| Weight | 28/30 (0) | 10/30 (0) | 24/25 (0) | 18/25 (0) | 14/25 (1) |
| TA systolic | 26/30 (0) | 10/30 (1) | 16/25 (0) | 20/25 (2) | 18/25 (1) |
| TA diastolic | 26/30 (0) | 11/30 (0) | 16/25 (0) | 22/25 (0) | 19/25 (0) |
| Fundal height | 19/25 (0) | 14/25 (0) | 16/19 (0) | 15/19 (0) | 11/19 (0) |
| Syphilis | 5/5 (0) | 3/5 (0) | 5/5 (0) | 5/5 (0) | 3/5 (0) |
| HIV | 5/5 (0) | 2/5 (0) | 5/5 (0) | 4/5 (0) | 2/5 (0) |
| Hemoglobin | 5/9 (0) | 0/9 (0) | 7/8 (0) | 4/8 (0) | 2/8 (0) |
| Blood glucose | 3/5 (0) | 2/5 (0) | 4/5 (0) | 1/5 (0) | 1/5 (0) |
| Albuminuria | 30/30 (0) | 15/30 (0) | 23/25 (0) | 21/25 (0) | 14/25 (0) |
| DDR | 4/5 (0) | 1/5 (0) | 4/5 (0) | 4/5 (0) | 3/5 (0) |

Height (one per grid page) is counted in the pregnancy layout below, not in this table.

### By layout

Written values: correct KNOWN / written; wrong KNOWN in brackets.

| Layout | dev `clean` | dev `photo_like` | held-out `clean` | `perspective_shadow` | `lowres_noise` |
|---|---|---|---|---|---|
| Cover | 0/10 (0) | 0/10 (0) | 0/10 (0) | 0/10 (0) | 0/10 (0) |
| Pregnancy grid page (grid, labs, DDR, height) | 200/239 (0) | 86/239 (1) | 162/202 (0) | 156/202 (3) | 113/202 (2) |
| Identification | 24/56 (0) | 17/56 (1) | 20/50 (0) | 16/50 (0) | 9/50 (1) |
| Delivery | 14/30 (0) | 15/30 (0) | 11/30 (0) | 18/30 (0) | 10/30 (0) |
| Newborn (first days) | 10/10 (0) | 8/10 (0) | 10/10 (0) | 8/10 (0) | 2/10 (0) |
| Newborn (later) | 7/10 (0) | 7/10 (0) | 6/10 (0) | 7/10 (0) | 2/10 (0) |
| Post-partum mother | not read | not read | not read | not read | not read |

Each patient's post-partum mother page (5 per condition) was sent and created no visit and no extended field, as intended. Many identification and delivery fields are always reviewed by design (counts, free text), so their KNOWN share is low even when the suggestion is right.

### Visits and encounters

| Split, condition | Visit columns found / expected | Extra columns | Shifted values | Grid pages with exactly the right visits | Documents with the right visits | Columns flagged unread (`GRID_COLUMN_UNREAD`) |
|---|---|---|---|---|---|---|
| development, `clean` | 30/30 | 0 | 0 | 5/5 | 5/5 | 0 |
| development, `photo_like` | 30/30 | 0 | 0 | 5/5 | 5/5 | 4 |
| held-out, `clean` | 25/25 | 0 | 0 | 5/5 | 5/5 | 1 |
| held-out, `perspective_shadow` | 25/25 | 0 | 0 | 5/5 | 5/5 | 4 |
| held-out, `lowres_noise` | 25/25 | 0 | 0 | 5/5 | 5/5 | 5 |

A shifted value is a value read in the wrong visit column (equal to the neighbouring column's truth). A document groups the patient's 8 pages; "right visits" means its visit slots equal the truth's.

### Patient keys

File number and facility name are always `NEEDS_REVIEW` (never KNOWN), so the measure is whether the suggestion a person confirms is right:

| Split, condition | File number suggested right | Facility suggested right | DDR right (of which KNOWN) | Midwife code |
|---|---|---|---|---|
| development, `clean` | 4/5 | 4/5 | 5/5 (4) | not on this layout |
| development, `photo_like` | 4/5 | 3/5 | 4/5 (1) | not on this layout |
| held-out, `clean` | 4/5 | 3/5 | 4/5 (4) | not on this layout |
| held-out, `perspective_shadow` | 5/5 | 3/5 | 4/5 (4) | not on this layout |
| held-out, `lowres_noise` | 5/5 | 2/5 | 4/5 (3) | not on this layout |

The midwife code is not on the specimen cover, so every document has one extra review item for it (5 per condition).

### Time and failures

| Split, condition | Pages read / sent | Failures | Median s per page | 90th percentile s | Max s |
|---|---|---|---|---|---|
| development, `clean` | 40/40 | 0 | 12.8 | 18.6 | 21.6 |
| development, `photo_like` | 30/30 | 0 | 9.3 | 16.7 | 19.0 * |
| held-out, `clean` | 40/40 | 0 | 11.0 | 13.6 | 18.0 |
| held-out, `perspective_shadow` | 30/30 | 0 | 8.7 | 13.5 | 15.1 |
| held-out, `lowres_noise` | 30/30 | 0 | 6.4 | 11.5 | 13.9 |

One laptop CPU worker; models load in 4 to 6 s once per process. Every page was assigned the right section (170/170). \* One development `photo_like` page (page 1) took 15 126 s because the laptop went to sleep during the read; it is left out of the time columns, and its values are scored normally. Smaller, noisier images read faster because the detector finds fewer text boxes.

### Scores

Values read, by OCR score band (right / wrong value, whatever the status):

| Split, condition | < 0.90 | 0.90–0.97 | ≥ 0.97 |
|---|---|---|---|
| development, `clean` | 10 / 0 | 57 / 7 | 251 / 2 |
| development, `photo_like` | 28 / 15 | 66 / 21 | 131 / 5 |
| held-out, `clean` | 11 / 1 | 52 / 3 | 202 / 2 |
| held-out, `perspective_shadow` | 2 / 3 | 60 / 6 | 199 / 5 |
| held-out, `lowres_noise` | 17 / 11 | 72 / 12 | 140 / 3 |

Wrong values occur at ≥ 0.97 in every condition; on clean pages they were caught by other rules (cross-checks, always-reviewed counts). **The scores are not calibrated** and the UI shows them as indicative only.

## Checks performed

| Check | Result |
|---|---|
| Regression suite, project `.venv` (Python 3.12, PaddleOCR installed) | 216 tests, all pass, none skipped (121 s), including the two real-OCR tests |
| Regression suite, system Python 3.14 without OCR packages (fixture mode) | 216 tests, pass, 24 skipped (the OCR tests) |
| Real-OCR tests on their own (`tests/test_real_ocr.py`) | 2 pass (43 s) |
| Restart persistence, live (`--extractor paddle --hold-outbound`, temporary database) | A reviewed-ready document (pages 01 + 03) was unchanged after a restart: status, revision, draft, events, processing time and visit slots identical; one patient, nothing reset |
| Crash during extraction, live | The server was killed while page 11 was being read. The orphaned OCR process exited on its own within about 18 s. After restart the document was still `PENDING_AI`, was read again, and reached `NEEDS_REVIEW` with the right visits 23 s later |
| Retake during extraction, live | A retake of page 43 was requested and fulfilled 4.5 s into its extraction. The first result was discarded: one draft saved, from the new page only, with the right visits (6 slots) |
| Bounded OCR, live (`DAYONE_OCR_TIMEOUT_SECONDS=5`, page 67) | `PROCESSING_FAILED` / `OCR_TIMEOUT` 5.3 s after extraction started; the OCR process was killed (not replaced until the next page); no work folder left; the API answered in at most 17 ms meanwhile |
| UI, fixture mode | Review flow from upload to registration, retake and replacement, labels and status text, error paths, layouts at 390 px, 1100 px and full width, focus order (DOM audit) and the app's keyboard handlers |
| UI, live OCR draft | Source box on the full page and in the zoomed crop on the right cell (TA, first-trimester visit 1); no page-wide horizontal scroll at 390 px |

## Checks not performed

- **Real phone photos.** Only simulated photo conditions. No result here predicts accuracy on real photos.
- **Human spot-check of the ground truth.** The check was done by the AI coding assistant.
- **Illegible cells.** None exist in the specimen.
- **Independent held-out pages.** The held-out clean pages were seen while tuning thresholds (see the split section).
- **The pink booklet layout** (`1-x.jpg`) and any other layout: not supported, not read.
- **Tesseract** and other engines.
- **Real keyboard use by a person.** The browser automation sends untrusted key events, so Tab and Enter were checked through a DOM audit of focus order and the app's own handlers, not by pressing keys.
- **Screen readers and other browsers** than the embedded Chromium.
- **Confidence calibration** (not enough evidence; see above).
