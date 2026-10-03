# Operating model (team agreement)

## Roles

| Actor | Responsibility | Does **not** do |
|-------|----------------|-----------------|
| **Midwife** | Photograph **fiche / registry pages** and send via WhatsApp (one or many pages per woman). | Patient tracking, field correction, match decisions, ID cards, extra steps beyond photos. |
| **Platform (Fatma + Ariyan)** | Extract structured data from the **fiche**, link visits, maintain longitudinal patient profiles, queue uncertain cases. | Change midwife paper workflow. |
| **Back-office reviewer** | Resolve `NEEDS_REVIEW` fields, confirm or correct values, choose patient match when ambiguous (`[Patiente 1] [Patiente 2] [Nouvelle] [Je ne sais pas]`). | — |

Human verification required by **consignes** is performed by the **back-office reviewer** (web UI, admin chat, or internal tool)—not by the midwife in WhatsApp.

## Input = fiche only (keep it simple)

Midwives send **photos of the paper maternal registry**—nothing else. No separate ID documents, no extra apps, no typing in chat.

## Patient tracking (from the fiche)

| Key | Source |
|-----|--------|
| `registry_file_number` | N° on the fiche header (e.g. `1-1.jpg`) |
| `midwife_patient_code` | Code written on the booklet—**extracted from the image** |
| `internal_patient_id` | System-generated (`PAT-000001` / UUID), never from name |
| Multipage | Group WhatsApp photos into one `Document` per woman/session |

**Never store** names, phone, or address from the form (consignes). If such text appears on paper, redact or drop in extraction.

**Never auto-create** a new patient when a plausible match exists—back-office decides (consignes).

## Midwife WhatsApp (minimal)

1. Send photo(s) of the fiche.  
2. Optional: multipage in one session (grouped by backend).  
3. Auto-reply: *« Reçu. Traitement en cours. »* — no clinical Q&A.

## Privacy

Per `consignes-fr-en.pdf`: extraction must ignore or redact direct identifiers on the fiche; no PHI in logs; synthetic dataset only unless approved for third-party models.
