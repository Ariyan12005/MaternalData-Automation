# MaternalData-Automation — DayOne (CodeML)

Platform that turns **photos of paper maternal registries** into structured digital records, **tracks each woman across visits**, and routes uncertain extractions to **back-office review**.

**Midwives only send documents** on WhatsApp; they do not manage patients or verify fields in chat. In the current MVP, WhatsApp is **simulated** in the browser by default. An optional WhatsApp Cloud API mode exists, but it has only been tested against a mocked Meta API so far. Extraction uses **fixtures** by default; an optional local OCR mode reads the synthetic specimen layout (see [Optional: local OCR](#optional-local-ocr-synthetic-specimen-layout) and [Simulator vs. real integration](#simulator-vs-real-integration)).

**Challenge:** *Une sage-femme, un téléphone et une IA* / *The Offline Midwife* (Challenge ID **17**).

## Operating model

| Role | What they do |
|------|----------------|
| Midwife | Send registry photo(s) → receive short ack |
| Platform | Extract, link visits, maintain patient timeline |
| Back-office | Confirm/correct fields & patient matches when AI is unsure |

Details: [docs/operating-model.md](./docs/operating-model.md) (fiche photos only—no ID cards)

## Team

| Person | Area |
|--------|------|
| **Fatma** | AI / extraction (+ linking fields from paper) |
| **Aymane** | WhatsApp ingest + back-office review UI |
| **Ariyan** | Backend, patient registry, sync, privacy |

**Work plan:** [tasks.md](./tasks.md)

## Key documents

| File | Purpose |
|------|---------|
| [consignes-fr-en.pdf](./consignes-fr-en.pdf) | Official instructions & grading (FR + EN) |
| [Extra Info CodeML Hackathon.docx](./Extra%20Info%20CodeML%20Hackathon.docx) | Architecture notes, V1 scope |
| [manifest.json](./manifest.json) | Dataset file list and checksums |

## Data (read-only)

- `data/Paper Registry/` — registry photos and specimen PDF  
- `data/maternal_registry_synthetic.csv` / `.xlsx` — 200-row synthetic reference table  

Do not modify original images or ground-truth files per consignes.

## Run the MVP (fixture-driven)

Python 3.10+ standard library only, with no dependencies to install.

```powershell
python -m dayone                    # http://127.0.0.1:8000
python -m dayone --port 8765 --window 8 --db var/dayone.sqlite3
python -m unittest discover -s tests -v
```

The database is created in `var/` and seeded with one patient (`PAT-000001`, 3 first-trimester visits). Use **Réinitialiser la démo** to start over.

### Optional: real WhatsApp (Cloud API)

```powershell
copy .env.example .env              # then fill in the placeholders (never commit .env)
python -m dayone.whatsapp --env-file .env check-config
python -m dayone.whatsapp --env-file .env add-sender --phone +2126XXXXXXXX --label "Sage-femme"
python -m dayone --env-file .env --whatsapp-mode cloud
python -m dayone --env-file .env --whatsapp-mode cloud --hold-outbound   # receive only: nothing is ever sent
```

The back-office stays on `127.0.0.1:8000`. Meta's webhook is served by a separate listener on `127.0.0.1:8001` that answers only `/webhooks/whatsapp`. Expose **only port 8001** through an HTTPS tunnel; never expose port 8000. Meta app setup, credentials, exposure strategy, privacy cautions and the sandbox checklist: [docs/whatsapp-cloud.md](./docs/whatsapp-cloud.md).

### Optional: local OCR (synthetic specimen layout)

Reads photos of the synthetic specimen pages (`dossiers_specimen_10_patientes-NN.png`) with PaddleOCR on the CPU, in a separate process. No page leaves the machine. It needs Python 3.12 in the project `.venv` (PaddlePaddle has no wheel for Python 3.14) and about 0.9 GB of disk (0.7 GB packages, 0.15 GB models). Details: [docs/ocr.md](./docs/ocr.md).

```powershell
$env:UV_LINK_MODE = "copy"                                       # OneDrive rejects uv's hardlinks
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-ocr.txt
.venv\Scripts\python -m dayone.ocr_engines download              # models, about 133 MB (the only network step)
.venv\Scripts\python -m dayone.ocr_engines check
.venv\Scripts\python -m dayone --extractor paddle --db var/dayone-ocr.sqlite3 --port 8020
```

Use a separate database so the fixture demo is not mixed with OCR drafts. Limits: `DAYONE_OCR_TIMEOUT_SECONDS` per page (default 120), `DAYONE_OCR_DOCUMENT_TIMEOUT_SECONDS` per document (600).

**Supported layouts.** Only the specimen: the cover (file number and facility, always confirmed by a person), the *Grossesse actuelle* visit grid (one encounter per visit column, with lab rows and the DDR), and as optional extended fields its identification, delivery and newborn consultation pages. Post-partum mother pages are recognised but not read. The pink booklet (`1-x.jpg`) and any other layout are not supported.

**Measured results** (80 specimen pages with a checked ground truth; patients 1–5 for development, 6–10 held out; full tables in [docs/ocr-evaluation.md](./docs/ocr-evaluation.md)):

| Condition | Correct KNOWN / written values | Wrong KNOWN | Blank filled in | Visits right | Median s / page |
|---|---|---|---|---|---|
| Clean renders, development | 255/355 | 0 | 0/110 | 30/30 columns | 12.8 |
| Clean renders, held-out | 209/312 | 0 | 0/92 | 25/25 columns | 11.0 |
| Simulated photos (3 conditions) | 474/979 | 8 | 0/294 | 80/80 columns | 6.4–9.3 |

**Limitations.** No real phone photo has been tested. On simulated photos 8 wrong values were marked KNOWN (0.8 %), with scores of 0.97 or more; the final confirmation records every unopened KNOWN value as confirmed, so the reviewer must check the summary against the page. Scores are not calibrated. About a third of the values on clean pages, and more than half on noisy ones, still need review. The held-out clean pages had been seen while tuning thresholds. Pages are read one at a time (6–22 s each).

### Screen layout

The back-office workspace is the main screen (French): review queue on the left, the open document in the centre, and the patient timeline below. The **Prochaine action** panel at the top of the document always says what to do next. It shows the current question, the patient choice or the confirmation summary. Drafts are labelled *Brouillon — non enregistré*; saved data is labelled *Enregistré*. Status is always written as text (for example *À vérifier*), not only shown by colour. A value's engine score is shown as *Score de lecture … (indicatif)*: it is not a calibrated probability.

The **simulated midwife phone** sits on the right (below the workspace on narrower screens) in a dashed frame labelled *Simulateur*. The *Extraction disponible (simulé)* toggle and *Réinitialiser la démo* are grouped under *Contrôles de démonstration*.

Keyboard: queue entries, field rows and actions are reachable with Tab and activated with Enter or Space. Visit tabs move with ← / → / Home / End. Escape closes a correction form, the open field or a simulator reply; the page preview is a dialog that returns focus where it was opened.

### Demo script

The presenter version, with the manual-entry path and the separately labelled hybrid media test (real Meta media API, simulated webhook), is in [docs/demo-script.md](./docs/demo-script.md). None of it needs Meta business verification.

1. **Midwife (simulated phone on the right):** select `1-1.jpg` then `1-5.jpg`, then press **Envoyer (2)**. Each page gets an acknowledgment, and the document appears in the queue as a provisional group.
2. Wait about 8 s (the grouping window). The fixture extractor produces a draft with 3 visits and **2 uncertain fields**.
3. **Back-office:** answer the questions in **Prochaine action**:
   - Hauteur utérine (2e trimestre – visite 1) is illegible ("2? cm"). Click **Saisir la valeur** and enter `25`, or **Confirmer « illisible »**.
   - Date of the 9th-month visit was read as "1?/12/25" (reading score 42 %, indicative only). Click **Corriger** and enter `19/12/2025`, which matches the 40 SA + 1 j gestational age.
4. **Patient:** `PAT-000001` is *suggested* because both keys match. Click **Choisir la patiente 1 : PAT-000001**. Nothing is saved yet.
5. **Confirmer et enregistrer:** 3 visits are created, and the timeline shows 6 visits, with the new ones marked *dossier ouvert*.
6. Optional checks:
   - **Rejouer le dernier webhook** is ignored as a duplicate.
   - Untick **Extraction disponible (simulé)** and send photos: they wait in `PENDING_AI` until it is ticked again.
   - **Déplacer… → nouveau dossier** splits a page into its own document.

### Retake demo

Run this before step 5 above, or on any document that is not yet registered.

1. Under **Pages reçues**, click **Demander une reprise** on page 2 and accept the confirmation. The simulated thread receives « Merci de reprendre la photo de la page 2. ». The document shows *Attendre la nouvelle photo (page 2)*, and confirmation is blocked. **Demandes de reprise** lists the request as *En attente de la photo*.
2. In the simulator, click **Répondre avec une nouvelle photo** under that message, pick `1-5.jpg`, and press **Envoyer la nouvelle photo**. The fixture set is unchanged, so this simulates a clearer photo of the same page.
3. The original photo is kept (shown in **Demandes de reprise** as *Photo d'origine (conservée)*). Earlier answers and the patient choice are discarded, and extraction runs again, so the 2 uncertain fields come back. The history records each step.
4. **Rejouer le dernier webhook** is ignored as a duplicate. Asking again for a page that already has a pending request sends no second message. **Annuler la reprise** closes a request and tells the midwife there is nothing to resend.

### Code map

| Path | Owner | Content |
|------|-------|---------|
| `docs/schema.md`, `docs/identity-strategy.md` | all | Contracts (fields, statuses, IDs, linking, visit identity, duplicates) |
| `fixtures/*.json`, `dayone/extraction.py`, `dayone/schema.py` | Fatma | Fixtures, extractor interface, draft validator |
| `dayone/ocr_engines.py`, `dayone/live_ocr.py`, `dayone/ocr_extended.py`, `dayone/extended.py`, `requirements-ocr*.txt`, `docs/ocr.md` | Aymane (started from Fatma's `origin/pinar` extractor) | Local OCR engines (PaddleOCR primary, Tesseract optional), the parser that turns OCR boxes into a v1.0 draft, and the optional extended fields |
| `dayone/ocr_process.py`, `dayone/ocr_preprocess.py`, `dayone/extraction_pool.py`, `dayone/live_ocr_adapter.py` | Aymane | OCR in a separate process with a per-page timeout, image preparation, extraction off the WhatsApp worker loop, and the adapter for `--extractor paddle`/`tesseract` (media paths, error mapping, draft checks) |
| `eval/specimen-ground-truth.json`, `tools/ocr_ground_truth.py`, `tools/ocr_evaluate.py`, `docs/ocr-evaluation.md` | Aymane (for Fatma's review) | Checked ground truth of the 80 specimen pages, development/held-out evaluation, results |
| `tests/test_live_ocr*.py`, `tests/test_ocr_*.py`, `tests/test_extraction_pool.py`, `tests/test_extended.py`, `tests/test_real_ocr.py`, `tools/ocr_specimen_eval.py` | Aymane | Mocked OCR tests; opt-in real-OCR test (`DAYONE_REAL_OCR=1`); the earlier text-layer evaluation |
| `dayone/export_columns.py`, `docs/excel-field-mapping.md` | all | The 31 CSV columns and what blocks each (nothing is exported yet) |
| `dayone/extraction_http.py`, `docs/extractor-contract.md`, `tests/test_extraction_http.py` | Fatma + Aymane | HTTP extractor adapter, prepared but unused; extraction boundary, contract and open questions |
| `dayone/store.py`, `dayone/service.py`, `dayone/linking.py` | Ariyan | Storage, lifecycle, review/confirm/timeline operations |
| `dayone/server.py`, `dayone/static/` | Aymane | HTTP API, simulated WhatsApp, webhook-only listener, back-office screen, Day1 logo (`logo-day1.jpg`) |
| `dayone/whatsapp.py`, `.env.example`, `docs/whatsapp-cloud.md`, `tools/webhook_relay.py`, `tools/hybrid_media_test.py` | Aymane (storage parts: Ariyan) | Cloud API adapter: webhook verification and parsing, media download, durable inbound/outbound jobs, outbound hold, configuration CLI, sandbox tools |
| `tests/` | all | Schema contract, end-to-end flow (idempotency, restart, grouping), retake workflow (`test_retake.py`), Cloud API adapter against a mocked Meta (`test_whatsapp_cloud.py`) |

Retake API: `POST /api/pages/{page_id}/retake` (idempotent while pending), `POST /api/retakes/{request_id}/cancel`, and an optional `retake_request_id` in the `/api/whatsapp/messages` body. Contract: [docs/schema.md](./docs/schema.md#retake-requests).

- **Verification is done by back-office staff, not the midwife.** The official instructions say "verified by the midwife". This is a deliberate deviation based on the organizers' verbal guidance, still to be confirmed in writing (see [docs/operating-model.md](./docs/operating-model.md)).
- **Retake photo** is implemented and tested locally; real WhatsApp reply context remains unverified.
- **Offline:** phone-side buffering relies on WhatsApp's own outbox. Our durable queue is the platform database. There is no on-device encrypted storage and fixture mode is unencrypted; secure Atlas and optional encrypted bridge capture are described below.
- The extractor is a fixture lookup by page set (`1-1.jpg` + `1-4.jpg` or `1-1.jpg` + `1-5.jpg`). Other page sets fail and go to manual entry. Fixture values are illustrative and not ground truth.
- Manual entry covers a single encounter. Fixture mode has no authentication; secure mode requires a shared backend credential, while the reviewer name remains an audit label. `SYNCED` is not implemented.


## Part 3: Atlas backend and encrypted offline bridge

[Backend architecture, integration endpoints and limits](docs/backend-part3.md).
The original fixture demo stays available. The app reads process environment
variables only (`.env.example` is a template, not auto-loaded).

### Windows PowerShell — fixture demo (SQLite, unencrypted, mock extractor)

```powershell
Set-Location C:\Users\ariya\MaternalData-Automation
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m dayone --host 127.0.0.1 --port 8000
```

### Windows PowerShell — Atlas + encrypted bridge

Do **not** generate a new `DAYONE_ENCRYPTION_KEY` if a database already exists;
paste the saved key instead. Allow this workstation's IP in Atlas Network Access.
Create a database user limited to the DayOne database. Set User/Password/Cluster
in the SRV URI; never commit it.

```powershell
Set-Location C:\Users\ariya\MaternalData-Automation
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:DAYONE_STORAGE = "mongodb"
$env:DAYONE_MONGODB_URI = "mongodb+srv://USER:PASSWORD@CLUSTER/?retryWrites=true&w=majority"
$env:DAYONE_MONGODB_DATABASE = "dayone"
# First database only. If a key already exists, paste it here instead of this line.
$env:DAYONE_ENCRYPTION_KEY = (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").Trim()
$env:DAYONE_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$env:DAYONE_EXTRACTOR = "external"

@("DAYONE_STORAGE","DAYONE_MONGODB_URI","DAYONE_MONGODB_DATABASE","DAYONE_ENCRYPTION_KEY","DAYONE_API_TOKEN","DAYONE_EXTRACTOR") | ForEach-Object {
  "{0} set={1} length={2}" -f $_, [bool][Environment]::GetEnvironmentVariable($_, "Process"), ([Environment]::GetEnvironmentVariable($_, "Process") + "").Length
}

python -m dayone --host 127.0.0.1 --port 8000
```

Browser login: username `dayone`, password = API token.
`DAYONE_EXTRACTOR=external` is the real Fatma contract (claim/result + leased originals).
Leave it unset only for the fixture extractor (mock; looks up `1-1.jpg`+`1-4.jpg` / `1-5.jpg`).
`POST /api/whatsapp/uploads` is Aymane's authenticated **bridge** (not Meta's webhook envelope).
The left-phone simulator (`/api/whatsapp/messages` + `dayone/static/`) is a mock.

Atlas does not seed senders. In a **second** PowerShell window, activate the same venv
and set the **same** token and encryption key (do not generate a second key):

```powershell
Set-Location C:\Users\ariya\MaternalData-Automation
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
$env:DAYONE_API_TOKEN = "<paste-the-same-token>"
$env:DAYONE_ENCRYPTION_KEY = "<paste-the-same-saved-key>"
$headers = @{ Authorization = "Bearer $env:DAYONE_API_TOKEN"; "X-Reviewer" = "ariyan" }
$body = @{ facility_id = "FAC-TEST"; facility_name = "Demo facility"; sender_id = "whatsapp:+212600000001"; label = "Demo sender" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/admin/senders -Method Post -Headers $headers -ContentType application/json -Body $body
python -m dayone.offline capture "data/Paper Registry/1-1.jpg" --sender "whatsapp:+212600000001" --message-id "wamid.edge-1" --group "scan-1"
python -m dayone.offline capture "data/Paper Registry/1-5.jpg" --sender "whatsapp:+212600000001" --message-id "wamid.edge-2" --group "scan-1"
python -m dayone.offline sync --url http://127.0.0.1:8000
```

Live Atlas checks must use a **separate test database**, never `dayone` production data.
`LiveAtlasTest` creates and drops only `dayone_test_<uuid>`:

```powershell
$env:DAYONE_TEST_MONGODB_URI = $env:DAYONE_MONGODB_URI
python -m unittest tests.test_mongo_store.LiveAtlasTest -v
python -m unittest discover -s tests -v
```

The Atlas user must be allowed to create/drop those `dayone_test_*` databases (or use a
separate test cluster). Unit tests without `DAYONE_TEST_MONGODB_URI` use an in-memory
Mongo double and do **not** prove Atlas connectivity.

Implemented: encrypted Atlas records/originals, grouped intake, durable encrypted
outbox, leased extraction handoff, retry deduplication, stale edit/visit rejection,
selected-field updates in the review UI, corrections and timeline history.
The Atlas compatibility adapter runs existing SQL only in memory and reads all
records per request; this preserves the team code but is intended for MVP scale.

## Simple Atlas setup in Antigravity

Open Antigravity's PowerShell terminal in this folder and run:

```powershell
python -m pip install -r requirements.txt
python -m dayone.setup
python -m dayone
```

The wizard asks privately for your real Atlas connection string. It retains any
existing encryption key; if none is configured, paste the saved key or type NEW
only for an empty database. Configuration is saved in Git-ignored `.env` and loaded
at server/offline CLI startup. Explicit environment variables take precedence.
Back up this private file securely. Do not commit it or share its contents.
Browser login is `dayone`, with the `DAYONE_API_TOKEN` value in your local `.env`.
The wizard configures external extraction; Fatma's worker is still required.
Setup saves configuration; it does not prove live Atlas connectivity.
## Check Atlas and recover extraction failures

```powershell
python -m dayone.check
```

This read-only check loads your private configuration, checks Atlas connectivity,
database read permissions and sample encrypted payloads without printing secrets.
Missing settings are reported by name. Use `python -m dayone.setup` to configure
Atlas first. Full write/transaction verification still uses the dedicated test.

Back-office can now start manual entry while a closed scan waits in PENDING_AI,
even without Fatma's worker. This invalidates late extraction callbacks through
the document revision. The worker can report permanent failures through the
new authenticated `/api/extraction/jobs/<job_id>/failure` endpoint.
### Simulator vs. real integration

| Part | In the MVP (default) | Real integration (`--whatsapp-mode cloud`) |
|------|------------|------------------|
| WhatsApp inbound | Browser phone panel posts `{sender_id, message_id, media_ref}` to `/api/whatsapp/messages`. Images must already be in `data/Paper Registry/` | Implemented, mocked tests only: signed webhook, batched events, deduplication by WhatsApp message ID, authenticated and bounded media download, durable retries. Against Meta (unpublished app): webhook verification, a signed dashboard test webhook, and real media download with a simulated webhook. No real message received yet |
| WhatsApp outbound | Acknowledgments and retake requests stored in the database and shown in the simulated thread | Implemented, mocked tests only: durable send queue with retries and delivery statuses. No template messages, so nothing can be sent more than 24 h after the midwife's last message |
| Retake reply | The simulator sends `retake_request_id` explicitly with the replacement photo | Implemented, mocked tests only: a photo sent as a reply to the retake message replaces the page. Whether WhatsApp includes that reply context for images still needs sandbox verification |
| Senders | One seeded demo number mapped to *C/S Sidi Smail* | `python -m dayone.whatsapp add-sender`. Messages from unregistered numbers are rejected without downloading the photo |
| Extraction | `FixtureExtractor` returns a hand-written draft for `1-1.jpg` + `1-4.jpg` or `1-1.jpg` + `1-5.jpg`; any other page set fails and goes to manual entry. Values are illustrative, not ground truth | `--extractor paddle` runs local PaddleOCR from the project `.venv` in a separate process with a per-page timeout ([docs/ocr.md](./docs/ocr.md)). It reads the specimen layout: the pregnancy visit grid (one encounter per visit column), the cover keys, and optional extended fields. Evaluated on synthetic specimen renders and simulated photos only, not on real phone photos ([docs/ocr-evaluation.md](./docs/ocr-evaluation.md)). Uncertain values go to review; names are never extracted. `--extractor tesseract` is optional and untested here. `--extractor http` stays unused: [docs/extractor-contract.md](./docs/extractor-contract.md) |
| AI outage | "Extraction disponible (simulé)" toggle | With local OCR: a missing install keeps documents queued; a page over the timeout or an engine crash is marked unread (the OCR process is restarted), and a document with no readable page fails and goes to retake or manual entry |

### Open blockers and limitations

Tracked in [tasks.md](./tasks.md#blockers-and-open-gaps-keep-visible-until-resolved).

- **Organizer confirmation (not obtained).** Verification is done by back-office staff, not the midwife, but the official instructions say "verified by the midwife". This deviation rests on verbal guidance and still needs written confirmation. The same applies to using WhatsApp's outbox as the phone-side offline queue. See [docs/operating-model.md](./docs/operating-model.md).
- **SQLite demo privacy limits.** SQLite records remain unencrypted. Atlas records and originals are encrypted; configured encrypted media storage also receives downloaded WhatsApp images.
- **WhatsApp Cloud API only partly verified against Meta.** Webhook verification, signatures and media download work with a real Meta app. No real message has been received or sent: the app is unpublished (publishing needs business verification) and is not yet subscribed to the WhatsApp Business Account. Using it sends photos through Meta and the tunnel provider, so use specimen pages only until the organizers approve. See [docs/whatsapp-cloud.md](./docs/whatsapp-cloud.md#what-works-locally-and-what-still-needs-the-sandbox).
- **Shared authentication.** Atlas requires a shared staff/API credential; individual users and facility RBAC remain unimplemented. Demo reset is disabled for Atlas or any database with an outbound hold. Use HTTPS for remote staff access.
- **Offline gaps.** There is no on-device encrypted storage, and no demo of "offline capture, then return of connectivity". The demo shows the platform-side equivalent instead (AI outage, then recovery). `SYNCED` is not implemented.
- **Retake over real WhatsApp is unverified.** In cloud mode, the request is sent as a reply quoting the photo, and the midwife's reply is matched to the request. Both are tested only with mocks. A registered document cannot be retaken: new photos form a new document, which goes through the update/keep decision for existing visits.
- **Manual entry** covers a single encounter and is available for closed scans waiting for extraction or after extraction failure. Starting it while a read is running makes that read's result stale; it is discarded.
- **Local OCR is evaluated on synthetic pages only.** No real phone photo has been read. On simulated photos, 8 of 979 written values were wrong but marked KNOWN, and confirmation saves unopened KNOWN values as confirmed. Only the specimen layout is supported. See [docs/ocr-evaluation.md](./docs/ocr-evaluation.md).

Backend integration details, OCR media interfaces, verification and the remaining
transport work are recorded in [docs/backend-integration.md](docs/backend-integration.md).
