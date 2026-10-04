# Excel / CSV field mapping (31 columns)

**Status:** paper extraction and review are implemented for the values below (catalog `excel-ext-1`); **the export is not**. No CSV row is produced: what a row is (D1), its generated ID (D2) and the pregnancy-level calculations (D3, D4, D5, D10) are still open. Of the 31 columns, 11 have their paper value extracted and evaluated, 1 is extracted but too thinly evaluated, 7 need an agreed rule across visits or items, and 12 are unavailable or awaiting a definition. **Not all 31 columns are covered.**

**Dataset:** `data/maternal_registry_synthetic.csv` (200 rows, 31 columns) and `data/maternal_registry_synthetic.xlsx` (one sheet, `maternal_registry_synthetic`, same 31 headers). Both are read-only and listed in `manifest.json`. Neither file contains a data dictionary, and `consignes-fr-en.pdf` describes the variable groups but not the column encodings.

**Code**

| What | Where |
|------|-------|
| Paper fields: scope, kind, unit, source and notes | `dayone/extended.py` (draft key `extended`, [schema.md](./schema.md#extended-fields-optional-extended-key-catalog-excel-ext-1)) |
| Reading them with local OCR (specimen layout) | `dayone/ocr_extended.py`, called by `dayone/live_ocr.py` |
| The 31 columns: category, scope, paper inputs, blocking decisions. No computation | `dayone/export_columns.py` (the coverage table below is generated from it and checked by `tests/test_extended.py`) |
| Review | back-office block *Données complémentaires*; `POST /api/documents/{id}/fields` with `scope: "extended"` |
| Evaluation | `tools/ocr_specimen_eval.py --extended` (report in `var/ocr-eval/`, git-ignored) |

**Rules for this document and the code**

- Headers are quoted exactly as they appear in the CSV.
- An encoding is stated only when the header documents it. Three headers do (`education level`, `type of delivery`, `newborn sex`); every other `0`/`1` meaning is marked **undocumented**, and nothing converts a paper value to it.
- A registry source is listed only when the label was seen on a page in `data/Paper Registry/`. Anything else is **not verified**, with candidates described in the notes, and nothing is extracted for it.
- Only values read directly from one page are extracted. No clinical value is derived, filled, defaulted or predicted, and no recommendation is made.
- Not substituted: family history for the woman's own history; *Bilan glycémique* for a fasting glucose; feeding mode at a later consultation for breastfeeding initiated at birth; a pregnancy weight for a pre-pregnancy weight or BMI; an empty box for "no".

## Definitions

**Coverage**

| Category | Meaning |
|----------|---------|
| Implemented and evaluated | The paper value at the column's scope is extracted, reviewable, and was scored with real OCR against the ground truth of the ten synthetic specimen booklets (see [Evidence](#evidence)). The CSV cell is still not produced (no export row, D1) and may need an agreed encoding or rounding, listed under *Blocking decisions*. |
| Implemented but unverified | The value is extracted and reviewable, but the evaluation has too few written values to judge the reading. |
| Requires aggregation | The paper inputs are extracted (per visit, per previous delivery), but one CSV value needs an agreed rule across them. The rule is not implemented. |
| Unavailable or awaiting definition | No verified source on the forms, or the column's meaning is not agreed (for example whether *Albuminurie* stands for proteinuria). A related paper value may be extracted, but it is never exported under this header. |

**Scope:** patient, pregnancy, visit, previous delivery, delivery, newborn, newborn consultation. Each extended value is stored in a section of that scope ([schema.md](./schema.md#extended-fields-optional-extended-key-catalog-excel-ext-1)). Pregnancy, delivery and newborn entities and IDs do not exist yet: the values stay with their source document (see [Pregnancy identity and multiple newborns](#pregnancy-identity-and-multiple-newborns-d6)).

**Source verification:** checked on the sample booklet `1-1.jpg` … `1-5.jpg` (not OCR'd) and on the specimen (`dossiers_specimen_10_patientes-NN.png`). The specimen is a different template ("Modèle de formulaire neutre") from the MVP layout `ma-fiche-surveillance-grossesse-v1` (D7); only the specimen is read. Each fictitious patient has 8 pages:

| Page of patient n | Section | Extended values read |
|-------------------|---------|----------------------|
| `8(n−1)+1` | Cover, *Grossesse classée à risque* | none (the risk checkboxes are D8 candidates) |
| `8(n−1)+2` | *Identification et antécédents* | patient, pregnancy, previous deliveries |
| `8(n−1)+3` | *Grossesse actuelle* (visit grid) | height, labs per visit |
| `8(n−1)+4` | *Déroulement de l'accouchement* | delivery, newborn 1 |
| `8(n−1)+5` / `+7` | Post-partum consultations, mother (*précoce* / *tardif*) | none |
| `8(n−1)+6` / `+8` | Post-partum consultations, newborn (*précoce* / *tardif*) | consultation date, feeding mode |

**Owners**

- **Extraction and review UI:** local OCR (`dayone/ocr_extended.py`) and the back-office. Fatma's extractor does not produce extended fields.
- **Ariyan:** storage, derived values and the export column (see [Changes for Ariyan's review](#changes-for-ariyans-review)).
- **Aymane:** review question and display in the back-office.

## Missing values and uncertainty (applies to every column)

- **Internal:** a missing value is `null` with a `field_status` (`NOT_PROVIDED`, `ILLEGIBLE`, `UNKNOWN`, `NOT_APPLICABLE`, `NEEDS_REVIEW`), as in the MVP. `raw_text` is kept (for example `RAS`, `Neg`, `Lycée`).
- **Not captured is not blank:** an extended field or section is present only when its page type was read. An absent field was not photographed; it is never `NOT_PROVIDED`. A value OCR could not find after its label is `NEEDS_REVIEW`, not blank.
- **Export:** a missing value would be an empty cell. The CSV cannot carry the reason for a blank, so the status is lost on export unless a companion column is added (decision D9).
- **Derived values:** a derived value is empty when any input is missing or unverified. Nothing is imputed.
- **Checkboxes:** stored as `MARKED` / `UNMARKED`. An empty checkbox is not assumed to mean `0` (decision D5).
- **Direct identifiers** (name, husband's name, CIN, phone, address) appear on the same pages as several of these fields (`1-2.jpg`, `-02`, `-04`). They are never extracted or exported, and list items are numbered by position, never by CIN.

## Coverage

Generated from `dayone/export_columns.py`:

| # | Exact header | Coverage | Scope | Paper inputs | Blocking decisions |
|---|--------------|----------|-------|--------------|--------------------|
| 1 | `id` | unavailable or awaiting definition | export row | none | D1, D2 |
| 2 | `age (years)` | implemented and evaluated | pregnancy | `maternal_age_years` | D1 |
| 3 | `education level (0=none/primary,1=secondary,2=higher)` | unavailable or awaiting definition | patient | `education_level_text` | crosswalk |
| 4 | `consanguinity` | implemented and evaluated | pregnancy | `consanguinity_mark` | D5 |
| 5 | `desired pregnancy` | implemented and evaluated | pregnancy | `pregnancy_desired_mark` | D5 |
| 6 | `hypertension history` | unavailable or awaiting definition | patient | none | D8, D5 |
| 7 | `diabetes mellitus` | unavailable or awaiting definition | patient | none | D8, D5 |
| 8 | `gravidity (number)` | implemented and evaluated | pregnancy | `gravidity` | D1 |
| 9 | `parity (number)` | implemented and evaluated | pregnancy | `parity` | D1 |
| 10 | `abortions (number)` | implemented but unverified | pregnancy | `abortions_count` | D1 |
| 11 | `living children (number)` | implemented and evaluated | pregnancy | `living_children_count` | D1 |
| 12 | `previous cesarean` | requires aggregation | pregnancy | `previous_delivery_mode_text` | D5, crosswalk |
| 13 | `bmi pregestational (kg/m2)` | unavailable or awaiting definition | pregnancy | `height_cm` | none |
| 14 | `mean systolic bp (mmhg)` | requires aggregation | pregnancy | `systolic_bp_mmhg` | D3 |
| 15 | `mean diastolic bp (mmhg)` | requires aggregation | pregnancy | `diastolic_bp_mmhg` | D3 |
| 16 | `hemoglobin (g/dl)` | requires aggregation | pregnancy | `hemoglobin_g_dl` | D10 |
| 17 | `first fasting glucose (mg/dl)` | unavailable or awaiting definition | pregnancy | `blood_glucose_g_l` | D10 |
| 18 | `proteinuria` | unavailable or awaiting definition | pregnancy | `albuminuria` | D10, D5 |
| 19 | `hiv test result` | requires aggregation | pregnancy | `hiv_test` | D10, D5 |
| 20 | `syphilis test result` | requires aggregation | pregnancy | `syphilis_test` | D10, D5 |
| 21 | `hepatitis c test result` | unavailable or awaiting definition | pregnancy | none | D8, D5 |
| 22 | `gestational age at enrollment (weeks)` | requires aggregation | pregnancy | `gestational_age_days`, `visit_date` | D4 |
| 23 | `gestational dm` | unavailable or awaiting definition | pregnancy | none | D8, D5 |
| 24 | `gestational age at birth (weeks)` | implemented and evaluated | delivery | `delivery_gestational_age_days` | D1, D4 |
| 25 | `preterm birth` | unavailable or awaiting definition | delivery | `delivery_gestational_age_days` | D5, definition |
| 26 | `type of delivery (0=vaginal,1=cesarean)` | implemented and evaluated | delivery | `delivery_mode` | D1 |
| 27 | `newborn sex (0=female,1=male)` | implemented and evaluated | newborn | `newborn_sex` | D1, D6 |
| 28 | `child birth weight (g)` | implemented and evaluated | newborn | `birth_weight_g` | D1, D6 |
| 29 | `head circumference (cm)` | implemented and evaluated | newborn | `birth_head_circumference_cm` | D1, D6 |
| 30 | `breastfeeding initiated` | unavailable or awaiting definition | newborn | `feeding_mode` | D5 |
| 31 | `referral to higher care` | unavailable or awaiting definition | unresolved | none | D8, D5 |

**Totals:** 11 implemented and evaluated, 1 implemented but unverified, 7 requiring aggregation, 12 unavailable or awaiting definition. "Paper inputs" of an unavailable column are extracted for review only; `crosswalk` is an agreed text-to-code table, `definition` a documented rule. Columns 14, 15, 19, 20 and 22 use the v1.0 visit fields.

## Evidence

Real OCR (PaddleOCR 3.7.0, PP-OCRv5 medium models, CPU) on the synthetic specimen: pages of types 2, 3, 4, 6 and 8 for all ten fictitious patients (50 pages), plus photo-like copies (blur, noise, tilt, JPEG) of pages `-02`, `-04` and `-06`. Ground truth is the PDF text layer and the checkbox drawings of `dossiers_specimen_10_patientes.pdf`. `KNOWN` needs OCR confidence ≥ 0.97 (and, for checkboxes, the box rule). Reproduce with `.venv\Scripts\python tools/ocr_specimen_eval.py --extended`; the report is `var/ocr-eval/report-extended-paddle-medium.md`.

**No value was wrongly accepted (`WRONG KNOWN` 0 on every field), and no written value was claimed blank.** "Review, value right" means the value went to a person with the correct reading proposed; "other review" means a person must type or pick it.

| Column | Field | Checks | Correct `KNOWN` | Review, value right | Other review | Verified blank |
|--------|-------|-------:|----------------:|--------------------:|-------------:|---------------:|
| 2 | `maternal_age_years` | 11 | 9 | 2 | 0 | 0 |
| 4 | `consanguinity_mark` | 11 | 10 | 0 | 1 | 0 |
| 5 | `pregnancy_desired_mark` | 11 | 11 | 0 | 0 | 0 |
| 8 | `gravidity` (always reviewed) | 11 | 0 | 11 | 0 | 0 |
| 9 | `parity` (always reviewed) | 11 | 0 | 11 | 0 | 0 |
| 10 | `abortions_count` (always reviewed) | 11 | 0 | 2 | 0 | 9 |
| 11 | `living_children_count` (always reviewed) | 11 | 0 | 9 | 2 | 0 |
| 24 | `delivery_gestational_age_days` | 11 | 4 | 7 | 0 | 0 |
| 26 | `delivery_mode` | 11 | 10 | 0 | 1 | 0 |
| 27 | `newborn_sex` | 11 | 1 | 10 | 0 | 0 |
| 28 | `birth_weight_g` | 11 | 1 | 10 | 0 | 0 |
| 29 | `birth_head_circumference_cm` | 11 | 8 | 3 | 0 | 0 |

Inputs of columns that need aggregation or a definition, extracted for review only:

| Column | Field | Checks | Correct `KNOWN` | Review, value right | Other review | Verified blank |
|--------|-------|-------:|----------------:|--------------------:|-------------:|---------------:|
| 3 | `education_level_text` (always reviewed) | 11 | 0 | 10 | 1 | 0 |
| 12 | `previous_delivery_date` | 18 | 16 | 2 | 0 | 0 |
| 12 | `previous_delivery_mode_text` (always reviewed) | 18 | 0 | 16 | 2 | 0 |
| 13 | `height_cm` | 10 | 1 | 9 | 0 | 0 |
| 16 | `hemoglobin_g_dl` | 55 | 12 | 4 | 1 | 38 |
| 17 | `blood_glucose_g_l` | 55 | 7 | 2 | 1 | 45 |
| 18 | `albuminuria` | 55 | 53 | 0 | 2 | 0 |
| 30 | `consultation_date` | 21 | 14 | 5 | 2 | 0 |
| 30 | `feeding_mode` | 21 | 21 | 0 | 0 | 0 |
| – | `delivery_date` | 11 | 4 | 6 | 1 | 0 |

- **Other review, explained.** `living_children_count`: on `-02`, `1` was read as `7` at confidence 0.977, above the threshold; it reached a person only because counts are always confirmed. On `-58`, nothing was read after the label (`OCR_NO_TEXT`). `albuminuria`: `Pos +` is not an agreed value (the ground truth cannot be parsed either). `consultation_date`: two fictitious dates after today, rejected as unparsable. `hemoglobin_g_dl` / `blood_glucose_g_l` on `-11`: stray characters (`13.2 9g/dL`, `L0.8 9/L`). `delivery_date` on `-36`: `08/0s/2026`. `previous_delivery_mode_text`: `Joie basse` (`-34`), `Voie basce` (photo-like `-02`). Photo-like `-02`: `Lycre`, consanguinity box faint (`CHECKBOX_UNCLEAR`). Photo-like `-04`: delivery mode box unclear.
- **Checkbox ink.** Marked boxes: n = 45, interior ink share 0.224–0.816 (median 0.571). Empty boxes: n = 84, 0–0.125 (median 0); the 0.125 is an empty box on the photo-like copy of `-04`. The full run used `MARKED` ≥ 0.12, which accepted that empty box as marked and sent delivery mode to review as `CHECKBOX_MULTIPLE_MARKED`. The threshold was then raised to 0.18 (`dayone/ocr_extended.py`). A recheck of `-02`, `-04`, `-06` and their photo-like copies (`report-extended-paddle-medium-recheck.md`) gives the same results except that `-04` photo-like delivery mode now goes to review as `CHECKBOX_UNCLEAR` with the right value; still 0 `WRONG KNOWN`. Clean pages are unaffected (lowest marked box 0.224).
- **Fix found by the evaluation.** On page `-58` the OCR box of the label *Nombre d'enfants vivants* covered the handwritten `1`, and the value was claimed blank. Values after a label are now never claimed blank: nothing read goes to review (`OCR_NO_TEXT`). Verified blanks remain only for table cells (`abortions_count`, labs), where the empty cell is checked on the image.
- **Speed.** 4.5–18.7 s per page on CPU.
- **Limits.** Synthetic renders with a handwriting-style font, not real handwriting or phone photos of real booklets; the photo-like copies are simulated. The ink thresholds were calibrated on these same pages. Column 10 has only two written values. The sample booklet `1-x.jpg` was not OCR'd, and the MVP layout `ma-fiche-surveillance-grossesse-v1` has no extended reading (D7).

## Column details

### 1. `id`

- **Coverage:** unavailable or awaiting definition.
- **Field:** none. A generated export row ID (decision D2), produced at export time.
- **Scope:** one export row (decision D1).
- **Source:** none; not on any form.
- **Missing / uncertainty:** never empty once defined. It must not be derived from CIN, `PAT-`/`DOC-` IDs, `registry_file_number`, `midwife_patient_code` or any personal data.
- **Owner:** Ariyan. No extraction or review.
- **Data note:** CSV values are 1–200, unique and sequential.

### 2. `age (years)`

- **Coverage:** implemented and evaluated.
- **Field:** `pregnancy.maternal_age_years`, integer, years (10–60). Stored as written; never computed from a birth date.
- **Scope:** pregnancy (age when the booklet was filled in).
- **Source:** verified. *Identification*, *Age :*, on `1-2.jpg` and specimen page 2 of each patient.
- **Missing / uncertainty:** `null` + status. The form does not say when the age was recorded (booking or delivery); kept as written.
- **Data note:** CSV 15–43, integers, no blanks.

### 3. `education level (0=none/primary,1=secondary,2=higher)`

- **Coverage:** unavailable or awaiting definition (no crosswalk).
- **Field:** `patient.education_level_text`, text as written (specimen: `Lycée`, `Université`, …). Always `NEEDS_REVIEW` (`OCR_CONFIRM_REQUIRED`).
- **Scope:** patient.
- **Source:** verified. *Niveau d'instruction* on `1-2.jpg` and specimen page 2. The form takes free text, not a 3-level code.
- **Missing / uncertainty:** no level is chosen until a text-to-level crosswalk is agreed; the CSV code is never guessed from the text.
- **Data note:** 18 blanks in the CSV.

### 4. `consanguinity`

- **Coverage:** implemented and evaluated (box state). Export encoding blocked by D5.
- **Field:** `pregnancy.consanguinity_mark`, choice `MARKED` / `UNMARKED` (the box as printed, not yes/no). CSV `0`/`1` meaning is **undocumented**.
- **Scope:** pregnancy (recorded once per booklet; relates to the current partner).
- **Source:** verified. Checkbox *Consanguinité* on `1-2.jpg` and specimen page 2.
- **Missing / uncertainty:** an empty box is `UNMARKED`, not "no", until D5 is decided. A faint mark or a box not found goes to review.
- **Data note:** no blanks in the CSV.

### 5. `desired pregnancy`

- **Coverage:** implemented and evaluated (box state). Export encoding blocked by D5.
- **Field:** `pregnancy.pregnancy_desired_mark`, choice `MARKED` / `UNMARKED`. CSV `0`/`1` meaning is **undocumented**.
- **Scope:** pregnancy.
- **Source:** verified. Checkbox *Grossesse désirée* on `1-2.jpg` and specimen page 2.
- **Missing / uncertainty:** same checkbox rule as column 4.
- **Data note:** no blanks in the CSV.

### 6. `hypertension history`

- **Coverage:** unavailable or awaiting definition (D8).
- **Field:** none. Nothing is extracted.
- **Scope:** patient.
- **Source:** **not verified.** Candidates, none confirmed as the CSV meaning:
  - row *HTA* under *Antécédents héréditaires et familiaux*. This is **family** history (*Famille de la femme*, *Mari/famille du mari*) and is never used for the woman's own history;
  - free text *Antécédents de la femme → Médicaux*;
  - checkbox *H.T.A* under *Grossesse classée à risque* on the cover.
- **Missing / uncertainty:** `RAS` in a free-text cell would be kept as `raw_text`; its conversion is part of D5.

### 7. `diabetes mellitus`

- **Coverage:** unavailable or awaiting definition (D8).
- **Field:** none. Nothing is extracted.
- **Scope:** patient.
- **Source:** **not verified.** Same candidates as column 6 (family-history row *Diabète*, never used; *Antécédents de la femme → Médicaux*; cover checkbox *Diabète*). Whether the column means pre-existing diabetes only, as distinct from column 23, is not documented.

### 8. `gravidity (number)`

- **Coverage:** implemented and evaluated.
- **Field:** `pregnancy.gravidity`, integer, count (0–20). Always `NEEDS_REVIEW` (`OCR_CONFIRM_REQUIRED`): a single handwritten digit has nothing to check it against.
- **Scope:** pregnancy (snapshot at booking). Whether it includes the current pregnancy is not stated on the form or in the header.
- **Source:** verified. *Gestation* on `1-3.jpg` and specimen page 2.
- **Data note:** CSV 1–7, no blanks; parity ≤ gravidity on every row.

### 9. `parity (number)`

- **Coverage:** implemented and evaluated.
- **Field:** `pregnancy.parity`, integer, count. Always confirmed by a person.
- **Scope:** pregnancy.
- **Source:** verified. *Parité* on `1-3.jpg` and specimen page 2.
- **Missing / uncertainty:** nothing read after the label goes to review, never 0.
- **Data note:** CSV 0–7, no blanks.

### 10. `abortions (number)`

- **Coverage:** implemented but unverified: only 2 of the 10 specimen booklets have a written value.
- **Field:** `pregnancy.abortions_count`, integer, count. Always confirmed by a person.
- **Scope:** pregnancy.
- **Source:** verified. *Antécédents obstétricaux*, row *Avortement*, column *Nombre*, on `1-3.jpg` and specimen page 2.
- **Missing / uncertainty:** an empty *Nombre* cell is `NOT_PROVIDED` (verified blank on the photo), never 0.
- **Data note:** 11 blanks in the CSV.

### 11. `living children (number)`

- **Coverage:** implemented and evaluated.
- **Field:** `pregnancy.living_children_count`, integer, count. Always confirmed by a person (page `-02`: `1` read as `7` at confidence 0.977).
- **Scope:** pregnancy.
- **Source:** verified. *Nombre d'enfants vivants* on `1-3.jpg` and specimen page 2.
- **Data note:** CSV 0–7, no blanks.

### 12. `previous cesarean`

- **Coverage:** requires aggregation (not implemented); the inputs are extracted.
- **Fields:** `previous_deliveries[column 1–5]`: `previous_delivery_date` (date) and `previous_delivery_mode_text` (text as written, always reviewed). One item per *Accouch.* column with writing.
- **Scope:** previous delivery; the CSV value is pregnancy level.
- **Source:** verified. *Déroulement des accouchements antérieurs*, rows *Date* and *Modalité d'extraction*, on `1-3.jpg` and specimen page 2. There is no single checkbox.
- **Missing / uncertainty:** the yes/no across previous deliveries needs a mode-text crosswalk and D5; it is not computed. *Si césarienne : indication* is not read.
- **Data note:** no row has `previous cesarean` set with parity 0.

### 13. `bmi pregestational (kg/m2)`

- **Coverage:** unavailable (no pre-pregnancy weight or BMI on the forms).
- **Field:** none for BMI. `pregnancy.height_cm` (integer, cm, the woman's height) is extracted for review only.
- **Scope:** pregnancy.
- **Source:** *Taille* is on the *Grossesse actuelle* header (`1-4.jpg`, specimen page 3). No pre-pregnancy weight was found; visit *Poids* is weight during pregnancy and is never used.
- **Missing / uncertainty:** stays empty; BMI is never computed or fabricated.
- **Data note:** 16 blanks; 15.0 is both the minimum and appears 3 times.

### 14. `mean systolic bp (mmhg)`

- **Coverage:** requires aggregation. Built from the v1.0 field `systolic_bp_mmhg`.
- **Field:** export only, decimal (CSV uses one decimal), mmHg. Not computed.
- **Scope:** pregnancy, from visits.
- **Source:** verified. Row *TA* of the visit grid (`1-4.jpg`, `1-5.jpg`, specimen page 3). Post-partum consultations also have *TA*, so the visit set matters (D3).
- **Missing / uncertainty:** visit set, verified-only rule, minimum number of visits and rounding are D3.
- **Data note:** CSV 72.6–130.7, no blanks.

### 15. `mean diastolic bp (mmhg)`

- **Coverage:** requires aggregation. Built from the v1.0 field `diastolic_bp_mmhg`.
- **Field:** export only, decimal, mmHg. Not computed.
- **Scope:** pregnancy, from visits.
- **Source:** verified. Row *TA*, as column 14.
- **Missing / uncertainty:** it must use the same visits as column 14 (D3).
- **Data note:** CSV 42.3–83.4, no blanks.

### 16. `hemoglobin (g/dl)`

- **Coverage:** requires aggregation; the per-visit input is extracted.
- **Field:** `visit_labs[slot].hemoglobin_g_dl`, decimal, g/dL (3–25).
- **Scope:** visit; the CSV value is pregnancy level.
- **Source:** verified. *Examen biologique*, row *Hémoglobine*, on `1-4.jpg` and specimen page 3.
- **Missing / uncertainty:** which visit gives the single CSV value is D10; nothing is chosen. Lab cells never make a grid column a visit; writing in a non-visit column is `LAB_WITHOUT_VISIT` (review). Lab values are not copied into the registered visits.
- **Data note:** 23 blanks.

### 17. `first fasting glucose (mg/dl)`

- **Coverage:** unavailable or awaiting definition: the form never says the test was fasting.
- **Field:** `visit_labs[slot].blood_glucose_g_l`, decimal, g/L as written (2 decimals), labelled *Bilan glycémique (à jeun non précisé)*. It is never stored, shown or exported as a fasting glucose. No mg/dL conversion is done.
- **Scope:** visit.
- **Source:** *Bilan glycémique* on `1-4.jpg` and specimen page 3 (written as `0.93 g/L`). *Glucosurie* (urine) is a different test and is not read.
- **Missing / uncertainty:** exporting *Bilan glycémique* under a "fasting" header needs agreement (D10).
- **Data note:** 35 blanks.

### 18. `proteinuria`

- **Coverage:** unavailable or awaiting definition: *Albuminurie* is not *Protéinurie*.
- **Field:** `visit_labs[slot].albuminuria`, enum `NEGATIVE` / `POSITIVE`, for review only. Entries such as `Pos +` are not parsed and go to review.
- **Scope:** visit.
- **Source:** the grid has *Albuminurie* (`1-4.jpg`, specimen page 3), not *Protéinurie*. Equivalence, the cross-visit rule and the encoding are D10 and D5.
- **Data note:** no blanks.

### 19. `hiv test result`

- **Coverage:** requires aggregation. The v1.0 field `hiv_test` (`NEGATIVE` / `POSITIVE`) is per visit.
- **Field:** existing `hiv_test`. Whether `1` means positive is **undocumented** (D5).
- **Scope:** visit; export needs a rule across visits (D10).
- **Source:** verified. *Sérologie VIH* on `1-4.jpg`, `1-5.jpg` and specimen page 3.
- **Data note:** 10 blanks.

### 20. `syphilis test result`

- **Coverage:** requires aggregation. The v1.0 field `syphilis_test` is per visit.
- **Field:** existing `syphilis_test`. Encoding **undocumented** (D5).
- **Scope:** visit; export rule as column 19 (D10).
- **Source:** verified. *Syphilis (TPHA/VDRL)* on `1-4.jpg`, `1-5.jpg` and specimen page 3.
- **Data note:** 7 blanks.

### 21. `hepatitis c test result`

- **Coverage:** unavailable or awaiting definition.
- **Field:** none. Nothing is extracted.
- **Scope:** visit.
- **Source:** **not verified.** Neither template has a hepatitis C row. *Ag HBs* is hepatitis B and is not used. On `1-4.jpg`, an HCV result is handwritten in the free-text *Autres* cell; the specimen has nothing.
- **Data note:** 5 blanks.

### 22. `gestational age at enrollment (weeks)`

- **Coverage:** requires aggregation. Built from the v1.0 field `gestational_age_days`.
- **Field:** export only, decimal weeks. Not computed.
- **Scope:** pregnancy, from the enrollment visit.
- **Source:** verified. *Âge probable (de la grossesse)* on `1-4.jpg` and specimen page 3.
- **Missing / uncertainty:** which visit counts as enrollment, whether to fall back on *DDR*, and rounding are D4.
- **Data note:** CSV 6.0–24.9, no blanks; 6.0 is the minimum and appears 7 times. Enrollment GA ≤ birth GA on every row.

### 23. `gestational dm`

- **Coverage:** unavailable or awaiting definition (D8).
- **Field:** none. Never derived from glucose values (that would be a clinical judgement).
- **Scope:** pregnancy.
- **Source:** **not verified.** No field says *diabète gestationnel*. The cover checkbox *Grossesse classée à risque → Diabète* does not distinguish gestational from pre-existing diabetes.

### 24. `gestational age at birth (weeks)`

- **Coverage:** implemented and evaluated (in days). The days-to-weeks rounding for the CSV is D4.
- **Field:** `delivery.delivery_gestational_age_days`, integer, days (140–315). Weeks + days as written; never recomputed from the *DDR*.
- **Scope:** delivery.
- **Source:** verified on the specimen only. *Déroulement de l'accouchement → État du nouveau-né → Âge gestationnel*, page 4. The sample booklet has no delivery page in the repo.
- **Missing / uncertainty:** the form value may be whole weeks while the CSV has decimals (22 of 200 rows are whole numbers).
- **Data note:** CSV 34.6–42.0, no blanks.

### 25. `preterm birth`

- **Coverage:** unavailable or awaiting definition.
- **Field:** none. Not derived from column 24: the definition is not documented, and the dataset's agreement with "GA at birth < 37" is an observation only.
- **Scope:** delivery.
- **Source:** not verified as a delivery field. The newborn post-partum pages have a checkbox *Nouveau-né prématuré*, not read.
- **Data note:** in all 200 rows, `1` coincides with GA at birth < 37.

### 26. `type of delivery (0=vaginal,1=cesarean)`

- **Coverage:** implemented and evaluated.
- **Field:** `delivery.delivery_mode`, choice from the form boxes: `VAGINAL_NON_INSTRUMENTAL`, `VAGINAL_INSTRUMENTAL`, `CESAREAN_PLANNED`, `CESAREAN_EMERGENCY`. The header documents `0` = vaginal and `1` = cesarean; the export mapping is not implemented.
- **Scope:** delivery.
- **Source:** verified on the specimen only. *Mode de l'accouchement* on page 4.
- **Missing / uncertainty:** `KNOWN` only with exactly one marked box and the other three clearly empty; several marked boxes (`CHECKBOX_MULTIPLE_MARKED`) or a faint box go to review; none marked is `NOT_PROVIDED` (`CHECKBOX_NONE_MARKED`).
- **Data note:** no blanks.

### 27. `newborn sex (0=female,1=male)`

- **Coverage:** implemented and evaluated.
- **Field:** `newborns[index 1].newborn_sex`, choice `FEMALE` / `MALE` (written `F`, `M`, *féminin*, *masculin*, *fille*, *garçon*). Encoding documented in the header; export not implemented.
- **Scope:** newborn. Several newborns are D6.
- **Source:** verified on the specimen only. *État du nouveau-né → Sexe* on page 4.
- **Missing / uncertainty:** any other written value goes to review; no third code is invented.
- **Data note:** no blanks.

### 28. `child birth weight (g)`

- **Coverage:** implemented and evaluated.
- **Field:** `newborns[index 1].birth_weight_g`, integer, grams (300–7000).
- **Scope:** newborn.
- **Source:** verified on the specimen only. *Poids à la naissance* on page 4. Weights at the post-partum consultations are never used as birth weight.
- **Data note:** CSV 2234–4800, no blanks. 4800 is the maximum and appears 37 times; possible cap, not used as a validation limit.

### 29. `head circumference (cm)`

- **Coverage:** implemented and evaluated.
- **Field:** `newborns[index 1].birth_head_circumference_cm`, decimal, cm (20–50).
- **Scope:** newborn.
- **Source:** verified on the specimen only. *Périmètre crânien à la naissance* on page 4. *Périmètre crânien* at later consultations is not used.
- **Data note:** 23 blanks; 82 values are non-integer. 40 is the maximum and appears 91 times; possible cap, not confirmed.

### 30. `breastfeeding initiated`

- **Coverage:** unavailable or awaiting definition: no field records initiation.
- **Field:** `newborn_consultations[period EARLY/LATE].feeding_mode`, choice `EXCLUSIVE_BREASTFEEDING` / `ARTIFICIAL` / `MIXED`, and `consultation_date`, for review only. Feeding on the day of a later consultation is not proof that breastfeeding was initiated at birth, so it is never exported under this header.
- **Scope:** newborn consultation.
- **Source:** *Allaitement : exclusivement au sein / artificiel / mixte* on the newborn post-partum pages (early: page 6, late: page 8). The delivery page has no breastfeeding field.
- **Missing / uncertainty:** whether any consultation value can stand for "initiated" must be agreed (D5). A consultation page without a readable period title is not kept.
- **Data note:** no blanks.

### 31. `referral to higher care`

- **Coverage:** unavailable or awaiting definition.
- **Field:** none. Nothing is extracted.
- **Scope:** unresolved: mother or newborn, and which period.
- **Source:** **not verified.** Candidates: *Transfert* + *établissement de référence* on the newborn post-partum pages; *Référée* on the maternal post-partum pages, but it sits under *Planification familiale*, so it is probably not the same concept. Nothing seen on the antenatal or delivery pages.
- **Data note:** no blanks.

## Pregnancy identity and multiple newborns (D6)

Decisions still required before any pregnancy, delivery or newborn record or export row exists:

- **Pregnancy identity.** Extended values are kept per source document (`documents.draft_json`), linked to the woman only through the document's registration (`PAT-` ID). Nothing tells two pregnancies of the same woman apart, and no pregnancy ID is generated. Candidates: a new booklet (new *N° de la fiche*), or the delivery date closing a pregnancy. Until this is decided, values from two documents of the same woman are not combined, and a document mixing pages of two pregnancies cannot be detected.
- **Several newborns.** The specimen delivery page has one newborn block, stored as `newborns[index 1]`. Twins have no layout on the specimen; their values could not be told apart and would conflict. One CSV row per newborn, first-born only, or flag and exclude is undecided.
- **Consultations and newborns.** Newborn consultations are keyed by period (`EARLY`, `LATE`) and are not linked to a newborn index.
- **IDs.** Any future pregnancy, delivery or newborn ID is generated (`PRG-`, `NBN-` style), never derived from CIN, names, file numbers or link keys.

## Changes for Ariyan's review

Storage and API changes were kept to what review and retention need. All are backward compatible; the v1 fixture workflow is unchanged (fixtures have no `extended` key and stay valid).

| Change | Where | Why |
|--------|-------|-----|
| Optional draft key `extended` (catalog `excel-ext-1`), validated when present | `dayone/schema.py` (`OPTIONAL_DRAFT_KEYS`), `dayone/extended.py` | Holds the extended values with status, flags and source |
| `FieldSpec` gains `choices` (kind `choice`) and `decimals` | `dayone/schema.py` | Checkbox and form-option values; glucose keeps 2 decimals |
| `review_field(..., section=None, item_index=None)`; body keys `section`, `item_index` on `POST /api/documents/{id}/fields` with `scope: "extended"`; `FIELD_REVIEWED` events carry them | `dayone/service.py`, `dayone/server.py` | Review one extended field |
| `catalog.extended` in `GET /api/system` | `dayone/schema.py` | The UI needs labels, units, choices and sources |
| Extended values are **not blocking** and the final confirmation does **not** confirm them; they stay `UNVERIFIED` unless reviewed one by one | `dayone/schema.py`, `dayone/service.py` | Registration of visits is unchanged; nobody approves values they did not look at. Policy to confirm with Aymane |
| Retention: the draft (with `extended`) stays in `documents.draft_json` after registration; no new table, no copy into `visits.fields_json` | none (existing behaviour) | No pregnancy/delivery/newborn entity before D1 and D6 |
| Live drafts may not claim an extended field was verified | `dayone/extraction.py` (`check_live_draft`) | Same rule as v1.0 fields |
| Re-extraction discards extended reviews and counts them | `dayone/service.py` | Same rule as v1.0 fields |
| Parser version `grid-4` in `extractor_version` | `dayone/live_ocr.py` | Lab rows and extended reading |
| Number parsing strips only a trailing unit (`3200 gr` was read as `3200 r`) | `dayone/schema.py` (`_number`) | Bug fix; applies to v1.0 fields too |

Not done, for Ariyan: an export, pregnancy/delivery/newborn tables, any derived value.

## Unresolved decisions

None of these are decided. Each needs agreement from Fatma, Ariyan and Aymane, and organizer input where noted.

| # | Decision | What we know | Open question |
|---|----------|--------------|---------------|
| D1 | **What one export row represents** | Each CSV row has one delivery and one newborn, which suggests one pregnancy per row. This is not documented. Extended values are stored per document. | One row per pregnancy, per patient, or per newborn? Ask the organizers. |
| D2 | **Generated export ID** | CSV `id` is 1–200, sequential. | Sequential per export, or stable across exports (needs a private mapping table)? It must never expose CIN, `PAT-` IDs, `registry_file_number`, `midwife_patient_code` or any personal data. |
| D3 | **Visits included in mean BP** | *TA* appears on antenatal visit columns and on maternal post-partum consultations. | Antenatal visits only? All verified visits, or only `CONFIRMED`/`CORRECTED`? Minimum number of visits? Rounding (CSV has one decimal)? Systolic and diastolic must use the same visits. |
| D4 | **Gestational-age rules** | Enrollment: `gestational_age_days` per visit (*Âge probable*). Delivery: `delivery_gestational_age_days` from *Âge gestationnel*. CSV uses decimal weeks. | Which visit is "enrollment": the first dated *Venue le* or the first visit with a GA? Use the written GA, or compute from *DDR*? Days-to-weeks rounding. |
| D5 | **Boolean and test-result encodings** | Only three headers document their codes. The `0`/`1` meaning is undocumented for 13 columns: `consanguinity`, `desired pregnancy`, `hypertension history`, `diabetes mellitus`, `previous cesarean`, `proteinuria`, `hiv test result`, `syphilis test result`, `hepatitis c test result`, `gestational dm`, `preterm birth`, `breastfeeding initiated`, `referral to higher care`. | Confirm with the organizers whether `1` = yes/positive. Does an empty checkbox mean `0` or missing? How do free-text entries (`RAS`, `Neg`, `Néant`) convert? |
| D6 | **Multiple pregnancies and multiple newborns** | See [above](#pregnancy-identity-and-multiple-newborns-d6). | How are separate pregnancies of the same patient told apart? For twins: one row per newborn, first-born only, or flag and exclude? |
| D7 | Specimen template as a second layout | Extended reading works on the specimen pages only; drafts still declare `ma-fiche-surveillance-grossesse-v1`. | Register a second layout ID, or extend v1? |
| D8 | Source for history and risk fields | Columns 6, 7, 21, 23 and 31 have only candidate sources (above). | Which section feeds each column? |
| D9 | Missing-value reasons in the export | The CSV has blanks only; internal statuses distinguish `NOT_PROVIDED` / `ILLEGIBLE` / `UNKNOWN`. | Add a companion status column, or export blanks only? Export only reviewer-verified values? |
| D10 | Cross-visit rules for labs | Hemoglobin, glucose, albuminuria, HIV and syphilis are per visit; the CSV has one value. | First, last, or any-positive per column? Is *Bilan glycémique* acceptable as "first fasting glucose"? Is *Albuminurie* acceptable as "proteinuria"? |

## Data observations (no conclusions drawn)

| Observation | Columns |
|-------------|---------|
| Blank cells | education 18, abortions 11, BMI 16, hemoglobin 23, glucose 35, HIV 10, syphilis 7, hepatitis C 5, head circumference 23. All other columns have none. |
| Values piled at the range limit | birth weight 4800 (max) × 37; head circumference 40 (max) × 91; GA at enrollment 6.0 (min) × 7; BMI 15.0 (min) × 3 |
| Consistency checks, all passing | `preterm birth` = 1 exactly when GA at birth < 37; GA at enrollment ≤ GA at birth; parity ≤ gravidity; no previous cesarean with parity 0 |
| Linkage | The CSV rows are not linked to the specimen patients or to the sample booklet. No row is known to correspond to any image. |

The clusters may come from clipping in the synthetic generator. Until the organizers confirm, they are not used as validation limits.
