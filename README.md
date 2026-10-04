# MaternalData-Automation — DayOne (CodeML)

Platform that turns **photos of paper maternal registries** into structured digital records, **tracks each woman across visits**, and routes uncertain extractions to **back-office review**.

**Midwives only send documents** on WhatsApp; they do not manage patients or verify fields in chat.

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

### Demo script

1. **Midwife (left phone):** select `1-1.jpg` then `1-5.jpg`, then press **Envoyer**. Each page gets an acknowledgment, and the document appears in the queue as a provisional group.
2. Wait about 8 s (the grouping window). The fixture extractor produces a draft with 3 visits and **2 uncertain fields**.
3. **Back-office:** answer the questions:
   - Hauteur utérine (2e trimestre – visite 1) is illegible ("2? cm"). Click **Corriger** and enter `25`.
   - Date of the 9th-month visit was read as "1?/12/25" at 42 % confidence. Click **Corriger** and enter `19/12/2025`, which matches the 40 SA + 1 j gestational age.
4. **Patient:** `PAT-000001` is *suggested* because both keys match. Click it to select. Nothing is saved yet.
5. **CONFIRMER l'enregistrement:** 3 visits are created, and the timeline shows 6 visits with the new ones highlighted.
6. Optional checks:
   - **Rejouer le dernier webhook** is ignored as a duplicate.
   - Untick **IA disponible** and send photos: they wait in `PENDING_AI` until it is ticked again.
   - **Déplacer… → nouveau dossier** splits a page into its own document.

### Code map

| Path | Owner | Content |
|------|-------|---------|
| `docs/schema.md`, `docs/identity-strategy.md` | all | Contracts (fields, statuses, IDs, linking, visit identity, duplicates) |
| `fixtures/*.json`, `dayone/extraction.py`, `dayone/schema.py` | Fatma | Fixtures, extractor interface, draft validator |
| `dayone/store.py`, `dayone/service.py`, `dayone/linking.py` | Ariyan | Storage, lifecycle, review/confirm/timeline operations |
| `dayone/server.py`, `dayone/static/` | Aymane | HTTP API, simulated WhatsApp, back-office screen |
| `tests/` | all | Schema contract + end-to-end flow (idempotency, restart, grouping) |

### Known limitations

- **Verification is done by back-office staff, not the midwife.** The official instructions say "verified by the midwife". This is a deliberate deviation based on the organizers' verbal guidance, still to be confirmed in writing (see [docs/operating-model.md](./docs/operating-model.md)).
- **Retake photo** is not implemented.
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