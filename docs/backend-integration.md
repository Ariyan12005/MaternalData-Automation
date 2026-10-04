# Backend integration — 2026-10-04

Work lives in the isolated `.backend-integration` checkout on
`backend/integration`. The original checkout remains on `ariyan-backend`.
WhatsApp/OCR development belongs to Aymane's other Codex session; coordinate
new commits before incorporating any of its unpublished files.

## Published inputs and integration plan

Fetched origin before editing. Backend: `ade03f5`; WhatsApp/review:
`1127cfe`; pinar: `1e4643c`; main: `e5da1e0`.
The WhatsApp branch contains the retake workflow, adapter, queue tables,
persistent outbound hold and redesigned UI/logo. Configurable local OCR is
absent from that branch. Pinar has a separate Tesseract demo, not a compatible
dedicated HTTP extraction endpoint. `docs/extractor-contract.md` is absent
from the integration inputs. No missing OCR implementation was recreated.

Plan: merge the published transport/review branch into the backend, retain
Atlas/authentication/leased extraction and versioned visit updates, combine
additive schema migrations, expose encrypted media to OCR, verify exclusively
with disposable SQLite databases and a mocked Mongo client, then prepare review.

## Resulting behavior

- Only `fixture` and `external` extractor modes are accepted. Unknown values
  fail before creating a database. Aymane must extend this factory when his
  local extractor is published; do not silently fall back to fixtures.
- External-mode SQLite startup seeds demo enrollment without running fixture
  registration. Startup never calls database reset. Existing documents and
  persistent holds survive restarting and checking seed state.
- Atlas persists transport queues, retake requests, sender channels, page
  replacement provenance, capture groups and extraction jobs together.
  Hydration suppresses triggers and restores them for new actions.
  Older encrypted sender payloads obtain the default channel on hydration;
  read-only hydration does not rewrite ciphertext.
- Retakes preserve originals, advance document revisions and reject results
  and failures from prior extraction leases, including same-ref replacements.
- Staff UI retains Aymane's design and logo, sends document/target revisions
  for manual entry and regrouping, and uses selected fields and visit versions
  for updates. Patient choice and final confirmation remain explicit.
- Downloaded WhatsApp media uses the configured encrypted media store when
  available, including Atlas. SQLite without a media key retains demo behavior.
- Cloud startup establishes a persistent outbound hold; the server worker
  never sends outbound messages. Existing hold startup refusal is preserved.
  No held messages were discarded or released. Reset is disabled for Atlas
  and held SQLite databases. Late failure statuses cannot regress READ or
  DELIVERED messages into retries.
- The authenticated sender enrollment API accepts optional `channel` of
  SIMULATOR or WHATSAPP. Omitting it preserves an existing channel.

## OCR handoff

Use `service.read_media(page_ref)` for original bytes from fixture files,
downloaded files or encrypted storage. For a path-based extractor:

```python
with service.extraction_media(page_refs) as paths_by_ref:
    # Pass paths to the local extractor; retain dictionary keys in provenance.
    # Complete extraction before leaving this context.
    ...
```

The temporary directory is removed on success, read failure or extraction
exception. This is filesystem cleanup, not a secure-erasure guarantee. Do not
retain those paths or spawn an asynchronous extractor that outlives the context.
Use the unchanged external claim/media/result/failure APIs for a separate worker.
The authenticated bridge and the public signature-verifying webhook remain
separate listeners; the bridge alias does not parse Meta envelopes.

## Part 1 Verification (Pre-OCR Baseline)

Run with `.venv/Scripts/python.exe -m unittest discover -s tests -q`.
110 tests ran: 109 passed, one live Atlas test skipped. The suite covers fixture
registration, duplicate messages and confirmation, versioned document/visit
updates, encrypted bridge recovery, leases, retakes and mocked Meta transport.
New combined tests cover external startup/restart, strict configuration,
actual outbound-trigger hydration/hold persistence, encrypted same-ref retake
lease rejection, temporary-file cleanup after exception, and late READ failure.
All manifest-listed assets are checked separately against their recorded hashes.

No local `.env` was loaded, working database opened, live Atlas invoked, real
WhatsApp message sent, Meta configuration changed, or deployment performed.
Node and GitHub CLI are unavailable: no JavaScript runtime/browser test or
remote PR creation is claimed. The PR title/body are prepared locally.

## Merged OCR & Backend Part 3 Capabilities

- **PaddleOCR Integration:** Configured `--extractor paddle` (primary) alongside `tesseract` and `fixture`. Model paths automatically detect `var/ocr-models` from parent repository or configured environment. OCR processes run isolated with bounded execution (page and document timeouts) using the `extraction_media(page_refs)` context manager, ensuring temporary files are pruned cleanly on success or failure without exposing unencrypted media.
- **Mandatory Revision Guarding:** Revisions are strictly verified across all state-mutating actions including `set_page_section`, manual entry transitions, field corrections, and visit confirmation. Stale leases and concurrent edits are rejected with explicit conflict codes.
- **Durable Offline Queue & SYNCED Lifecycle:** `dayone/offline.py` provides an explicit `item_status` table tracking `PENDING`, `SYNCED`, and `FAILED` states with recovery mechanisms for queue retries. `service.sync_document(document_id)` safely transitions registered documents to `SYNCED` with simulated central registry sink and idempotent replay.
- **Conversational Midwife Review Actions:** `service.conversational_prompt()` and `service.conversational_reply()` support step-by-step midwife conversational interactions (Confirm, Correct, Set Status, Retake, Manual Entry, Patient Choice, and Sync). Selective questions target uncertain/illegible fields, followed by explicit record confirmation.
- **Deterministic 31-Column Export:** `dayone/export_columns.py` and `service.export_patient()`, `service.export_all_patients()`, and `service.export_csv()` implement deterministic mapping to the 31 columns of `maternal_registry_synthetic.csv`. Aggregations (mean systolic/diastolic blood pressure, delivery type 0/1, newborn sex 0/1) are computed deterministically, while unmeasured or missing fields remain visibly `None` without data fabrication.

## Verification

Run with `.venv/Scripts/python.exe -m unittest discover -s tests -v`.
- **266 tests ran:** 263 passed, 3 skipped (1 live Atlas test, 2 opt-in real OCR tests).
- Opt-in real OCR test suite (`DAYONE_REAL_OCR=1`): 2 passed with PaddleOCR reading specimen visit grids and verifying timeouts/process isolation.
- Node syntax check: `node -c dayone/static/app.js` passed cleanly.
- Merge-marker check: `git diff --check` passed with 0 conflicts.
- All 132 manifest-listed assets match recorded SHA-256 hashes.

No local `.env` was loaded, working database opened, live Atlas invoked, real WhatsApp message sent, Meta configuration changed, or remote deployment performed.

## Remaining work and declared limits

- **Midwife Conversational Interface:** The backend conversational action handlers are complete and tested; a standalone midwife mobile chat frontend remains a simulated capability in the prototype UI.
- **WhatsApp Cloud API Sandbox:** Outbound holds remain strictly active; live delivery against Meta's sandbox requires an approved/published Meta app with WABA subscription.
- **OCR Real-World Calibration:** PaddleOCR has been validated on synthetic specimen renders and simulated scans (8 wrong KNOWN of 979 values); testing against real-world smartphone photos in varied clinic lighting remains open for future field pilots.
- **Atlas Scaling:** Atlas hydrates documents per transaction; full RBAC and multi-facility role partitioning remain recommended future enhancements.
