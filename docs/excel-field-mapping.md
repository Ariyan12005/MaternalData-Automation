# Excel field mapping: contract proposal (DRAFT, not agreed)

Status: **proposal.** Nothing here is implemented. Agree the open decisions (section 6) before extending `docs/schema.md` or `dayone/schema.py`.
Baseline: schema v1.0 on `main` (12 fields, layout `ma-fiche-surveillance-grossesse-v1`).
Scope of this review: fiche pages `1-1` to `1-5` only. The ~125 `dossiers_specimen_*` images were **not** reviewed.

## 0. Corrections to earlier assumptions (read first)

1. **CIN must not be a key or be stored.** `consignes-fr-en.pdf` says national ID, name, husband's name, phone and address must be ignored or masked, never stored. `docs/schema.md` already lists them as "never extracted". CIN is visibly handwritten on page `1-2`, so it must be recorded only as `pii_detected` with `action: NOT_EXTRACTED`. Link key = `registry_file_number` + `midwife_patient_code` (already in the contract). The "CIN as primary key" idea is dropped.
2. **The problems found in `maternal_registry_synthetic.xlsx` are in the organizers' reference file, not in our API.** It is byte-identical to `data/maternal_registry_synthetic.xlsx` (same SHA-256), which the README marks read-only. Do not edit it. The anomalies (see section 5) become validation warnings and test expectations, not data fixes.
3. **`midwife_patient_code` and multi-visit-per-page already exist in v1.0** (`document_fields.midwife_patient_code`, `encounters[]` with `slot`). The work is extending around them, not adding them.
4. **Today only a `FixtureExtractor` exists.** "Extractable" below means "present on the paper and expressible in the contract", not "an extractor does it today".

## 1. Mapping of the 31 Excel columns

Legend: **A** = in v1.0 field set, **B** = on a fiche page we have seen (`1-2`, `1-3`, or extra rows of `1-4`), needs new fields/pages, **C** = needs pages not reviewed (delivery, postpartum, newborn), **D** = unavailable or ambiguous, **G** = generated.

| # | Excel column | Cls | Paper source / rule | Notes and ambiguity |
|---|---|---|---|---|
| 1 | id | G | Backend (`patient_id` `PAT-nnnnnn`) | Row order only. Never derived from keys or CIN |
| 2 | age (years) | B | `1-2` Identification, *Age* (e.g. "21 ans") | Parse to integer; keep `raw_text` |
| 3 | education level | B | `1-2` *Niveau d'instruction* | Free text, often blank. Mapping to 0/1/2 needs a rule (decision 3). Blank = `NOT_PROVIDED`, never 0 |
| 4 | consanguinity | B | `1-2` checkbox | Unchecked box is ambiguous: "no" vs "not filled" (decision 4) |
| 5 | desired pregnancy | B | `1-2` checkbox *Grossesse désirée* | Same checkbox ambiguity |
| 6 | hypertension history | B | `1-2` *Antécédents de la femme: Médicaux* | Family HTA row is a different thing: do not mix. "RAS" is free text, not an explicit 0 (decision 5) |
| 7 | diabetes mellitus | B | Same as 6 | Same rule |
| 8 | gravidity | B | `1-3` *Gestation* | Handwriting in the sample is hard to read: expect `NEEDS_REVIEW` |
| 9 | parity | B | `1-3` *Parité* | Blank in sample: `NOT_PROVIDED` |
| 10 | abortions | B | `1-3` Obstétricaux, *Avortement / Nombre* | Blank cell vs explicit 0 is ambiguous |
| 11 | living children | B | `1-3` *Nombre d'enfants vivants* | Sample value ambiguous ("00"/"0"?) |
| 12 | previous cesarean | B | `1-3` prior deliveries table, *Modalités d'extraction* / *Si césarienne* | Derived from any prior delivery marked cesarean. Empty table is ambiguous |
| 13 | bmi pregestational | D | Paper has *Taille* (`1-4` header) and *Poids* during pregnancy only | Pre-pregnancy weight is not recorded. Do not compute from visit weight silently |
| 14 | mean systolic bp | A | *TA* per visit (`12/7` to 120/70) | Mean is an **export-time aggregate** over visits; store per-visit values |
| 15 | mean diastolic bp | A | Same | Same. Record number of visits used |
| 16 | hemoglobin (g/dl) | B | `1-4` *Hémoglobine*, per visit | Which visit goes in the single Excel cell (decision 6). Units written inconsistently |
| 17 | first fasting glucose (mg/dl) | B | `1-4` *Bilan glycémique* (sample "0,76 g/L") | g/L x 100 = mg/dL, keep original unit. Paper does not say "fasting" (decision 7). *Glucosurie* is urine, not this |
| 18 | proteinuria | B | `1-4` *Albuminurie* | Values like `+`, `traces`, `neg`. Threshold for 0/1 is a decision (decision 8) |
| 19 | hiv test result | A | *Sérologie VIH* (`hiv_test`) | Result may also be written in *Autres* ("VIH: nég") |
| 20 | syphilis test result | A | *Syphilis (TPHA/VDRL)* (`syphilis_test`) | One cell can hold two results ("nég, nég"); rapid test may be in *Autres* |
| 21 | hepatitis c test result | D | No HCV row. *Ag HBs* is hepatitis **B** | Never map Ag HBs to HCV. Only if written in *Autres* |
| 22 | gestational age at enrollment (weeks) | A | First registered visit, *Âge probable de la grossesse* (`gestational_age_days` / 7) | "Enrollment" = earliest registered visit; wrong if the first-visit column was not photographed |
| 23 | gestational dm | D | Not recorded as such | A diagnosis; clinical inference is out of scope. Cover risk box *Diabète* is a different field |
| 24 | gestational age at birth (weeks) | C | Delivery / postpartum section | Not in v1 layout |
| 25 | preterm birth | C | **Derived** from 24 (< 37 weeks) | Never extracted independently |
| 26 | type of delivery | C | Delivery section | Not in v1 layout |
| 27 | newborn sex | C | Newborn section | Not in v1 layout |
| 28 | child birth weight (g) | C | Newborn section | Not in v1 layout |
| 29 | head circumference (cm) | C | Newborn section | May not exist on paper |
| 30 | breastfeeding initiated | C | Postpartum / newborn section | Not in v1 layout |
| 31 | referral to higher care | C | Postpartum / newborn or risk section | Source page unconfirmed |

