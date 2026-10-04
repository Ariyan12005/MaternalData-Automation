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

## Verification

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

## Remaining work and limits

- Aymane's unpublished local OCR must be brought in and wired to the media
  interface. Accuracy, multiple visits, renamed images, layout detection and
  timeouts remain OCR work; unknown layouts must fail visibly into review.
- Published transport workers select pending rows without atomic claims. Before
  multiple transport workers run, add owned inbound/outbound leases and define
  recovery for uncertain send outcomes. No exactly-once Meta delivery guarantee
  is claimed. Live outbound remains disabled during this handoff.
- Actual phone intake, real delivery and real retake reply context remain
  unverified. Existing WABA subscription/publishing blockers are unchanged.
- Atlas still hydrates all data per transaction and serializes writes globally.
  Shared credentials, no facility RBAC, no automatic SQLite migration/retention,
  and plain SQLite demo records remain limitations.
- Pregnancy entities and the 31-column Excel export require the agreed identity,
  aggregation and missing-value decisions; source assets remain read-only.
- Organizer approval of back-office verification is still pending.
