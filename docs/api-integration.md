# Current local PaddleOCR API contract (branch pinar)

Base URL: `http://127.0.0.1:8001/api`. This is a local development address,
not a published or remotely accessible service. No public URL has been deployed.
Start with:

```sh
source .venv-paddle312/bin/activate
python -m dayone --extractor paddleocr --port 8001 --db var/dayone-paddleocr.sqlite3
```

Important: the current server resets this demo database on PaddleOCR startup.
Do not use it as a production persistence service.

## Integration flow

1. `GET /api/system`: verify `extractor: "paddleocr"`; returns field catalog,
   `ai_available`, and `grouping_window_seconds` (default 8).
2. `GET /api/senders`: obtain a registered demo `sender_id`.
3. `GET /api/media`: obtain existing repository media references. For OCR use
   only `dossiers_specimen_10_patientes-01.png` through `-06.png`.
4. `POST /api/whatsapp/messages`, JSON body:

```json
{
  "sender_id": "whatsapp:+212600000001",
  "message_id": "unique-integration-message-id",
  "media_ref": "data/Paper Registry/dossiers_specimen_10_patientes-03.png"
}
```

The sender above is simulated demo metadata. Responses contain `page_id`,
`document_id`, `position`, `duplicate`, and `acknowledgment`. Replaying a
message ID is idempotent. Send all pages within the grouping window; processing
begins after the last photo's window expires. This endpoint accepts existing
media references, not multipart uploads, image bytes, or arbitrary image URLs.

5. Poll `GET /api/documents/{document_id}`. The response contains `document`,
   `next_step`, `pages`, `draft`, `review`, `registration`, `events`, and
   `move_targets`. `draft` is null while pending or after extraction failure.
6. Review via `POST /api/documents/{document_id}/fields` with `X-Reviewer` header,
   `scope`, `field`, `encounter_index` for encounter fields, `action`
   (`CONFIRM`, `CORRECT`, `SET_STATUS`), and the latest `expected_revision`.
   Corrections supply `value`; status changes supply `field_status`.
7. Select a patient via `POST /api/documents/{document_id}/patient`, then explicitly
   confirm via `POST /api/documents/{document_id}/confirm`. Review and patient
   selection alone do not save clinical visits. Both use `X-Reviewer` and the
   latest `expected_revision`. See `docs/schema.md` for the stored draft schema.

No authentication or CORS integration layer is implemented here; use a local
same-origin integration or a controlled backend proxy for this prototype.

## Actual extraction support

Schema version `1.0`; layout `ma-fiche-surveillance-grossesse-v1`.
`encounters[]` can represent multiple visits and fixtures do preserve them.
**The current live PaddleOCR adapter returns one encounter only:** final `M9`
column of the selected `-03.png` grid, or a blank `MANUAL` encounter for other
page sets. Earlier visit columns are not extracted or preserved. Do not use
this result to compute enrollment age or a mean across all page visits.
A cover-only draft has a placeholder encounter requiring manual review, not
an observed visit.

`midwife_patient_code` exists in the schema but is **not extracted** by this
adapter. It returns null / `NEEDS_REVIEW`. No fixture values are used in live OCR.
Cover registry number recognition requires the label and digits in
a single OCR detection. The selected cover now yields a registry number from
PaddleOCR. Readings without that complete context require review. Facility
and DDR still require manual review.

| Scope | Field | Stored unit | Current selected-PNG OCR |
|---|---|---|---|
| document | registry_file_number | null | Labelled OCR detection required |
| document | midwife_patient_code | null | Not extracted |
| document | facility_name | null | Not extracted |
| document | last_menstrual_period | null | Not extracted |
| encounter | visit_date | null (ISO date string) | M9 date |
| encounter | gestational_age_days | days | M9; OCR weeks normalized to days |
| encounter | weight_kg | kg | M9 |
| encounter | systolic_bp_mmhg | mmHg | M9 |
| encounter | diastolic_bp_mmhg | mmHg | M9 |
| encounter | fundal_height_cm | cm | M9 |
| encounter | syphilis_test | null | Not extracted; requires review |
| encounter | hiv_test | null | Not extracted; requires review |

