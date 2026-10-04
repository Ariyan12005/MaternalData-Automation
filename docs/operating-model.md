# Operating model (team agreement)

## Roles

| Actor | Responsibility | Does **not** do |
|-------|----------------|-----------------|
| **Midwife** | Photograph **fiche pages** and send them on WhatsApp (one or many pages per woman). Receives a short acknowledgment per page. | Patient tracking, field correction, match decisions, ID cards, typing in chat. |
| **Platform (Fatma + Ariyan)** | Group pages into documents, extract a draft, **suggest** a patient, keep longitudinal patient timelines. | Register anything without a reviewer. |
| **Back-office reviewer (Aymane's screen)** | Answer the uncertain-field questions, correct values, **select** the patient (`[Patiente N] [Nouvelle] [Je ne sais pas]`), split/regroup pages, request a retake of a page, press **Confirmer et enregistrer**. | — |

Registration always requires an explicit reviewer: auto-linking only suggests a candidate (see `identity-strategy.md`).

## Input = fiche only

Midwives send **photos of the paper fiche**—nothing else. No ID documents, no extra app, no typing.

## Patient tracking (from the fiche)

| Key | Source |
|-----|--------|
| `registry_file_number` | *N° de la fiche* on the cover (`1-1.jpg`) |
| `midwife_patient_code` | Code written on the booklet (e.g. `CM: 164125`) |
| `patient_id` | Generated (`PAT-000001`), never derived from name or keys |

Keys are unique **per facility**. A visit is identified by (`patient_id`, `encounter_type`, `visit_date`), so re-photographing a booklet does not create new visits. Details: `identity-strategy.md`.

**Never store** names, phone or address from the form. **Never auto-create** a patient.

## Midwife WhatsApp (minimal)

1. Send photo(s) of the fiche. Pages sent close together are grouped provisionally into one document.
2. Auto-reply per page: *« Reçu : page N. Merci, le traitement est en cours. »*
3. Only if the back-office asks: *« Merci de reprendre la photo de la page N. »* The midwife replies to that message with a new photo. A cancelled request is announced as *« Demande annulée : inutile de reprendre la photo de la page N. »*
4. No clinical Q&A in the midwife thread.

In the MVP this channel is a **simulator**. A browser phone panel posts JSON to `/api/whatsapp/messages`, and acknowledgments are stored and displayed but never sent to a phone. The real Meta Cloud API integration has not been started: webhook format, signature check, media download and outbound messages are all missing.

## Offline: where the durable queue lives

Decision: **no companion app**. The midwife's phone runs WhatsApp only.

| Segment | Who holds the data while something is down | Durable? | Ours? |
|---------|--------------------------------------------|----------|-------|
| Phone has no network | WhatsApp's own outbox on the phone; delivered when the network returns | Yes (WhatsApp behaviour) | **No**—we do not control its storage or encryption |
| Platform receives a photo | `pages` + `documents` rows in the platform SQLite file (`var/dayone.sqlite3`), committed **before** the acknowledgment is sent | Yes | Yes |
| AI / extraction unavailable | Document stays `CAPTURED` → `PENDING_AI` in the same database; the worker retries every second | Yes, survives restarts | Yes |
| Webhook delivered twice | `source_message_id` unique → same page returned, no second acknowledgment | — | Yes |
| Confirm retried | Already `REGISTERED` → stored result returned, no new visit | — | Yes |

**What the demo proves:** acknowledgment only after persistence; AI outage ("IA disponible" toggle) leaves documents queued and nothing is lost; queue and drafts survive a server restart (`tests/test_flow.py`); webhook replay and confirm retry never duplicate pages or visits.

**What it does not prove (gaps against `consignes-fr-en.pdf`):**

- *Offline capture on the device with encrypted local storage* (§4, task 4). We rely on WhatsApp's outbox, which we neither build nor demonstrate. The official demo asks for "an offline capture, the return of connectivity"; ours shows the platform-side equivalent (AI outage → recovery).
- Encryption at rest of the platform database and images: not implemented yet.
- `SYNCED` (upload to a central registry): not implemented.

Ask the organizers whether WhatsApp's outbox is an acceptable "local queue". If not, the fallback is a simulated phone outbox in the demo, clearly labelled as simulation (task 4 allows "simulated").

## Conversational requirement: checked against the instructions

The official text puts verification **with the midwife, in the conversation**:

- Short description: *"verified by the midwife"*.
- Main objective: *"guides the midwife through verification"*.
- Objectives: *"Let the midwife confirm, edit, retake the photo, or enter data manually through follow-up questions."*
- Task 3 and rubric (20 pts, "Conversational review workflow"): Confirm / Edit / Retake, follow-up questions, manual entry, multi-page sessions.

Moving review to the back-office therefore **changes that part of the submission**; it does not satisfy it as written. Our position:

| Rubric item | Our implementation |
|-------------|--------------------|
| Confirm / Edit | Yes, one question at a time in the back-office "Prochaine action" panel |
| Follow-up questions on illegible fields | Yes (`ILLEGIBLE` / `NEEDS_REVIEW` must be answered before registration) |
| Manual entry when AI is unavailable | **Partly**: only after a failed extraction (`PROCESSING_FAILED`), one encounter; not offered while documents wait for an AI outage to end |
| Multi-page sessions | Yes (provisional grouping, split/regroup) |
| Retake photo | **Yes, in the simulator**: the reviewer asks for a page again, the midwife's thread receives « Merci de reprendre la photo de la page N. », and the reply photo replaces the page and re-runs extraction. Not sent to a real phone; real WhatsApp reply-context mapping not started |
| Actor = midwife | **No**: actor is back-office staff, following the organizers' verbal guidance |

Actions: get the organizers' guidance **in writing**; state the deviation in the README and the demo narration; keep the review logic channel-agnostic (same API) so it could be offered to the midwife in WhatsApp if required.

## Privacy

Per `consignes-fr-en.pdf`: extraction must ignore or redact direct identifiers on the fiche; no PHI in logs (the server logs paths only, never query strings or bodies); synthetic data only for third-party models unless approved.


## Part 3 implementation update

Secure Atlas mode now stores encrypted records and original uploads; SQLite
remains the fixture demo. An optional encrypted bridge/simulator outbox captures
photos while disconnected and retries uploads and batch closure. This is not a
phone companion app and does not establish WhatsApp device storage guarantees.
Backend bearer/Basic authentication uses a shared credential; individual facility
roles and automated retention remain gaps. See [backend contracts](backend-part3.md).
Per `consignes-fr-en.pdf`: extraction must ignore or redact direct identifiers on the fiche; no PHI in logs; local storage must be encrypted; synthetic data only for third-party models unless approved.

| Control | State in the MVP |
|---------|------------------|
| Only the 12 contract fields can be stored | Done: `validate_draft` rejects unknown fields |
| Direct identifiers detected on the page | Recorded by hand in the fixtures (`pii_detected`); no real detection yet |
| PHI in logs | Request logs contain the path only, never query strings or bodies |
| Original images and ground truth | Read only; pages reference files in `data/Paper Registry/` with a SHA-256 hash |
| Encryption at rest | Atlas records/originals encrypted; SQLite demo records remain plain |
| Authentication / roles | Shared token in secure mode; individual/facility roles remain unimplemented; held/Atlas reset disabled |
| Transport | Plain HTTP, bound to `127.0.0.1` by default |
