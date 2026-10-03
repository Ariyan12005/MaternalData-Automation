# MaternalData-Automation — DayOne (CodeML)

Offline-first, WhatsApp-style agent: photograph paper maternal registries, extract structured fields with uncertainty, midwife verification, encrypted queue/sync, and visit linking.

**Challenge:** *Une sage-femme, un téléphone et une IA* / *The Offline Midwife* (Challenge ID **17**).

## Team

| Person | Area |
|--------|------|
| **Fatma** | AI / extraction |
| **Aymane** | WhatsApp / conversation |
| **Ariyan** | Backend / sync / privacy |

**Work plan:** [tasks.md](./tasks.md)

## Key documents

| File | Purpose |
|------|---------|
| [consignes-fr-en.pdf](./consignes-fr-en.pdf) | Official instructions & grading (FR + EN) |
| [Extra Info CodeML Hackathon.docx](./Extra%20Info%20CodeML%20Hackathon.docx) | Architecture notes, V1 scope, workflow FAQ |
| [manifest.json](./manifest.json) | Dataset file list and checksums |

## Data (read-only)

- `data/Paper Registry/` — registry photos and specimen PDF  
- `data/maternal_registry_synthetic.csv` / `.xlsx` — 200-row synthetic reference table  

Do not modify original images or ground-truth files per consignes.

## Status

Planning / Day 0 — implementation services not yet in repo. Follow milestones in `tasks.md`.
