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

Each **column** of the visit grid is one **encounter** (antenatal visit). One photo can contain several encounters, and one document can contain several pages.

Other sections (history, delivery, postpartum, newborn) are out of the v1 field set.

### Several visits and several pages

These rules extend v1.0 without changing its shape. Every existing draft and fixture stays valid.

- **One encounter per populated column.** A column is populated when OCR read something other than a dash in any of its rows. A populated column without a readable date is kept: its `visit_date` has no value and blocks registration (`REQUIRED_MISSING`). Empty columns create no encounter. A column with ink but no text read creates no encounter either, because the blank-cell check also sees ink in some empty cells. The page goes to review instead (`GRID_COLUMN_UNREAD`, columns listed in `pages[].grid.unread_columns`). Encounters follow the column order `T1_V1` … `M9`, never only the latest visit.
- **The same slot on several pages** (two photos of one grid) gives **one** encounter. Agreeing pages keep the best reading. If the pages disagree, the field is `NEEDS_REVIEW` with no value and `CONFLICT_ACROSS_PAGES`. A disagreement is a different value, or a value or unread writing on one page and a verified blank on another. Nothing is silently overwritten: `source.readings` lists every page's reading. The same rule applies to document fields read on two covers or two grid pages.
- **`encounters` may be empty.** This happens when no page shows the visit grid (cover, history, delivery, postpartum or newborn pages only), or when the grid is read and all its columns are empty. Registration then writes no visit. A grid page whose table cannot be found still gets one `MANUAL` encounter for manual entry, sourced to that page, even when other pages gave visits; until the reviewer confirms its section, the page itself also goes to review (`GRID_NOT_FOUND`). If such a page holds no new visit, the reviewer requests a retake or moves the page out of the document. A visit-like table on a page whose printed labels say another section is `SECTION_UNCERTAIN`, and gives no encounter until a reviewer chooses the section.
- **Slot dates stay distinct:** two encounters with the same `visit_date` block registration (`DUPLICATE_ENCOUNTER_DATE`), as before.

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
| `gestational_age_days` | integer, days | *Âge probable de la grossesse* (`16SA+3j`, `16 SA 3 j`, `16+3` → 115; `16 SA` → 112) | 0–315 |
| `weight_kg` | decimal, kg | *Poids* | 30–200 |
| `systolic_bp_mmhg` | integer, mmHg | *TA* (`12/7` → 120) | 60–250 |
| `diastolic_bp_mmhg` | integer, mmHg | *TA* (`12/7` → 70, `12/7,5` → 75) | 30–150, below the systolic |
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
- `source`: where the value was read: `page_ref` (required), and optionally `row`, `column` and `bbox` (`[x0, y0, x1, y1]` as fractions of the image that was read). Two optional keys are checked by `validate_draft` when present:
  - `readings`: present when two or more pages showed the field. It has one object per page, in page order: `page_ref`, `raw_text`, `value`, `field_status`, `confidence`, `validation_flags` and optionally `bbox`. It is evidence only: the field's own `value` and `field_status` are what count.
  - `normalization`: present when the stored value is not the written number, for example `{"from": "cmHg", "to": "mmHg", "factor": 10}` for `12/7`, or `{"from": "weeks+days", "to": "days", "weeks": 28, "days": 3}` for `28 SA + 3 j`. `raw_text` keeps what was written.

Gestational age is stored in days, so weeks plus days are never rounded. Days need a separator, a weeks unit or a `j` (`28+3`, `28 SA 3`, `28 3j`). `283`, `28 3` and decimal weeks (`28,5`) are refused, because they could be 28+3, 28+5 or a decimal.

Reasons a value goes to review (flags added by local OCR, besides the reading flags in [ocr.md](./ocr.md)):

| Flag | Meaning |
|------|---------|
| `CONFLICT_ACROSS_PAGES` | Pages disagree; no value is chosen. A field with this flag cannot be `KNOWN` before a reviewer acts |
| `OCR_TEXT_SPANS_COLUMNS` | One text box crossed a column rule. It was split into words by position, and both cells are reviewed |
| `BP_LOOKS_LIKE_DATE` | The TA cell holds a date (`10/05/2025`), or a pair with a leading zero (`10/05`, `12/07`) |
| `BP_ORDER_IMPLAUSIBLE` | The diastolic would not be below the systolic (`11/12` would give 110/120) |
| `GA_FORMAT_AMBIGUOUS` | Decimal weeks (`28.5`): no value, because the days would be guessed |

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