Each field has `raw_text`, `value`, `unit`, `confidence`, `field_status`,
`validation_flags`, `source`, `verification`, and `corrections`.
Confidence is PaddleOCR confidence normalized to [0,1], taking the minimum
across cell tokens. It is not a calibrated clinical accuracy probability.
Live `KNOWN` requires confidence >= 0.85, a complete reading, and deterministic
format/range validation. `KNOWN` still has verification state `UNVERIFIED`.
Current sources contain `page_ref` only; no bounding boxes, row, or column
metadata is returned. The encounter's `slot: "M9"` identifies the visit column.

Unread, invalid, low-confidence, missing, and potentially illegible OCR fields
all return `value: null`, `raw_text: null`, `confidence: null`,
`field_status: "NEEDS_REVIEW"`, flag `OCR_VALUE_UNCLEAR`, and a page reference.
OCR does not reliably distinguish empty cells from illegible ones. A reviewer
can explicitly choose `ILLEGIBLE`, `NOT_PROVIDED`, `UNKNOWN`, or
`NOT_APPLICABLE`; none represents zero or a negative test. Required visit dates
cannot be left missing before saving. Name, CIN, phone, address, and spouse
name are not returned as field values or stored OCR text.

## Example responses

[Cover response](api-examples/cover-response.json) and
[visit-grid response](api-examples/visit-grid-response.json) are complete
`GET /api/documents/{id}` response examples, generated using actual local
PaddleOCR reads of the selected PNGs through the API service. They are
integration artifacts only; the extractor never reads these files.
IDs, timestamps, revisions, and confidence can vary between runs.

## Errors and queue behavior

HTTP failures use this shape:

```json
{"error":{"code":"MEDIA_NOT_FOUND","message":"Image introuvable.","details":null}}
```

Typical HTTP statuses: 400 malformed JSON; 422 invalid field/input or missing
reviewer; 403 unknown sender; 404 missing media/document/route; 409 stale
revision or invalid workflow transition; 413 body too large; 500 unexpected
API error (`INTERNAL`).

Extraction is asynchronous: the ingest request can succeed even if OCR later
fails. Polling the document still returns HTTP 200 with
`document.status: "PROCESSING_FAILED"`, `draft: null`, and
`document.failure_reason`, such as `RETAKE_REQUIRED`,
`OCR_DEPENDENCY_MISSING`, or `INVALID_LIVE_OCR_DRAFT`. The status-change event
contains the failure code/message. PaddleOCR model initialization failure returns `OCR_INITIALIZATION_FAILED`;
inference failure returns `OCR_FAILED`; malformed output returns `OCR_INVALID_RESULT`.
Unexpected extraction exceptions leave the document pending for retry; there
is no retry limit. If `ai_available` is false, the local OCR queue pauses despite
using no AI model. A failed draft can enter manual entry via the existing API.

## Coverage against excel-field-mapping.md

That file remains a proposal and predates the live PaddleOCR adapter. Schema
v1.1, history fields, outcome fields, derived metadata, Excel aggregation,
and the proposed exporter are not implemented.

| Excel columns | Current live coverage |
|---|---|
| 1 id | Backend patient ID is generated only through registration |
| 14–15 mean BP | Per-visit BP supported for M9; means/export not implemented |
| 19–20 HIV/syphilis | Schema fields present; selected-PNG OCR needs review |
| 22 enrollment GA | M9 GA supported; earliest/enrollment GA not established |
| 2–12 history/demographics | Not implemented |
| 13 BMI; 16–18 hemoglobin/glucose/proteinuria | Not implemented |
| 21 HCV; 23 gestational DM | Not implemented; no clinical inference |
| 24–31 delivery/postpartum/newborn/referral | Not implemented |

Weight, fundal height, and visit date support integration of antenatal visit
records but are not separate columns in the proposed 31-column Excel mapping.
Start integration with these six observed M9 fields plus the review workflow;
full Excel coverage and multi-column visit extraction remain incremental work.

PaddleOCR uses local PP-OCRv6 medium detection/recognition on CPU (no VLM).
Python 3.12 is required by this project setup. First use may download model
weights; subsequent inference uses cached weights locally. No photo is sent
to cloud OCR. No Tesseract fallback exists. Model score thresholds are
conservative screening rules, not a guarantee of correct recognition.
