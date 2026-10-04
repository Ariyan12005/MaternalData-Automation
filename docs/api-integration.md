# API integration (branch `p-ocr-camera`)

Local development server only; nothing is deployed. Start it with one of:

```sh
python3 -m dayone                                   # fixture extraction, demo seed
python3 -m dayone --extractor tesseract --port 8001 # local OCR of the specimen pages (starts with an empty demo)
python3 -m dayone --extractor http                  # external model service (DAYONE_EXTRACTOR_URL)
```

The first start prints a one-time password for the `admin` account. Create named accounts with
`python3 -m dayone --add-user amina --role reviewer`.

## Authentication

| Step | Request |
|---|---|
| Sign in | `POST /api/login` `{"username", "password"}` → user, and a `dayone_session` cookie (HttpOnly, SameSite=Strict, 12 h; Secure under HTTPS) |
| Every `POST /api/*` | must send `X-Requested-With: dayone` (CSRF guard) |
| Who am I | `GET /api/me` |
| Sign out | `POST /api/logout` |

Without a session every `/api/*` route and `/media/*` answers `401 LOGIN_REQUIRED`; admin routes answer
`403 ADMIN_ONLY` to reviewers. Five wrong passwords lock an account for five minutes (`429 LOCKED`).
The reviewer recorded on reviews, confirmations and retakes is the signed-in user.

## Routes

| Method and path | Role | Purpose |
|---|---|---|
| `GET /api/system` | any | extractor, AI and central-registry switches, field catalog |
| `POST /api/system/ai`, `POST /api/system/central` | admin | `{"available": bool}` demo switches |
| `POST /api/demo/reset` | admin | wipe and reseed the demo (accounts are kept) |
| `GET/POST /api/users` | admin | list / create accounts (`username`, `password` ≥ 10 chars, `role`) |
| `GET /api/senders`, `GET /api/facilities` | any | registered WhatsApp numbers and facilities |
| `POST /api/senders` | admin | `{"sender_id": "whatsapp:+212…", "label", "facility_id" or "facility_name"}` |
| `GET /api/media` | any | dataset images (`data/Paper Registry/`) |
| `POST /api/media` | any | raw JPEG/PNG body (≤ 12 MB) → `{"media_ref": "upload/<sha256>.jpg"}`, stored encrypted |
| `POST /api/whatsapp/messages` | any | simulated inbound photo `{"sender_id", "message_id", "media_ref", "captured_at"?}` |
| `GET /api/whatsapp/thread?sender_id=` | any | simulated thread, with `delivery_status` of outbound messages |
| `GET /api/documents`, `GET /api/documents/{id}` | any | queue, and one document with draft, review, events |
| `POST /api/documents/{id}/fields` | any | `CONFIRM` / `CORRECT` (`value`) / `SET_STATUS` (`field_status`), with `expected_revision` |
| `POST /api/documents/{id}/patient` | any | `EXISTING` (`patient_id`) / `NEW` / `UNSURE` |
| `POST /api/documents/{id}/confirm` | any | register; `existing_visit_decisions` for visits that differ |
| `POST /api/documents/{id}/manual-entry` | any | after a failed extraction, or while the AI is down |
| `POST /api/documents/{id}/encounters` | any | add a visit to a manual draft |
| `POST /api/pages/{id}/move` | any | split (no target) or regroup (`target_document_id`) |
| `POST /api/pages/{id}/retake` | any | ask the midwife for a new photo (`reason` optional) |
| `GET /api/patients`, `GET /api/patients/{id}/timeline` | any | registry and timeline |
| `GET /api/export/visits.json`, `GET /export/visits.csv` | admin | anonymized visits (internal IDs and values only) |
| `GET /media/{media_ref}` | any | page image (uploads decrypted on the fly) |
| `GET/POST /webhook/whatsapp` | Meta signature | WhatsApp Cloud API webhook (only when `WHATSAPP_APP_SECRET` is set) |

`captured_at` is the phone's capture time (ISO with time zone, at most 30 days old, not in the future);
offline captures keep it next to the server's `received_at`.