`extraction.extractor` is `fixture`, `manual`, or the OCR engine name (`paddleocr`, `tesseract`); `extractor_version` must change whenever prompts, models or the parser change, so that results are traceable.

`pages[]` items need `page_ref`; local OCR adds `section`, `section_source` (`detected` or `reviewer`), `section_confidence`, `read`, `review_reason` (a blocking page item, e.g. `UNKNOWN_LAYOUT`, `SECTION_UNCERTAIN`, `GRID_NOT_FOUND`, `GRID_COLUMN_UNREAD`, `PHOTO_UNUSABLE`, `OCR_TIMEOUT`), `image` geometry and, on grid pages, `grid`: `{"found": true, "visit_columns": [...], "unread_columns": [...], "blank_columns": [...]}` or `{"found": false}`. A section chosen by the reviewer settles the page (`review_reason` null); its unread fields still go to review one by one. The reviewer sets it with `POST /api/pages/{page_id}/section` and `{"section": "<section or null>", "expected_revision": <revision shown>}`; a stale revision is refused (`STALE_REVISION`), and a change discards the draft's review and extracts the document again.

### Extended fields (optional `extended` key, catalog `excel-ext-1`)

Paper values beyond the 12 fields, read for the CSV columns of [excel-field-mapping.md](./excel-field-mapping.md). The key is optional: fixtures and v1.0 drafts without it stay valid, and `schema_version` stays `1.0`. It is defined in `dayone/extended.py`, read by `dayone/ocr_extended.py` (specimen layout only, decision D7), and validated by `validate_draft` when present. `GET /api/system` lists it under `catalog.extended`, with each field's scope, kind, unit, choices and paper source.

```json
"extended": {
  "catalog_version": "excel-ext-1",
  "pregnancy": {"maternal_age_years": {"...": "field value object"}},
  "previous_deliveries": [{"column": 1, "fields": {"previous_delivery_date": {"...": "field value object"}}}],
  "visit_labs": [{"slot": "T1_V1", "fields": {"hemoglobin_g_dl": {"...": "field value object"}}}],
  "newborns": [{"index": 1, "fields": {"birth_weight_g": {"...": "field value object"}}}],
  "newborn_consultations": [{"period": "EARLY", "fields": {"feeding_mode": {"...": "field value object"}}}]
}
```

| Section | Scope | Item key | Fields (unit) | Page |
|---------|-------|----------|---------------|------|
| `patient` | patient | none | `education_level_text` (text, as written) | identification |
| `pregnancy` | pregnancy | none | `maternal_age_years` (years), `gravidity`, `parity`, `living_children_count`, `abortions_count` (counts), `consanguinity_mark`, `pregnancy_desired_mark` (box state), `height_cm` (cm) | identification; height on the pregnancy grid page |
| `previous_deliveries` | previous delivery | `column` 1–5 (*Accouch. n*) | `previous_delivery_date`, `previous_delivery_mode_text` (text, as written) | identification |
| `visit_labs` | visit | `slot` (grid column) | `hemoglobin_g_dl` (g/dL), `blood_glucose_g_l` (g/L, fasting not stated), `albuminuria` (`NEGATIVE`/`POSITIVE`) | pregnancy grid |
| `delivery` | delivery | none | `delivery_date`, `delivery_gestational_age_days` (days), `delivery_mode` (`VAGINAL_NON_INSTRUMENTAL`, `VAGINAL_INSTRUMENTAL`, `CESAREAN_PLANNED`, `CESAREAN_EMERGENCY`) | delivery |
| `newborns` | newborn | `index` (1 = the one newborn block) | `newborn_sex` (`FEMALE`/`MALE`), `birth_weight_g` (g), `birth_head_circumference_cm` (cm) | delivery |
| `newborn_consultations` | newborn consultation | `period` `EARLY`/`LATE` | `consultation_date`, `feeding_mode` (`EXCLUSIVE_BREASTFEEDING`, `ARTIFICIAL`, `MIXED`) | newborn post-partum |

