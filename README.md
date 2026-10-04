# MaternalData-Automation — DayOne (CodeML)

Platform that turns **photos of paper maternal registries** into structured digital records, **tracks each woman across visits**, and routes uncertain extractions to **back-office review**.

**Midwives only send documents** on WhatsApp; they do not manage patients or verify fields in chat. In the current MVP, WhatsApp is **simulated** in the browser by default. An optional WhatsApp Cloud API mode exists, but it has only been tested against a mocked Meta API so far. Extraction uses **fixtures** (see [Simulator vs. real integration](#simulator-vs-real-integration)).

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

### Screen layout

The back-office workspace is the main screen (French): review queue on the left, the open document in the centre, and the patient timeline below. The **Prochaine action** panel at the top of the document always says what to do next. It shows the current question, the patient choice or the confirmation summary. Drafts are labelled *Brouillon — non enregistré*; saved data is labelled *Enregistré*. Status and confidence are always written as text (for example *à vérifier · confiance faible (42 %)*), not only shown by colour.

The **simulated midwife phone** sits on the right (below the workspace on narrower screens) in a dashed frame labelled *Simulateur*. The *IA disponible* toggle and *Réinitialiser la démo* are grouped under *Contrôles de démonstration*.

Queue entries and grid cells are buttons, so the screen works with Tab / Enter. Escape closes a correction form or a simulator reply.

### Demo script

The presenter version, with the manual-entry path and the separately labelled hybrid media test (real Meta media API, simulated webhook), is in [docs/demo-script.md](./docs/demo-script.md). None of it needs Meta business verification.

1. **Midwife (simulated phone on the right):** select `1-1.jpg` then `1-5.jpg`, then press **Envoyer (2)**. Each page gets an acknowledgment, and the document appears in the queue as a provisional group.
2. Wait about 8 s (the grouping window). The fixture extractor produces a draft with 3 visits and **2 uncertain fields**.
3. **Back-office:** answer the questions in **Prochaine action**:
   - Hauteur utérine (2e trimestre – visite 1) is illegible ("2? cm"). Click **Corriger** and enter `25`, or **Confirmer « illisible »**.
   - Date of the 9th-month visit was read as "1?/12/25" at 42 % confidence. Click **Corriger** and enter `19/12/2025`, which matches the 40 SA + 1 j gestational age.
4. **Patient:** `PAT-000001` is *suggested* because both keys match. Click **Choisir la patiente 1 : PAT-000001**. Nothing is saved yet.
5. **Confirmer et enregistrer:** 3 visits are created, and the timeline shows 6 visits, with the new ones marked *dossier ouvert*.
6. Optional checks:
   - **Rejouer le dernier webhook** is ignored as a duplicate.
   - Untick **IA disponible** and send photos: they wait in `PENDING_AI` until it is ticked again.
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
| `dayone/store.py`, `dayone/service.py`, `dayone/linking.py` | Ariyan | Storage, lifecycle, review/confirm/timeline operations |
| `dayone/server.py`, `dayone/static/` | Aymane | HTTP API, simulated WhatsApp, webhook-only listener, back-office screen, Day1 logo (`logo-day1.jpg`) |
| `dayone/whatsapp.py`, `.env.example`, `docs/whatsapp-cloud.md`, `tools/webhook_relay.py`, `tools/hybrid_media_test.py` | Aymane (storage parts: Ariyan) | Cloud API adapter: webhook verification and parsing, media download, durable inbound/outbound jobs, outbound hold, configuration CLI, sandbox tools |
| `tests/` | all | Schema contract, end-to-end flow (idempotency, restart, grouping), retake workflow (`test_retake.py`), Cloud API adapter against a mocked Meta (`test_whatsapp_cloud.py`) |

Retake API: `POST /api/pages/{page_id}/retake` (idempotent while pending), `POST /api/retakes/{request_id}/cancel`, and an optional `retake_request_id` in the `/api/whatsapp/messages` body. Contract: [docs/schema.md](./docs/schema.md#retake-requests).

### Simulator vs. real integration

| Part | In the MVP (default) | Real integration (`--whatsapp-mode cloud`) |
|------|------------|------------------|
| WhatsApp inbound | Browser phone panel posts `{sender_id, message_id, media_ref}` to `/api/whatsapp/messages`. Images must already be in `data/Paper Registry/` | Implemented, mocked tests only: signed webhook, batched events, deduplication by WhatsApp message ID, authenticated and bounded media download, durable retries. Against Meta (unpublished app): webhook verification, a signed dashboard test webhook, and real media download with a simulated webhook. No real message received yet |
| WhatsApp outbound | Acknowledgments and retake requests stored in the database and shown in the simulated thread | Implemented, mocked tests only: durable send queue with retries and delivery statuses. No template messages, so nothing can be sent more than 24 h after the midwife's last message |
| Retake reply | The simulator sends `retake_request_id` explicitly with the replacement photo | Implemented, mocked tests only: a photo sent as a reply to the retake message replaces the page. Whether WhatsApp includes that reply context for images still needs sandbox verification |
| Senders | One seeded demo number mapped to *C/S Sidi Smail* | `python -m dayone.whatsapp add-sender`. Messages from unregistered numbers are rejected without downloading the photo |
| Extraction | `FixtureExtractor` returns a hand-written draft for `1-1.jpg` + `1-4.jpg` or `1-1.jpg` + `1-5.jpg`; any other page set fails and goes to manual entry. Values are illustrative, not ground truth | Not started: OCR / vision model behind the same interface |
| AI outage | "IA disponible" toggle | Would be a real extractor timeout or failure |

### Open blockers and limitations

Tracked in [tasks.md](./tasks.md#blockers-and-open-gaps-keep-visible-until-resolved).

- **Organizer confirmation (not obtained).** Verification is done by back-office staff, not the midwife, but the official instructions say "verified by the midwife". This deviation rests on verbal guidance and still needs written confirmation. The same applies to using WhatsApp's outbox as the phone-side offline queue. See [docs/operating-model.md](./docs/operating-model.md).
- **No encryption at rest.** `var/dayone.sqlite3` is a plain SQLite file, and photos received from WhatsApp are stored unencrypted in `var/media/whatsapp/`.
- **WhatsApp Cloud API only partly verified against Meta.** Webhook verification, signatures and media download work with a real Meta app. No real message has been received or sent: the app is unpublished (publishing needs business verification) and is not yet subscribed to the WhatsApp Business Account. Using it sends photos through Meta and the tunnel provider, so use specimen pages only until the organizers approve. See [docs/whatsapp-cloud.md](./docs/whatsapp-cloud.md#what-works-locally-and-what-still-needs-the-sandbox).
- **No authentication.** Every `/api/*` route is open, including `POST /api/demo/reset`, which wipes the database. The reviewer name is free text. The server listens on `127.0.0.1` only by default and uses plain HTTP.
- **Offline gaps.** There is no on-device encrypted storage, and no demo of "offline capture, then return of connectivity". The demo shows the platform-side equivalent instead (AI outage, then recovery). `SYNCED` is not implemented.
- **Retake over real WhatsApp is unverified.** In cloud mode, the request is sent as a reply quoting the photo, and the midwife's reply is matched to the request. Both are tested only with mocks. A registered document cannot be retaken: new photos form a new document, which goes through the update/keep decision for existing visits.
- **Manual entry** covers a single encounter and is only offered after a failed extraction.