Counts: G 1 · A 5 · B 14 · C 8 · D 3 = 31.

Rows 24 to 31 come from the organizers' variable list ("Postpartum et nouveau-né"), not from pages we inspected.

## 2. Missing / illegible handling (no guessing)

Keep the six v1.0 statuses: `KNOWN`, `NEEDS_REVIEW`, `ILLEGIBLE`, `NOT_PROVIDED`, `UNKNOWN`, `NOT_APPLICABLE`. Rules:

- A blank cell is `NOT_PROVIDED`. It is never 0, "no" or "negative".
- An explicit "RAS" / "néant" is recorded as `raw_text` with a status chosen by the agreed rule (decision 5), not silently turned into 0.
- Derived values (mean BP, GA in weeks, preterm, g/L to mg/dL) carry `derived: true`, `derived_from: [field paths]`, and inherit the **worst** status of their inputs.
- Excel export: write a blank cell for any non-`KNOWN`/non-`CONFIRMED` value, and emit a second sheet `export_status` with one row per (`patient_id`, column, status, flags). Never export a guess.

## 3. Proposed contract changes (schema v1.1)

Additive only; v1.0 drafts must still validate.

```json
{
  "schema_version": "1.1",
  "document_fields": {
    "registry_file_number": {"...": "unchanged"},
    "midwife_patient_code": {"...": "unchanged"},
    "facility_name": {"...": "unchanged"},
    "last_menstrual_period": {"...": "unchanged"}
  },
  "history": {
    "age_years": {"...": "field value object"},
    "education_level": {"...": "field value object"},
    "consanguinity": {"...": "field value object"},
    "desired_pregnancy": {"...": "field value object"},
    "hypertension_history": {"...": "field value object"},
    "diabetes_history": {"...": "field value object"},
    "gravidity": {"...": "field value object"},
    "parity": {"...": "field value object"},
    "abortions": {"...": "field value object"},
    "living_children": {"...": "field value object"},
    "previous_cesarean": {"...": "field value object"}
  },
  "encounters": [
    {
      "encounter_type": "ANTENATAL",
      "slot": "T1_V1",
      "source": {"page_ref": "data/Paper Registry/1-4.jpg", "column": "Visite 1"},
      "encounter_status": "RECORDED",
      "fields": {
        "visit_date": {"...": "existing"},
        "gestational_age_days": {"...": "existing"},
        "weight_kg": {"...": "existing"},
        "systolic_bp_mmhg": {"...": "existing"},
        "diastolic_bp_mmhg": {"...": "existing"},
        "fundal_height_cm": {"...": "existing"},
        "syphilis_test": {"...": "existing"},
        "hiv_test": {"...": "existing"},
        "hemoglobin_g_dl": {"...": "new"},
        "blood_glucose_mg_dl": {"...": "new, unit_original kept in raw_text"},
        "urine_albumin": {"...": "new, enum NEGATIVE/TRACE/POSITIVE"}
      }
    }
  ],
  "outcome": null
}
```

