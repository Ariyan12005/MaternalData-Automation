# Ariyan Part 3 backend

## Architecture and setup

The current team repository remains Python's standard-library HTTP server with
`DayOneService`, the strict `docs/schema.md` draft contract, and the existing review
UI. No FastAPI prototype was copied. `DAYONE_STORAGE=mongodb` selects Atlas;
`sqlite` remains the unencrypted local fixture demo. A storage typo fails startup.
Configuration comes from environment variables; `.env.example` is a template,
and the CLI now loads private .env at startup, without overriding environment settings. No private `.env` or Atlas URI was available.
Set `DAYONE_MONGODB_URI` to your Atlas SRV connection string. Install dependencies
and permit your workstation's IP in Atlas network access; use a database user
limited to the DayOne database. See [PyMongo transaction documentation](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/transactions/).

Atlas is the durable authority. Encrypted collections store patients, visits,
documents, pages, extraction jobs, sender enrollments, receipts, audit events,
settings and originals (`media_blobs`). Original JPEG/PNG uploads are capped at
8 MiB to keep encrypted bytes below MongoDB's 16 MiB BSON document limit.
Authenticated Fernet encryption protects payloads; HMAC indexes preserve unique
message IDs, facility paper codes and visit identity without plaintext keys.
Never regenerate a key for an existing database; retain secure key backups.

The Atlas adapter rebuilds the existing SQL model **in RAM per transaction** to
preserve teammates' SQL domain logic and tests. No patient SQLite file is written
in Atlas mode. Snapshot transactions, majority commit and a global generation CAS
protect atomic multi-record registration across processes. This is an MVP adapter:
every request reads all rows and writes are serialized globally. Larger deployments
need native Mongo queries and bounded history; individual encrypted rows must fit
BSON limits. Do not write these collections outside the adapter. No automatic
SQLite-to-Atlas migration occurs, and Atlas never seeds or clears demo patients.

## Privacy and access

Atlas mode requires `DAYONE_API_TOKEN` and `DAYONE_ENCRYPTION_KEY`. All HTTP routes,
including UI, media and bridge endpoints, require authentication. Integrations use
`Authorization: Bearer TOKEN`; the browser accepts Basic login with username
`dayone` and password equal to the token. `X-Reviewer` retains the existing audit
identifier. This is one shared staff/bridge credential, not per-user authentication
or facility RBAC; use only a trusted hackathon team environment. HTTPS via a reverse
proxy is required for remote access; default binding stays on loopback.

Original photos may contain PII and remain encrypted restricted originals.
Structured fields use the existing allowlist: no patient names, addresses, ID card
fields or clinical text commands are accepted. Fatma must still redact free-text
metadata/raw text where necessary; allowlisting and encryption do not themselves
prove arbitrary model text contains no PII. Exception traces are suppressed in
service/HTTP worker logs to avoid printing extraction values or connection strings.
No external OCR service is invoked by this implementation. Failed captures can
leave encrypted unreferenced originals; retention cleanup is not automated.
Organizer datasets and manifest are unchanged.

## Aymane ingest contract

`POST /api/whatsapp/uploads` (alias `/webhooks/whatsapp`) accepts bridge JSON:

```json
{"sender_id":"whatsapp:+212600000001","message_id":"wamid.unique","image_base64":"BASE64_JPEG","suffix":".jpg","group_id":"scan-session-1"}
```

The optional group ID is scoped to sender. Explicit groups stay open until
`POST /api/documents/DOC-000001/close`; this supports slow reconnect batches.
Without a group, the existing eight-second window applies. Group IDs cannot be
reused for new pages after closure. Replaying a message ID returns its original
page/document and no second receipt. Image and page persistence precede the
receipt; Aymane sends the returned `acknowledgment` through WhatsApp.

This is an authenticated **bridge contract**, not Meta's public webhook envelope.
Actual Meta signature verification, downloading media and sending receipts remain
Aymane's transport integration. This backend never sends messages to a midwife.
The existing `/api/whatsapp/messages` media-ref simulator is retained.

Provision allowed senders via `POST /api/admin/senders`:

```json
{"facility_id":"FAC-TEST","facility_name":"Demo facility","sender_id":"whatsapp:+212600000001","label":"Demo sender"}
```

A sender cannot silently move facilities. Patient keys and matching remain scoped
to the enrolled facility.

## Fatma extraction contract

`DAYONE_EXTRACTOR=external` disables fixture extraction. After capture closure,
`POST /api/extraction/jobs/claim` with `{}` returns null or a job with `job_id`,
`document_id`, `token`, `expected_revision`, `page_refs` and a five-minute lease.
Retrieve ordered original bytes using authenticated `GET /media/<page_ref>`.
Post results to `/api/extraction/jobs/JOB-000001/result`:

```json
{"token":"LEASE_TOKEN","expected_revision":0,"draft":{"schema_version":"1.0","...":"complete shared draft"}}
```

The full draft must follow `docs/schema.md`: statuses, confidence, raw text,
normalized values, units, flags, provenance, extractor version and both paper
codes. Page membership is checked; extractor-supplied human verification or
corrections are rejected. Expired/replaced leases and changed document revisions
are rejected. Identical completed callbacks replay without modifying staff edits.
Crashed jobs can be reclaimed after five minutes. Fixture extraction uses these
same leases. External extraction errors can retry by lease expiry; permanent
failure reporting is not yet a separate external callback.

Patient auto-linking remains **suggestion only** per the team's accepted workflow:
both codes must be present, KNOWN, confidence >= 0.75, match both stored keys in one
facility, and identify one unambiguous candidate. Staff must select the patient
and explicitly CONFIRMER; new patients are never created automatically.

