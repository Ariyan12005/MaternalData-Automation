# Identity, visit identification and duplicates

Inputs are fiche photos only. No ID cards, no names, no extra steps for midwives.

## 1. Facility-scoped patient keys

- Each WhatsApp sender is registered to **one facility** (`senders` table). A document inherits the sender's facility. In the MVP the only sender is a seeded demo number; there is no registration flow yet.
- A patient belongs to one facility. Link keys are unique **within a facility only**:
  - `registry_file_number`: *N° de la fiche* on the cover.
  - `midwife_patient_code`: code the midwife writes on the booklet (consignes' random code; e.g. `CM: 164125`).
- The same number at two facilities means two different patients. There is no cross-facility matching in the MVP.
- Keys are stored in canonical form and compared exactly. The validator only accepts digits for `registry_file_number` and `A-Z 0-9 -` for `midwife_patient_code`. Reviewer input is normalized first: separators are removed from the file number, and the code is uppercased with spaces and the leading `CM` label removed.
- `patient_id` (`PAT-000001`) is generated at registration and never derived from keys.

## 2. Candidate search

Run on every draft change, using the draft's current key values (corrections re-run the search).

| Situation | Result | Suggested? |
|-----------|--------|------------|
| All keys present in the draft match one patient, both keys `KNOWN` | `STRONG` candidate | **Yes** (`suggested_patient_id`) |
| All present keys match one patient, but a key is `NEEDS_REVIEW` | `STRONG` candidate | No |
| One key matches, the other differs from the stored value | `CONFLICT` candidate, flag `KEY_MISMATCH` | No |
| Keys point to two different patients | Both listed as `CONFLICT`, flag `MULTIPLE_CANDIDATES` | No |
| No key readable / no match | No candidate | No |

**Auto-link only suggests.** The suggestion is highlighted in the review screen but not stored as a selection: the reviewer must click the patient (status becomes `PATIENT_MATCHED`) and then confirm the whole record before anything is registered.

## 3. Reviewer choices

`[Patiente N]` · `[Nouvelle patiente]` · `[Je ne sais pas]`

- **Existing patient:** must be in the document's facility. Keys missing on the patient are filled from the draft if no other patient holds them; stored keys are never overwritten.
- **New patient:** always an explicit reviewer action, never automatic. Rejected if a draft key is already registered to another patient in the facility. The reviewer must then correct the key or pick that patient.
- **New patient without any key:** allowed and flagged `NO_LINK_KEY`; future visits will not auto-link.
- **Je ne sais pas:** document parked as `DUPLICATE_SUSPECTED`; nothing registered until someone decides.

## 4. Visit identification (document ≠ visit)

- Keys identify the **woman**. A **document** is one upload session (a group of pages).
- A **visit** is one encounter column of the visit grid, identified by **(`patient_id`, `encounter_type`, `visit_date`)**, where `visit_date` is the *Venue le* row. The column label (`slot`, e.g. `M8`) is supporting evidence.
- `visit_date` is required: an encounter without a date cannot be registered (blocking field).
- Two encounters with the same date in one draft → blocking error `DUPLICATE_ENCOUNTER_DATE`.

At confirmation, each encounter is compared with the selected patient's visits:

| Outcome | Rule | Action |
|---------|------|--------|
| `NEW` | No visit on that date | Create visit |
| `EXISTS_SAME` | Visit on that date, no differing value | Link document as extra source; no new visit |
| `EXISTS_DIFFERENT` | Visit on that date, some value differs | Reviewer chooses **Mettre à jour** (draft values replace differing ones; previous values kept in history) or **Garder l'existante** |

A re-photographed booklet therefore produces `EXISTS_SAME` for columns already registered and `NEW` only for columns added since.

## 5. Duplicate protection layers

| Risk | Protection |
|------|------------|
| Webhook delivered twice | `pages.source_message_id` unique; replay returns the existing page, no second acknowledgment |
| Confirm clicked twice / retried | Already `REGISTERED` → returns the stored registration result; nothing written |
| Same visit registered twice | Unique index (`patient_id`, `encounter_type`, `visit_date`) |
| Same woman created twice | Unique index on each key within facility; NEW never automatic |
| Booklet re-photographed | Visit outcomes above |

## 6. Multipage grouping is provisional

- Pages from one sender are grouped into the open document while the **grouping window** is open (default 8 s in the demo, configurable). Each page gets an acknowledgment.
- Grouping can mix two women's pages, so it stays `PROVISIONAL` until registration.
- Back-office can **split** (move a page to a new document) or **regroup** (move a page to another open document of the same facility).
- Regrouping resets the affected drafts (re-extraction; earlier field reviews on those drafts are discarded and logged).
- Registration sets the grouping to `CONFIRMED`: the reviewer has seen the page list in the summary.


## Part 3 refinements

Suggestions require both paper codes, each KNOWN with confidence >= 0.75, and one
candidate matching both. One readable key lists candidates but cannot produce a
suggestion. Staff still select explicitly. Existing visit updates accept selected
differing fields and a version fingerprint; stale visits return 409. The UI
supplies this fingerprint and checkboxes; HTTP unversioned UPDATE strings are
refused. Previous values and source documents remain in history. See
[backend contracts](backend-part3.md).
