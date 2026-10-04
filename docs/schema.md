# Shared schema (v1.0)

Contract between extraction (Fatma), backend (Ariyan) and review UI (Aymane).
Machine-readable definitions live in `dayone/schema.py`; fixtures in `fixtures/` must validate against it (`python -m unittest`).

## Supported layout

`layout_id`: **`ma-fiche-surveillance-grossesse-v1`**: *Fiche de surveillance de la grossesse et du post-partum* (pink booklet), as printed in the specimen set `data/Paper Registry/dossiers_specimen_10_patientes-*.png` (10 fictitious patients, 8 pages each: patient 1 is pages 01–08).

| Page in the booklet | Printed title | Used for |
|------|---------|----------|
| 1 | *Fiche de surveillance de la grossesse et du post-partum* | `registry_file_number` (*N° de la fiche*), `facility_name` |
| 2 | *Identification et antécédents* | Nothing (name, CIN, phone, address are detected as PII, never extracted) |
| 3 | *Grossesse actuelle* | `last_menstrual_period` (*DDR*) and the visit grid: columns `T1_V1`–`T1_V3`, `T2_V1`–`T2_V3`, `M7`, `M8`, `M9` |
| 4–8 | Delivery, early and late postpartum (mother, newborn) | Out of the v1 field set |

Each **column** of the visit grid is one **encounter** (antenatal visit); a column becomes an encounter when its *Venue le* cell is written. The specimen layout has no place for `midwife_patient_code`.

## Field catalog (12 fields)

### Document-level fields (`document_fields`)

| Field | Type / unit | Source on paper | Validation |
|-------|-------------|-----------------|------------|
| `registry_file_number` | text (digits) | Cover, *N° de la fiche* | 3–12 digits; **link key** |
| `midwife_patient_code` | text | Cover, code written by the midwife (e.g. `CM: 164125`) | 3–20 chars `A-Z 0-9 -`; **link key** |
| `facility_name` | text | Cover, *Nom de l'établissement sanitaire* | free text, 2–80 chars |
| `last_menstrual_period` | date | Current pregnancy header, *DDR* | ISO date, not in the future |

### Encounter-level fields (`encounters[].fields`)

| Field | Type / unit | Grid row | Validation |
|-------|-------------|----------|------------|
| `visit_date` | date | *Venue le* | ISO date, **required to register** |
| `gestational_age_days` | integer, days | *Âge probable de la grossesse* (`16SA+3j` → 115) | 0–315 |
| `weight_kg` | decimal, kg | *Poids* | 30–200 |
| `systolic_bp_mmhg` | integer, mmHg | *TA* (`12/7` → 120) | 60–250 |
| `diastolic_bp_mmhg` | integer, mmHg | *TA* (`12/7` → 70) | 30–150 |
| `fundal_height_cm` | integer, cm | *HU* | 5–50 |
| `syphilis_test` | enum `NEGATIVE` / `POSITIVE` | *Syphilis (TPHA/VDRL)* | enum |
| `hiv_test` | enum `NEGATIVE` / `POSITIVE` | *Sérologie VIH* | enum |

Never extracted: patient name, husband's name, national ID number, phone, address, even when visible on the page (see `pii_detected`).

## Field value object

Every field, at both levels, has the same shape:

```json
{
  "raw_text": "1?/12/25",
  "value": "2025-12-12",
  "unit": null,
  "confidence": 0.42,
  "field_status": "NEEDS_REVIEW",
  "validation_flags": ["DIGIT_UNCLEAR"],
  "source": {"page_ref": "data/Paper Registry/dossiers_specimen_10_patientes-03.png", "row": "Venue le", "column": "9e mois"},
  "verification": {"state": "UNVERIFIED", "by": null, "at": null},
  "corrections": []
}
```

- `raw_text`: what is written on paper, unchanged. `null` if the cell is empty.
- `value`: normalized value in the field's type/unit. `null` unless a value was read or entered.
- `confidence`: extractor score in `[0, 1]`, or `null` for manual entry and blank cells. For the Tesseract extractor it is the share of the four independent readings that agree (0.75 = three of four); it is not a calibrated probability.
- `validation_flags`: format/plausibility warnings. Values are flagged, never silently changed. Flags set by the OCR extractor:

| Flag | Meaning |
|------|---------|
| `BLANK_ON_PAGE` / `MARKED_DASH` | Cell has no pen mark / holds only a dash (status `NOT_PROVIDED`) |
| `OCR_READINGS_DISAGREE` / `OCR_FEW_READINGS_AGREE` / `OCR_VALUE_UNCLEAR` | Fewer than three readings agree, one disagrees, or nothing parses |
| `AGE_INCONSISTENT_WITH_DATES` / `AGE_NOT_CONFIRMED` | Gestational age does not match DDR and visit date, or nothing confirms it |
| `DDR_NOT_CONFIRMED_BY_VISITS` | DDR does not match the visits' dates and gestational ages |
| `VISIT_DATES_OUT_OF_ORDER` | Visit dates do not increase from column to column |
| `WEIGHT_CHANGE_IMPLAUSIBLE` | Drop over 1.5 kg, or gain over 1.5 kg + 0.15 kg/day, between neighbouring visits |
| `FUNDAL_HEIGHT_NOT_CONFIRMED` / `FUNDAL_HEIGHT_INCONSISTENT_WITH_AGE` | No confirmed gestational age, or more than 6 cm from the age in weeks |
| `NOT_ON_THIS_LAYOUT` | The specimen layout has no place for this field |