## Staff visits and history

Existing queue, field correction, patient choice and timeline APIs remain.
HTTP field edits, patient selection, confirmation, manual entry and page moves require integer
`expected_revision`; stale documents return 409. Regrouping also requires the target
`target_expected_revision`. Correcting a paper key clears the prior patient selection. Existing differing visits require
KEEP or a versioned selected-field update. The UI includes checkboxes for differences:

```json
{"expected_revision":4,"existing_visit_decisions":{"1":{"action":"UPDATE","fields":["weight_kg"],"expected_version":"VERSION_FROM_ENCOUNTER_MATCH"}}}
```

`existing_version` comes from the document's `review.encounter_matches`; a changed
visit returns `STALE_VISIT`. The HTTP API refuses legacy unversioned UPDATE strings;
the Python service retains that form for existing internal tests/callers. Only
selected differing fields change; others remain. Visit history keeps previous field
objects, staff/time and source document. Timelines expose `history` and full
`field_details` alongside the existing compact `fields`. Draft correction histories
are preserved. Final confirmation explicitly approves new encounters; the same
patient/type/date cannot be forced into a duplicate visit. Confirmation retries
return the stored registration without adding visits.

## Offline capture and synchronization

`python -m dayone.offline capture` is an optional encrypted bridge/simulator outbox,
not a new midwife app. It persists image bytes plus sender, stable message ID and
group metadata while the backend/Atlas is unavailable. `sync` retries uploads,
removes items only after persisted-page acknowledgment, then closes grouped scans.
Closure acknowledgments are queued durably too, so crashes or lost responses after
uploads or closure can retry safely. Encryption keys remain external to SQLite.
An edge can use a different key from the backend. WhatsApp's own phone outbox is
still outside our control and its device-side encryption is not demonstrated.

## Verification

### Aymane PR integration safeguards

Atlas hydration temporarily disables SQL triggers while restoring encrypted rows,
then reinstalls them before domain operations. Restoring a stored outbound message
must not recreate a pending receipt or overwrite an existing delivery state.
Additive columns with SQL defaults can read older encrypted records without
rewriting them during reads; subsequent writes persist the expanded rows using
the existing encryption key. No database reset or key rotation is required.

The published WhatsApp transport, retake tables and persistent outbound hold from
Aymane's branch are integrated. Before running multiple transport workers, add atomic
inbound/outbound job claims with lease ownership, guard each outbound claim with
the persistent hold, disable cloud demo reset, and prevent delayed failure
callbacks from retrying delivered/read messages. A send with an uncertain network
outcome needs an explicit recovery policy; leases alone cannot guarantee that
Meta accepted a message only once. Retake replacement must invalidate the current
document revision and extraction lease while preserving original encrypted media.
This integration preserves the Atlas factory, authenticated encrypted media
endpoints and external claim/result/failure contract. Downloaded originals use
encrypted media storage when configured. See [integration handoff](backend-integration.md)
for tested behavior, the local OCR interface and remaining transport work.

Pregnancy-level entities and anonymized Excel export remain Ariyan's backend
scope; neither is implemented by the WhatsApp integration. Fatma owns extraction
and Aymane owns transport/review. Agree pregnancy identity, row granularity and
unknown-value encoding before implementing the proposed Excel field map.

On Windows PowerShell (venv activated):

```powershell
python -m unittest discover -s tests -v
```

Atlas adapter tests in `tests/test_mongo_store.py` use an in-memory Mongo test double
unless `DAYONE_TEST_MONGODB_URI` is set. That URI must point at a dedicated test
credential/cluster. `LiveAtlasTest` creates only `dayone_test_<uuid>`, writes grouped
uploads, encrypted originals, extraction results, patients, visits, corrections and
history, then drops that database. It never targets `DAYONE_MONGODB_DATABASE`. Skip
is expected when the URI is unset; that skip is not live Atlas proof.

```powershell
$env:DAYONE_TEST_MONGODB_URI = "mongodb+srv://USER:PASSWORD@CLUSTER/?retryWrites=true&w=majority"
python -m unittest tests.test_mongo_store.LiveAtlasTest -v
```

Real OCR, Meta transport, per-facility authorization, automated retention and
downstream SYNCED export remain outside this implementation. The HTTP simulator and
`FixtureExtractor` are mocks. Fatma's worker is real when `DAYONE_EXTRACTOR=external`
and it calls claim/media/result. Aymane's Meta Cloud API remains a separate transport
in front of `POST /api/whatsapp/uploads`.

## Extraction recovery update

Workers can POST `/api/extraction/jobs/JOB-000001/failure` with `token`, integer
`expected_revision`, and `code`: ILLEGIBLE_IMAGE, UNSUPPORTED_LAYOUT, or
EXTRACTION_FAILED. Arbitrary exception/clinical text is not stored. Active lease,
revision and document state are checked; failed callback retries are idempotent.
The record becomes PROCESSING_FAILED. Retryable outages should retain PENDING_AI
and retry the lease instead of reporting permanent failure.

Staff may explicitly start manual entry from PENDING_AI or PROCESSING_FAILED,
using the existing revision-protected API/UI. Late worker results or failures
cannot overwrite the manual draft. This works without an external model.

`python -m dayone.check` performs a read-only readiness check using local .env and
reports safe error categories; it never initializes collections or writes data.
It cannot validate write access or all stored ciphertext; use LiveAtlasTest for
full validation on a dedicated database.