Rules:

- **Present means read.** A section exists only when a page of its type was read; a field exists only when its page type was read. An absent section or field was not captured; it is never `NOT_PROVIDED`.
- **Only direct readings.** Nothing is computed across pages, visits or previous deliveries, converted to a CSV code, or filled in. A box is stored as `MARKED` or `UNMARKED`; an empty box is not "no" (D5). `blood_glucose_g_l` is never called fasting, `albuminuria` is not proteinuria, `feeding_mode` is the feeding on the day of a later consultation and not proof of initiation at birth. Family history is not read at all. No pre-pregnancy weight or BMI exists on the forms, so none is stored.
- **Item keys are positions on the page**, never CIN, names or patient IDs. No pregnancy, delivery or newborn ID exists until D1 and D6 are decided.
- **Kind `choice`** (new): a closed set of codes with accepted French spellings (`FieldSpec.choices`). The value is the code. **`FieldSpec.decimals`** (new, default 1) sets the rounding of a decimal; glucose keeps 2.
- **Field value objects** are the same as above. A checkbox value has `confidence: null` and `source.checkboxes`: `[{"label", "state", "ink"}]` with `state` in `MARKED`, `UNMARKED`, `UNCLEAR`, `BOX_NOT_FOUND`, `LABEL_NOT_FOUND`.
- **Never blocking.** Extended fields are not in `review.blocking` and do not stop registration. The final confirmation does **not** confirm them: an extended field is `CONFIRMED` or `CORRECTED` only after an explicit review of that field, otherwise it stays `UNVERIFIED`.
- **Kept with the document.** The draft, including `extended`, stays in `documents.draft_json` after registration. Extended values are not written to `visits.fields_json`; no table was added.

| Flag | Meaning |
|------|---------|
| `CHECKBOX_UNCLEAR` | Ink inside the box is between the empty and marked limits |
| `CHECKBOX_NOT_FOUND` | The label was read but no printed square was found next to it |
| `CHECKBOX_MULTIPLE_MARKED` | Several boxes of one choice are marked; no value |
| `CHECKBOX_NONE_MARKED` | Every box of one choice is clearly empty (`NOT_PROVIDED`) |
| `LAB_WITHOUT_VISIT` | A lab cell has writing in a grid column that is not a visit |
| `OCR_CONFIRM_REQUIRED` | Always on handwritten counts and free text (also used for cover fields) |

## Identifiers

All internal IDs are generated by the backend, sequential, and never derived from personal data or link keys.

| ID | Format | Scope |
|----|--------|-------|
| `document_id` | `DOC-000001` | One upload session (group of pages) |
| `page_id` | `PAGE-000001` | One received photo |
| `patient_id` | `PAT-000001` | One woman within one facility |
| `visit_id` | `VIS-000001` | One encounter, unique per (`patient_id`, `encounter_type`, `visit_date`) |
| `request_id` | `RTK-000001` | One retake request for one page |
| `inbound_id` | `WIN-000001` | One received WhatsApp Cloud API message (cloud mode only) |
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

**Blocking fields:** pages with a `review_reason` (listed first), unverified fields in `NEEDS_REVIEW` or `ILLEGIBLE` (including `CONFLICT_ACROSS_PAGES`), any `visit_date` without a value, and two encounters with the same date. Extended fields never block. Extractors never mark a field as verified: only a reviewer's action, or the final confirmation, sets `CONFIRMED` or `CORRECTED`.

**Registration gate:** only `PATIENT_MATCHED` can be confirmed, and only by an explicit reviewer action. Auto-linking never registers. A document with a `PENDING` retake request cannot be confirmed (`409 RETAKE_PENDING`).

## Retake requests

A reviewer can ask the sender for a new photo of one page of a document that is not `REGISTERED`. Stored in `retake_requests`:

| Column | Meaning |
|--------|---------|
| `request_id` | `RTK-000001` |
| `document_id`, `page_id` | Page to re-photograph (follows the page if it is moved) |
| `sender_id`, `facility_id` | Who must answer; a replacement from any other sender or facility is refused (`403`) |
| `status` | `PENDING` → `FULFILLED` (new photo received) or `CANCELLED` (by a reviewer). At most one `PENDING` request per page |
| `requested_by`, `requested_at` | Reviewer and time |
| `request_message_id` | Outbound message « Merci de reprendre la photo de la page N. » in `messages` |
| `replacement_page_id`, `replacement_message_id` | Set when fulfilled |
| `closed_by`, `closed_at` | Reviewer who cancelled / time of fulfilment or cancellation |