## Flow

1. Send the pages of one booklet with `POST /api/whatsapp/messages` within the grouping window (default 8 s),
   or upload camera photos first with `POST /api/media`. A replayed `message_id` returns the same page
   (`"duplicate": true`) and no second acknowledgment.
2. Poll `GET /api/documents/{id}` until `next_step` is `FIELD`, `PATIENT`, `EXISTING_VISITS` or `CONFIRM`
   (`WAITING_AI`, `WAITING_RETAKE` and `FAILED` mean wait, wait for the new photo, or start manual entry).
3. Answer every item of `review.blocking`, select the patient, then confirm. Nothing is registered before
   the explicit confirmation.
4. After registration the worker delivers the anonymized visits to the central registry: status
   `SYNCED`, or `SYNC_FAILED` with automatic retries (`document.sync_attempts`, `sync_next_attempt_at`).

## What the Tesseract extractor returns

Only `data/Paper Registry/dossiers_specimen_*` pages are read; anything else fails with
`NOT_A_SPECIMEN_PAGE` (camera photos go to manual entry). See `docs/schema.md` for the field object and the
flags, and the README for measured accuracy on the 10 specimen patients.

| Field | Source | Notes |
|---|---|---|
| `registry_file_number`, `facility_name` | page 1, beside the printed label | handwriting: mostly review |
| `last_menstrual_period` | page 3, *DDR* | `KNOWN` only when the visits' dates and ages confirm it |
| `midwife_patient_code` | not on the specimen layout | always `NOT_PROVIDED` (`NOT_ON_THIS_LAYOUT`) |
| all 8 visit fields | page 3 grid, one encounter per written *Venue le* | blank / dash cells are `NOT_PROVIDED` |

Examples: [patient 1, pages 01–03](api-examples/specimen-booklet-response.json) and
[a camera photo that is not a specimen page](api-examples/camera-photo-failed-response.json), both real
`GET /api/documents/{id}` responses from the Tesseract extractor (IDs, times and confidences vary).

## External extractor contract (`--extractor http`)

`POST $DAYONE_EXTRACTOR_URL` with `Authorization: Bearer $DAYONE_EXTRACTOR_TOKEN` and
`{"pages": [{"page_ref", "image_base64"}]}`. Answer `200` with a draft (`docs/schema.md`) whose `pages`
match the request, or `422` to refuse the pages. Drafts that break the schema fail as
`INVALID_EXTRACTION`; network errors and other statuses leave the document queued and retried. Only send
pages to a service where the data may legally go.

## Central registry contract

With `DAYONE_CENTRAL_URL`, the worker posts each registered document as JSON with headers
`Idempotency-Key: <document_id>:<registered_at>` and `X-DayOne-Signature: sha256=<HMAC-SHA256 of the body
with DAYONE_CENTRAL_SECRET>`. Answer `200` with a receipt (any JSON object); anything else is retried.
Payload: `document_id`, `facility_id`, `patient_id`, `registered_at`, and `visits[]` with `visit_id`,
`visit_date`, `slot`, and per field `value`, `field_status`, `verification`. No link keys, no raw OCR text.
Without the URL, an encrypted local file (`var/central/registry.log`) stands in.

## Errors

```json
{"error": {"code": "MEDIA_NOT_FOUND", "message": "Image introuvable.", "details": null}}
```

400 malformed JSON · 401 not signed in / bad credentials · 403 role, CSRF header or unknown sender ·
404 missing resource · 409 stale revision or invalid transition · 413 body too large · 422 invalid input ·
429 locked account · 500 `INTERNAL`.

Extraction is asynchronous: ingest can succeed and extraction fail later (`document.status:
"PROCESSING_FAILED"`, `failure_reason` such as `NOT_A_SPECIMEN_PAGE`, `SEVERAL_BOOKLETS`, `NO_VISIT_FOUND`,
`OCR_DEPENDENCY_MISSING`, `INVALID_EXTRACTION`). An unusable photo (`RETAKE_REQUIRED`) sends a retake request
automatically. Unexpected extractor exceptions leave the document queued for retry.
