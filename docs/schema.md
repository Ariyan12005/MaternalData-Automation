# Shared schema (v1.0)

Contract between extraction (Fatma), backend (Ariyan) and review UI (Aymane).
Machine-readable definitions live in `dayone/schema.py`; fixtures in `fixtures/` must validate against it (`python -m unittest`).

## Supported layout

`layout_id`: **`ma-fiche-surveillance-grossesse-v1`**: Moroccan Ministry of Health *Fiche de surveillance de la grossesse et du post-partum* (pink booklet).

| Page | Section | Used for |
|------|---------|----------|
| Cover (`1-1.jpg`) | Identification / facility header | Patient link keys, facility |
| Current pregnancy, left (`1-4.jpg`) | *Grossesse actuelle*, 1st trimester visit columns + DDR | Visits `T1_V1`–`T1_V3`, LMP |
| Current pregnancy, right (`1-5.jpg`) | 2nd trimester, 7th/8th/9th month columns | Visits `T2_V1`–`T2_V3`, `M7`, `M8`, `M9` |

Each **column** of the visit grid is one **encounter** (antenatal visit). One photo can contain several encounters.

Other sections (history, delivery, postpartum, newborn) are out of the v1 field set.

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
  "source": {"page_ref": "data/Paper Registry/1-5.jpg", "row": "Venue le", "column": "9ème mois"},
  "verification": {"state": "UNVERIFIED", "by": null, "at": null},
  "corrections": []
}
```

- `raw_text`: what is written on paper, unchanged. `null` if the cell is empty.
- `value`: normalized value in the field's type/unit. `null` unless a value was read or entered.
- `confidence`: extractor score in `[0, 1]`, or `null` for manual entry. **Heuristic until calibrated** on labeled pages.
- `validation_flags`: format/plausibility warnings. Values are flagged, never silently changed.

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
  "pages": [{"page_ref": "data/Paper Registry/1-1.jpg", "section": "identification"}],
  "pii_detected": [{"page_ref": "data/Paper Registry/1-1.jpg", "category": "PATIENT_NAME", "action": "NOT_EXTRACTED"}],
  "document_fields": {"registry_file_number": {"...": "field value object"}},
  "encounters": [
    {"encounter_type": "ANTENATAL", "slot": "M9", "fields": {"visit_date": {"...": "field value object"}}}
  ]
}
```

`extraction.extractor` is `fixture`, `manual`, or the real model name (only `fixture` and `manual` exist today); `extractor_version` must change whenever prompts/models change so results are traceable.

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

`CAPTURED` → `PENDING_AI` → `AI_PROCESSED` → `NEEDS_REVIEW` → `VALIDATED` → `PATIENT_MATCHED` → `REGISTERED` (→ `SYNCED`, reserved for export, not in MVP)

Failure/parking states: `PROCESSING_FAILED`, `DUPLICATE_SUSPECTED`, `MANUAL_REVIEW_REQUIRED`, `SYNC_FAILED` (reserved).

| State | Entered when |
|-------|--------------|
| `CAPTURED` | Page received; grouping window still open |
| `PENDING_AI` | Window closed (or pages regrouped); waiting for extraction, including while AI is unavailable |
| `AI_PROCESSED` | Draft stored (transient) |
| `NEEDS_REVIEW` | At least one blocking field (see below) |
| `VALIDATED` | No blocking field; no patient selected yet |
| `PATIENT_MATCHED` | No blocking field and a reviewer has selected the patient (auto-link only suggests a candidate) |
| `REGISTERED` | Reviewer confirmed; visits written |
| `PROCESSING_FAILED` | No usable extraction; reviewer can start manual entry |
| `DUPLICATE_SUSPECTED` | Reviewer answered *Je ne sais pas* to the patient question |

**Blocking fields:** unverified fields in `NEEDS_REVIEW` or `ILLEGIBLE`, and any `visit_date` without a value.

**Registration gate:** only `PATIENT_MATCHED` can be confirmed, and only by an explicit reviewer action. Auto-linking never registers.

## Confidence policy (initial, to calibrate)

- `confidence < 0.75` → `NEEDS_REVIEW` (the reviewer is asked a follow-up question).
- `KNOWN` fields are still shown in the final summary and become `CONFIRMED` by the reviewer's confirmation.
- Report accuracy of `KNOWN` fields separately: a wrong value marked `KNOWN` is the worst error (it escapes review).
