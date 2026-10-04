# MaternalData-Automation — DayOne (CodeML)

Platform that turns **photos of paper maternal registries** into structured digital records, **tracks each woman across visits**, and routes uncertain extractions to **back-office review**.

**Midwives only send documents** on WhatsApp; they do not manage patients or verify fields in chat. In the current MVP, WhatsApp is **simulated** in the browser and extraction uses **fixtures** (see [Simulator vs. real integration](#simulator-vs-real-integration)).

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

The database is created in `var/` and seeded with one patient (`PAT-000001`, the first 3 visits of specimen patient 1). Use **Réinitialiser la démo** to start over.

### Local OCR on the specimen pages (Tesseract)

Install Tesseract with French language data (macOS: `brew install tesseract tesseract-lang`) and Pillow
(`pip install -r requirements.txt`), then run:

```sh
python3 -m dayone --extractor tesseract --port 8001 --db var/dayone-tesseract.sqlite3
```

The OCR reads **only** the specimen pictures `data/Paper Registry/dossiers_specimen_*`. Any other image fails
extraction with `NOT_A_SPECIMEN_PAGE` and goes to manual entry. Send the eight pages of one booklet together
(patient 1 is pages 01–08, patient 2 is 09–16, and so on).

How it reads (`dayone/specimen_ocr.py`):

- Pages are recognised by their printed title, not their file name. The cover gives the file number and the
  facility, the "Grossesse actuelle" page gives the DDR and the visit grid. Pages from two booklets in one
  document fail with `SEVERAL_BOOKLETS` so the reviewer can split them.
- Every grid column with a visit date becomes a visit (slots `T1_V1` … `M9`). Rows are anchored on their printed
  labels; blank cells and dashes become `NOT_PROVIDED` from the pixels, before any OCR.
- Each value is read four times (page in French, page in English, cell crop with and without a character
  whitelist). It is `KNOWN` only if at least three readings agree and none disagrees, **and** it is consistent
  with the booklet: gestational age against DDR and visit date, visit dates in order, plausible weight change
  between visits, fundal height against a confirmed gestational age. Confidence is the share of agreeing readings.
- Everything else goes to review with the best candidate and a flag saying why. Checks only demote values; nothing
  is inferred from other fields.

Accuracy on the 10 specimen patients (`python3 eval/compare_ocr.py --markdown`, about 2.4 s per patient on a laptop):

| field | KNOWN, right | FALSE-KNOWN | review | review (blank cell) | empty, right | missed | right value suggested |
|---|---:|---:|---:|---:|---:|---:|---:|
| registry_file_number | 0 | 0 | 10 | 0 | 0 | 0 | 2 |
| midwife_patient_code | 0 | 0 | 0 | 0 | 10 | 0 | 0 |
| facility_name | 1 | 0 | 9 | 0 | 0 | 0 | 2 |
| last_menstrual_period | 5 | 0 | 5 | 0 | 0 | 0 | 0 |
| visit_date | 24 | 0 | 31 | 0 | 0 | 0 | 15 |
| gestational_age_days | 20 | 0 | 35 | 0 | 0 | 0 | 10 |
| weight_kg | 17 | 0 | 38 | 0 | 0 | 0 | 15 |
| systolic_bp_mmhg | 15 | 0 | 40 | 0 | 0 | 0 | 10 |
| diastolic_bp_mmhg | 15 | 0 | 40 | 0 | 0 | 0 | 14 |
| fundal_height_cm | 12 | 0 | 32 | 0 | 11 | 0 | 20 |
| syphilis_test | 7 | 0 | 3 | 3 | 42 | 0 | 2 |
| hiv_test | 4 | 0 | 6 | 8 | 37 | 0 | 6 |
| **total** | **120** | **0** | **249** | **11** | **100** | **0** | **96** |

Of 369 values written on the pages, 120 (33 %) come out `KNOWN` and right, none comes out `KNOWN` and wrong, and none
is missed; the reviewer confirms or corrects the rest, with the right value already suggested for 96 of them.

Limits, stated plainly:

- The ground truth (`eval/ground_truth.json`) was transcribed by eye and is **not verified by a person yet**. The
  agreement thresholds and consistency tolerances were chosen on these same 10 patients; there is no held-out set.
- Handwriting styles vary per patient; Tesseract reads some of them poorly (patients 2, 3, 7 and 10 mostly go to
  review). Real phone photos (perspective, blur, Arabic handwriting) are not handled: the layout is the fixed
  specimen scan.
- Ticked boxes (facility type, blood group) are not read; `eval/mark_detection_demo.py` is a prototype only.

PaddleOCR remains available for comparison with `--extractor paddleocr` (Python 3.12 and the pinned
dependencies, `.venv-paddle312`). On patient 1 it took about 52 s and 7 GB of RAM for three pages.

### Demo script

1. **Midwife (simulated phone on the left):** select specimen pages `…-01.png`, `…-02.png` and `…-03.png`, then press **Envoyer**. Each page gets an acknowledgment, and the document appears in the queue as a provisional group.
2. Wait about 8 s (the grouping window). The fixture extractor produces a draft with 6 visits and **2 uncertain fields**.
3. **Back-office:** answer the questions:
   - Hauteur utérine (8e mois) is illegible ("3?"). Click **Corriger** and enter `31`.
   - Date of the 9th-month visit was read as "1?/01/2026" at 42 % confidence. Click **Corriger** and enter `18/01/2026`, which matches 38 SA from the DDR.
4. **Patient:** `PAT-000001` is *suggested* because the file number matches. Click it to select. Nothing is saved yet.
5. **CONFIRMER l'enregistrement:** the first three visits are already registered (unchanged), three new visits are created, and the timeline shows 6 visits.
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

### Simulator vs. real integration

| Part | In the MVP | Real integration |
|------|------------|------------------|
| WhatsApp inbound | Browser phone panel posts `{sender_id, message_id, media_ref}` to `/api/whatsapp/messages`. Images must already be in `data/Paper Registry/` | Not started: Meta webhook format, signature check, media download |
| WhatsApp outbound | Acknowledgments stored in the database and shown in the simulated thread | Not started: sending through the Cloud API |
| Senders | One seeded demo number mapped to *C/S Sidi Smail* | Not started: sender registration |
| Extraction | Default `FixtureExtractor` returns a hand-written draft for the selected demo pages. Optional `LiveOcrExtractor` reads the specimen pages with local Tesseract (four readings + consistency checks) and sends anything uncertain to review | OCR is local only; WhatsApp media download is not started |
| AI outage | "IA disponible" toggle | Would be a real extractor timeout or failure |

### Open blockers and limitations

Tracked in [tasks.md](./tasks.md#blockers-and-open-gaps-keep-visible-until-resolved).

- **Organizer confirmation (not obtained).** Verification is done by back-office staff, not the midwife, but the official instructions say "verified by the midwife". This deviation rests on verbal guidance and still needs written confirmation. The same applies to using WhatsApp's outbox as the phone-side offline queue. See [docs/operating-model.md](./docs/operating-model.md).
- **No encryption at rest.** `var/dayone.sqlite3` is a plain SQLite file.
- **No authentication.** Every `/api/*` route is open, including `POST /api/demo/reset`, which wipes the database. The reviewer name is free text. The server listens on `127.0.0.1` only by default and uses plain HTTP.
- **Offline gaps.** There is no on-device encrypted storage, and no demo of "offline capture, then return of connectivity". The demo shows the platform-side equivalent instead (AI outage, then recovery). `SYNCED` is not implemented.
- **WhatsApp retake message** is not implemented. Local OCR raises `RETAKE_REQUIRED` for an unusable photo; Aymane's Cloud API adapter must send the actual message.