- **Multiple visits per page:** keep `encounters[]` as is: one entry per grid column, each with its own `source.page_ref` and `slot`. Page `1-4` yields up to 3 encounters, page `1-5` up to 6. Add `encounter_status` (`RECORDED` | `COLUMN_EMPTY` | `NOT_CAPTURED`) so an empty or unphotographed column is explicit instead of absent.
- **`outcome`** (delivery/newborn) stays `null` until the C-class pages are reviewed and a layout id exists for them.
- Add `midwife_patient_code` to the Excel export beside `patient_id`. It is a link key, not one of the 31 columns.
- New fields need `FieldSpec` entries in `dayone/schema.py` with units and ranges (reuse the existing validator style).

## 4. Tasks for Cursor (in order)

1. Add this file as `docs/excel-field-mapping.md`. Do not change code until section 6 is answered.
2. After agreement: update `docs/schema.md` and `dayone/schema.py` to v1.1 (`history`, new encounter fields, `encounter_status`, `derived` / `derived_from`). Keep v1.0 fixtures valid.
3. Add `tests/test_schema.py` cases: blank stays `NOT_PROVIDED`; derived field inherits worst status; v1.0 draft still validates.
4. Add an exporter (new `dayone/export.py`): 31 columns + `patient_id` + `midwife_patient_code`; mean BP only over verified visits; blank cell + `export_status` sheet for missing values.
5. Add reference-data checks (section 5) as warnings in a script under `tools/`. Read the reference file; never write to it.
6. Do **not** add any CIN field, parser or column anywhere.

## 5. Anomalies in the organizers' reference file (warnings, not fixes)

`data/maternal_registry_synthetic.xlsx`, 200 rows, 31 columns. Structure is clean (valid codes and ranges, systolic > diastolic, `preterm` = GA at birth < 37 in all rows). Anomalies:

- Gravidity != parity + abortions in 70 rows (11 only because `abortions` is blank).
- Birth weight capped at exactly 4,800 g in 37 rows; head circumference exactly 40.0 cm in 91 rows; mean birth weight 4,289 g is implausibly high.
- Mean BP about 97/60 and no systolic >= 140 despite 12 rows with hypertension history.
- 3 gestational DM cases with mean glucose (86 mg/dL) no higher than the rest.
- HIV positive 3 of 190, syphilis positive 9 of 193: far above national rates.
- Missing values: glucose 35, hemoglobin 23, head circumference 23, education 18, BMI 16, abortions 11, HIV 10, syphilis 7, hepatitis C 5.

Use these as `WARN` rules in the export checker. They also tell you the export must tolerate blanks in every one of those columns.

## 6. Decisions to agree before extending the contract

1. Confirm the CIN exclusion (section 0, item 1) and that `midwife_patient_code` stays the link key.
2. Confirm the Excel `id` stays the backend `patient_id` sequence.
3. Education: how do free-text answers map to 0 / 1 / 2?
4. Checkboxes (consanguinity, desired pregnancy): unchecked means 0, or `NOT_PROVIDED`?
5. "RAS" in history cells: record as explicit negative, or leave `NOT_PROVIDED` with `raw_text: "RAS"`?
6. Hemoglobin and glucose: first visit only, first non-missing value, or lowest/highest?
7. "First fasting glucose": accept the first blood glucose as-is, or require a fasting mention?
8. Proteinuria: which paper values count as 1 (`+` only, or `traces` too)?
9. Mean BP: mean over all registered visits, or only those before delivery?
10. Pregestational BMI: leave `NOT_PROVIDED`, or export an approximation clearly labeled as derived from first-visit weight?
11. Which pages cover the C-class columns (24 to 31)? Review the specimen images before defining `outcome`.