### Field status (`field_status`): what the paper says

| Status | Meaning | `value` |
|--------|---------|---------|
| `KNOWN` | Value read with confidence ≥ 0.75 and passes validation | required |
| `NEEDS_REVIEW` | Value read but low confidence or failed validation, or manual entry pending | optional |
| `ILLEGIBLE` | Something is written but cannot be read | `null` |
| `NOT_PROVIDED` | Cell left blank, or marked as not done (`NF`); flag `SECTION_NOT_CAPTURED` if the page was not photographed | `null` |
| `UNKNOWN` | Paper explicitly records the value as unknown (`?`, *inconnu*) | `null` |
| `NOT_APPLICABLE` | Field does not apply to this encounter | `null` |

### Verification (`verification.state`): what a human did

Separate from `field_status`. High confidence is **not** human approval.

| State | Meaning |
|-------|---------|
| `UNVERIFIED` | No human action yet |
| `CONFIRMED` | Reviewer accepted the extracted value/status (individually, or by the final record confirmation) |
| `CORRECTED` | Reviewer changed the value or status, and the change is recorded in `corrections` |

### Correction history (`corrections[]`)

Append-only:

```json
{
  "at": "2026-10-03T18:42:10+00:00",
  "by": "agent.demo",
  "action": "CORRECT",
  "previous": {"value": "2025-12-12", "field_status": "NEEDS_REVIEW"},
  "new": {"value": "2025-12-19", "field_status": "KNOWN"}
}
```

## Draft object

One draft per document (re-extraction replaces it and increments `draft_revision`).

```json
{
  "schema_version": "1.0",
  "layout_id": "ma-fiche-surveillance-grossesse-v1",
  "extraction": {
    "extractor": "fixture",
    "extractor_version": "fixture-0.1",
    "processed_at": "2026-10-03T18:40:00+00:00"
  },
  "pages": [{"page_ref": "data/Paper Registry/dossiers_specimen_10_patientes-01.png", "section": "cover"}],
  "pii_detected": [{"page_ref": "data/Paper Registry/dossiers_specimen_10_patientes-01.png", "category": "PATIENT_NAME", "action": "NOT_EXTRACTED"}],
  "document_fields": {"registry_file_number": {"...": "field value object"}},
  "encounters": [
    {"encounter_type": "ANTENATAL", "slot": "M9", "fields": {"visit_date": {"...": "field value object"}}}
  ]
}
```

`extraction.extractor` is `fixture`, `manual`, `tesseract`, `paddleocr`, or the external service's name (`--extractor http`); `extractor_version` must change whenever prompts/models change so results are traceable.

## Identifiers

All internal IDs are generated by the backend, sequential, and never derived from personal data or link keys.

| ID | Format | Scope |
|----|--------|-------|
| `document_id` | `DOC-000001` | One upload session (group of pages) |
| `page_id` | `PAGE-000001` | One received photo |
| `patient_id` | `PAT-000001` | One woman within one facility |
| `visit_id` | `VIS-000001` | One encounter, unique per (`patient_id`, `encounter_type`, `visit_date`) |
| `source_message_id` | WhatsApp `wamid…` | Idempotency key for ingest |

## Document lifecycle

`CAPTURED` → `PENDING_AI` → `AI_PROCESSED` → `NEEDS_REVIEW` → `VALIDATED` → `PATIENT_MATCHED` → `REGISTERED` → `SYNCED`

Failure/parking states: `PROCESSING_FAILED`, `DUPLICATE_SUSPECTED`, `MANUAL_REVIEW_REQUIRED` (waiting for a retaken photo), `SYNC_FAILED`.

| State | Entered when |
|-------|--------------|
| `CAPTURED` | Page received; grouping window still open |
| `PENDING_AI` | Window closed (or pages regrouped); waiting for extraction, including while AI is unavailable |
| `AI_PROCESSED` | Draft stored (transient) |
| `NEEDS_REVIEW` | At least one blocking field (see below) |
| `VALIDATED` | No blocking field; no patient selected yet |
| `PATIENT_MATCHED` | No blocking field and a reviewer has selected the patient (auto-link only suggests a candidate) |
| `REGISTERED` | Reviewer confirmed; visits written |
| `SYNCED` | The central registry acknowledged the anonymized visits |
| `SYNC_FAILED` | Delivery to the central registry failed; retried with back-off until `SYNCED` |
| `PROCESSING_FAILED` | No usable extraction; reviewer can start manual entry |
| `MANUAL_REVIEW_REQUIRED` | A new photo of a page was requested (by the reviewer, or automatically for an unusable photo); the midwife's next photo replaces the page and the document returns to `PENDING_AI` |
| `DUPLICATE_SUSPECTED` | Reviewer answered *Je ne sais pas* to the patient question |

**Blocking fields:** unverified fields in `NEEDS_REVIEW` or `ILLEGIBLE`, and any `visit_date` without a value.

**Registration gate:** only `PATIENT_MATCHED` can be confirmed, and only by an explicit reviewer action. Auto-linking never registers.

## Confidence policy

- `confidence < 0.75` → `NEEDS_REVIEW` (the reviewer is asked a follow-up question). The OCR extractor is stricter: `KNOWN` also needs the booklet-level consistency checks to pass.
- `KNOWN` fields are still shown in the final summary and become `CONFIRMED` by the reviewer's confirmation.
- Report accuracy of `KNOWN` fields separately: a wrong value marked `KNOWN` is the worst error (it escapes review).