Rules:

- Requesting again while a request is `PENDING` returns the same request (`created: false`) and sends no second message.
- The replacement is a normal ingest call with `retake_request_id`. The simulator sends it explicitly; the Cloud API adapter derives it from the reply context of the request message (see below). A duplicate `message_id` is ignored as for any page.
- The original page is **kept**: `pages.replaced_by` points to the new page, which takes the same position. Active pages are those with `replaced_by IS NULL`; only they are sent to extraction.
- After a replacement the draft and the patient selection are discarded (event `PAGE_REPLACED` records how many reviews were lost), the revision increases, and the document returns to `PENDING_AI` (unless still `CAPTURED`). Registered visits are never touched: the new draft goes through the normal `EXISTS_SAME` / `EXISTS_DIFFERENT` (update or keep) decision at confirmation.
- An extraction started before the replacement is discarded when it finishes (the active page IDs changed).
- `next_step` is `WAITING_RETAKE` while a request is pending.

Events: `RETAKE_REQUESTED`, `PAGE_REPLACED`, `RETAKE_CANCELLED`.

## WhatsApp Cloud API jobs

Used only in `--whatsapp-mode cloud` (`dayone/whatsapp.py`; setup in [whatsapp-cloud.md](./whatsapp-cloud.md)). The ingest, review and retake contracts above are unchanged: the adapter calls `ingest_photo` like the simulator does.

**`senders.channel`:** `SIMULATOR` (default, never sent to) or `WHATSAPP`. `sender_id` is `whatsapp:+<digits>`. Set with `python -m dayone.whatsapp add-sender`.

**`whatsapp_inbound`** has one row per received WhatsApp message, and is written in the webhook transaction before the `200` response.

| Column | Meaning |
|--------|---------|
| `inbound_id` | `WIN-000001` |
| `wa_message_id` | WhatsApp `wamid`, **unique**. Redeliveries are dropped here, and become `pages.source_message_id` |
| `message_type`, `sender_id` | WhatsApp type; registered sender, or `NULL` when rejected |
| `media_id`, `mime_type`, `media_sha256`, `context_message_id` | Image data from the webhook (kept only for `PENDING` rows) |
| `status` | `PENDING` → `DONE` or `FAILED`; terminal on arrival: `IGNORED` (not an image), `REJECTED` (unregistered number) |
| `attempts`, `next_attempt_at`, `last_error` | Retry state. `last_error` is a short code (e.g. `HTTP_503_131016`), never a URL or secret |
| `media_ref`, `page_id` | `whatsapp-media/<sha256>.jpg\|png` and the page created by `ingest_photo` |

**`whatsapp_outbound`** has one row per `OUT` message to a `WHATSAPP` sender. A trigger inserts it in the same transaction as the `messages` row.

| Column | Meaning |
|--------|---------|
| `message_id` | `messages.id` (text = French acknowledgment, retake request, cancellation, resend request) |
| `status` | `PENDING` → `SENT` → `DELIVERED` → `READ` (never moves backwards), or `FAILED` |
| `attempts`, `next_attempt_at`, `last_error` | Retry state, as above |
| `wa_message_id` | `wamid` returned by Meta, **unique**. Status webhooks and retake replies are matched on it |

**Media references:** `whatsapp-media/<64 hex>.jpg|png` resolves only inside `WHATSAPP_MEDIA_DIR`. Any other name is `MEDIA_NOT_FOUND`.

**Retake mapping:** an image whose `context.id` equals the `wa_message_id` of a `PENDING` request's message becomes that request's replacement. Otherwise it is a normal page.

## Confidence policy (initial, to calibrate)

- `confidence < 0.75` → `NEEDS_REVIEW` (the reviewer is asked a follow-up question).
- `KNOWN` fields are still shown in the final summary and become `CONFIRMED` by the reviewer's confirmation.
- Report accuracy of `KNOWN` fields separately: a wrong value marked `KNOWN` is the worst error (it escapes review).
