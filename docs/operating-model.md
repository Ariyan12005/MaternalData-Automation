# Operating model (team agreement)

## Roles

| Actor | Responsibility | Does **not** do |
|-------|----------------|-----------------|
| **Midwife** | Photograph **fiche pages** and send them on WhatsApp (one or many pages per woman). Receives a short acknowledgment per page. | Patient tracking, field correction, match decisions, ID cards, typing in chat. |
| **Platform (Fatma + Ariyan)** | Group pages into documents, extract a draft, **suggest** a patient, keep longitudinal patient timelines. | Register anything without a reviewer. |
| **Back-office reviewer (Aymane's screen)** | Answer the uncertain-field questions, correct values, **select** the patient (`[Patiente N] [Nouvelle] [Je ne sais pas]`), split/regroup pages, press **CONFIRMER**. | — |

Registration always requires an explicit reviewer: auto-linking only suggests a candidate (see `identity-strategy.md`).

## Input = fiche only

Midwives send **photos of the paper fiche**—nothing else. No ID documents, no extra app, no typing.

## Patient tracking (from the fiche)

| Key | Source |
|-----|--------|
| `registry_file_number` | *N° de la fiche* on the cover (page 1 of the booklet) |
| `midwife_patient_code` | Code written on the booklet (e.g. `CM: 164125`) |
| `patient_id` | Generated (`PAT-000001`), never derived from name or keys |

Keys are unique **per facility**. A visit is identified by (`patient_id`, `encounter_type`, `visit_date`), so re-photographing a booklet does not create new visits. Details: `identity-strategy.md`.

**Never store** names, phone or address from the form. **Never auto-create** a patient.

## Midwife WhatsApp (minimal)

1. Send photo(s) of the fiche. Pages sent close together are grouped provisionally into one document.
2. Auto-reply per page: *« Reçu : page N. Merci, le traitement est en cours. »*
3. No clinical Q&A in the midwife thread.

Two channels share the same ingest code:

- **Simulator** (default): a browser phone panel picks specimen pages or takes a camera photo and posts to `/api/whatsapp/messages`; acknowledgments are shown in the simulated thread.
- **WhatsApp Cloud API** (`dayone/whatsapp.py`, enabled by `WHATSAPP_*` environment variables): `GET /webhook/whatsapp` answers Meta's verify-token handshake; `POST` calls must carry a valid `X-Hub-Signature-256`; images are downloaded through the Graph API and stored encrypted; the text `fin` closes the open group of pages; acknowledgments and retake requests are sent back through the Cloud API with retries. Tested against a fake Graph API only: no live Meta account has been connected.

## Offline: where the durable queue lives

Decision: **no companion app**. The midwife's phone runs WhatsApp only.

| Segment | Who holds the data while something is down | Durable? | Ours? |
|---------|--------------------------------------------|----------|-------|
| Phone has no network (real WhatsApp) | WhatsApp's own outbox on the phone; delivered when the network returns | Yes (WhatsApp behaviour) | **No**—we do not control its storage or encryption |
| Phone has no network (simulated phone, demo) | Browser outbox in IndexedDB, AES-GCM encrypted under a non-extractable WebCrypto key; sent in capture order on reconnection, with the original message ids | Yes (survives a page reload) | Yes, clearly labelled as simulation |
| Platform receives a photo | `pages` + `documents` rows in the platform database (`var/dayone.sqlite3`, AES-GCM encrypted snapshot written after each commit), committed **before** the acknowledgment is sent | Yes | Yes |
| AI / extraction unavailable | Document stays `CAPTURED` → `PENDING_AI` in the same database; the worker retries every second | Yes, survives restarts | Yes |
| Webhook delivered twice | `source_message_id` unique → same page returned, no second acknowledgment | — | Yes |
| Confirm retried | Already registered → stored result returned, no new visit | — | Yes |
| Central registry unreachable | Document stays `SYNC_FAILED`; retried with exponential back-off, delivered once it returns (idempotency key) | Yes | Yes |

**What the demo proves:** an offline capture on the (simulated) phone, kept encrypted on the device, sent in order when connectivity returns; acknowledgment only after persistence; AI outage ("IA disponible") and central-registry outage ("Registre central disponible") leave work queued and nothing is lost; queue and drafts survive a server restart; webhook replay and confirm retry never duplicate pages or visits; registered visits reach `SYNCED`.

**What it does not prove:** encryption of a real phone's WhatsApp storage (not ours). Ask the organizers whether the simulated phone outbox is acceptable as the "local queue" (task 4 allows "simulated").

## Conversational requirement: checked against the instructions

The official text puts verification **with the midwife, in the conversation**:

- Short description: *"verified by the midwife"*.
- Main objective: *"guides the midwife through verification"*.
- Objectives: *"Let the midwife confirm, edit, retake the photo, or enter data manually through follow-up questions."*
- Task 3 and rubric (20 pts, "Conversational review workflow"): Confirm / Edit / Retake, follow-up questions, manual entry, multi-page sessions.

Moving review to the back-office therefore **changes that part of the submission**; it does not satisfy it as written. Our position:

| Rubric item | Our implementation |
|-------------|--------------------|
| Confirm / Edit | Yes, as a one-question-at-a-time chat in the back-office screen |
| Follow-up questions on illegible fields | Yes (`ILLEGIBLE` / `NEEDS_REVIEW` must be answered before registration) |
| Manual entry when AI is unavailable | Yes: after a failed extraction or while a document waits during an AI outage, with as many visits as needed |
| Multi-page sessions | Yes (provisional grouping, split/regroup, "fin" closes a session) |
| Retake photo | Yes: the reviewer (or the system, for an unusable photo) sends *« Merci de reprendre la photo de la page N »*; the next photo replaces the page |
| Actor = midwife | **No**: actor is back-office staff, following the organizers' verbal guidance |

Actions: get the organizers' guidance **in writing**; state the deviation in the README and the demo narration; keep the review logic channel-agnostic (same API) so it could be offered to the midwife in WhatsApp if required.

## Privacy

Per `consignes-fr-en.pdf`: extraction must ignore or redact direct identifiers on the fiche; no PHI in logs; local storage must be encrypted; synthetic data only for third-party models unless approved.

| Control | State in the MVP |
|---------|------------------|
| Only the 12 contract fields can be stored | Done: `validate_draft` rejects unknown fields |
| Direct identifiers detected on the page | OCR finds the printed labels (CIN, nom, prénom, adresse, téléphone, mari) and records them in `pii_detected` as `NOT_EXTRACTED`; the values are never read into fields |
| PHI in logs | Request logs contain the path only, never query strings or bodies |
| Original images and ground truth | Read only; pages reference files in `data/Paper Registry/` with a SHA-256 hash |
| Encryption at rest | AES-256-GCM: database snapshot, uploaded photos, central-registry sink; key from `DAYONE_DATA_KEY` (or an owner-only `var/dayone.key` for development) |
| Authentication / roles | Login with scrypt-hashed passwords, HttpOnly SameSite=Strict session cookie, CSRF header, roles `reviewer` / `admin`; the reviewer on every action is the signed-in user |
| Transport | HTTPS with `--tls-cert` / `--tls-key` (Secure cookies, HSTS); bound to `127.0.0.1` by default |
| Central registry | Anonymized payload: internal IDs and values, no link keys, no raw OCR text |
