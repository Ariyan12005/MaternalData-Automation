# Demo script (no Meta business verification needed)

Parts A and B use only the browser simulator: nothing leaves the machine and no Meta account is involved. Part D (optional) is the same with local OCR instead of fixtures. Part C is a separate, optional test that calls Meta's real media API while the inbound WhatsApp message is **simulated locally**. Keep the labels when presenting it:

| Label | Meaning |
|-------|---------|
| **SIMULATOR** | Browser phone panel; no network |
| **REAL META** | Real Graph API call with the app's token (upload, metadata, download) |
| **SIMULATED WEBHOOK** | A webhook signed locally with the app secret and posted to `127.0.0.1`; Meta did not send it |
| **LOCAL** | DayOne's own processing and back-office |

Use synthetic specimen pages only. Do not claim that a real WhatsApp message was received or sent: with an unpublished app, none was.

## Before you start

```powershell
python -m dayone --db var/demo.sqlite3          # http://127.0.0.1:8000, simulator mode
```

A separate `--db` keeps the demo away from the sandbox databases. **Réinitialiser la démo** wipes only the database the server was started with.

## Part A: fixture extraction → review → confirmation → timeline (SIMULATOR + LOCAL)

1. **Midwife, simulated phone:** select `1-1.jpg` then `1-5.jpg`, then press **Envoyer (2)**. Each page gets « Reçu : page N… », and a provisional document appears in the queue.
2. Wait about 8 s, the grouping window. The fixture extractor returns a draft with 3 visits and 2 uncertain fields.
3. **Back-office, Prochaine action:**
   - Hauteur utérine (2e trimestre – visite 1) is *Illisible, à vérifier*. Click **Saisir la valeur**, enter `25`, then **Valider la correction**.
   - The 9th-month visit date reads « 1?/12/25 », with *Score de lecture 42 % (indicatif)*. Click **Corriger** and enter `19/12/2025`.
4. **Patient:** `PAT-000001` is suggested because both keys match. Click **Choisir la patiente 1 : PAT-000001**. Nothing is saved yet.
5. Click **Confirmer et enregistrer**. The timeline shows 6 visits, with the 3 new ones marked *dossier ouvert*.

## Part B: no fixture → manual entry → review → confirmation → timeline (SIMULATOR + LOCAL)

In fixture mode, this is the path for a photo outside the sample page sets. For local PaddleOCR, use Part D.

1. **Simulated phone:** select `dossiers_specimen_10_patientes-01.png` and `dossiers_specimen_10_patientes-03.png`, then press **Envoyer (2)**. Sending both together keeps them in one document.
2. After the grouping window, the queue shows *Échec de lecture* and the document says *Lecture impossible : choisir une solution*, with the reason *Aucune extraction disponible pour ce groupe de pages (prototype à fixtures)*. Click **Saisie manuelle**.
3. Answer each question from pages -01 and -03 with **Saisir la valeur** (then **Valider la correction**) or **Non renseigné sur la fiche**. The values below are the checked ground truth for visit 2 of the first trimester (`eval/specimen-ground-truth.json`):

   | Field | Enter |
   |-------|-------|
   | N° de la fiche | `2026823001` (printed 2026-823-001) |
   | Code patiente (sage-femme) | **Non renseigné** |
   | Établissement | `CSCA Al Wifaq` |
   | DDR (dernières règles) | `26/04/2025` |
   | Date de visite | `20/07/2025` |
   | Âge gestationnel | `12SA+0j` |
   | Poids | `58.8` |
   | TA systolique / TA diastolique | `104` / `74` |
   | Hauteur utérine | **Non renseigné** |
   | Syphilis (TPHA/VDRL) / Sérologie VIH | `négatif` / `négatif` |

4. After the last answer, **Prochaine action** becomes *Choisir la patiente*: *Aucune patiente de cet établissement ne porte ces clés.* Click **Nouvelle patiente**.
5. Read the summary, then click **Confirmer et enregistrer**. A new patient is created (*patiente PAT-00000N (créée)*), with one antenatal visit dated 20/07/2025 in its timeline. Each field is marked *saisie manuelle*, and the history lists every answer with the reviewer's name.

Optional: **Rejouer le dernier webhook** is ignored as a duplicate. The retake demo is in the [README](../README.md#retake-demo).

## Part C (optional): hybrid media test (REAL META + SIMULATED WEBHOOK + LOCAL)

What it proves: DayOne can download a photo from Meta's real media API and carry it through the workflow. What it does **not** prove: that Meta delivers real messages to our webhook, that `from` matches the registered number, or that sending works. The app is unpublished, and outgoing messages are held.

Prerequisites:

- `.env` filled in, and `check-config` passes.
- A test sender registered on the hybrid database.
- A valid access token. The temporary API Setup token expires after about 24 hours.

1. **LOCAL:** start a separate instance on a copy of the database, with outgoing messages held:

   ```powershell
   python -m dayone --env-file .env --whatsapp-mode cloud --db var/sandbox-hybrid.sqlite3 --port 8010 --webhook-port 8011 --hold-outbound
   ```

   The hold is stored in that database. The instance will not start again without `--hold-outbound` until someone runs `python -m dayone.whatsapp discard-held --db var/sandbox-hybrid.sqlite3`, which marks the held messages `FAILED` without sending them.
2. **REAL META → SIMULATED WEBHOOK → LOCAL:**

   ```powershell
   python tools/hybrid_media_test.py --image "data/Paper Registry/dossiers_specimen_10_patientes-01.png" --image "data/Paper Registry/dossiers_specimen_10_patientes-03.png" --replay
   ```

   Each output line starts with its label. Expect:
   - `[REAL META] uploaded`, with size and SHA-256 matching;
   - `[SIMULATED] ... HTTP 200`, posted then replayed;
   - `[LOCAL] inbound jobs for this message: 1 ... DONE`, with a page and `stored bytes identical: True`;
   - finally `Nothing was sent`.

   The tool refuses a database without the hold and any webhook URL that is not on loopback. It prints no token, signature, phone number or media URL, and it does not save the webhook body.
3. **LOCAL:** open `http://127.0.0.1:8010` and continue as in Part B, steps 2–5. Pages sent more than 8 s apart form two documents. Move the second page with **Déplacer…** before starting manual entry.

Uploaded media stay with Meta for up to 30 days unless deleted. The tool was checked against the mocked Graph API (`tests/test_whatsapp_cloud.py`). The 2026-10-03 run recorded in [docs/whatsapp-cloud.md](./whatsapp-cloud.md#what-works-locally-and-what-still-needs-the-sandbox) made the same calls by hand, before the tool existed.

## Part D (optional): local OCR → review → confirmation → timeline (SIMULATOR + LOCAL)

Needs the OCR install from the [README](../README.md#optional-local-ocr-synthetic-specimen-layout). Specimen pages only; no page leaves the machine.

```powershell
$env:DAYONE_OCR_CPU_THREADS = "2"
.venv\Scripts\python -m dayone --host 127.0.0.1 --extractor paddle --db var/submission-demo.sqlite3 --port 8025 --hold-outbound
```

The server loads the OCR models at startup (a few seconds) and refuses to start if a model or package is missing.

1. **Simulated phone:** select `dossiers_specimen_10_patientes-01.png` and `dossiers_specimen_10_patientes-03.png`, then press **Envoyer (2)**.
2. After the grouping window, the document shows the reading progress (page N of M). About 30 s later it is *À vérifier*, with the patient's 6 visits read from the grid. In the live check, 8 values needed review, including the file number and facility (always confirmed by a person) and the midwife code, which is not on this cover (**Non renseigné sur la fiche**).
3. **Prochaine action** shows each value with *Lu sur la photo*, an indicative score, and an orange box on the page and in the zoomed crop. Compare with the photo, then **Confirmer la valeur** or **Corriger**. The checked ground truth is in `eval/specimen-ground-truth.json` (patient 1).
4. **Patient:** click **Nouvelle patiente**, as in Part B.
5. **Read the whole summary against the page before Confirmer et enregistrer.** Values read as sure (`KNOWN`) were not asked about, and confirmation records them as confirmed by the reviewer.

Say when presenting it: the pages are synthetic renders of a handwriting font, not real phone photos. On clean renders no wrong value was marked sure (0 of 667), but on simulated photos 8 of 979 were, with high scores; the score is indicative, not a probability ([ocr-evaluation.md](./ocr-evaluation.md)). Only the specimen layout is read, not the pink booklet.

After registration, demonstrate **Synchroniser avec le registre central (simulé)** and the **Exporter CSV (31 col.)** button. The central receipt and interrupted-network outbox are local simulations. Show the exported headers and verified values, with unavailable values left blank. For offline transport, queue specimen pages in the simulated phone's offline mode, restore the connection, and continue the same OCR review. The browser still needs access to this local server while queuing: disconnected-phone capture is not implemented.

For age, gravidity or parity, include the specimen identification page `dossiers_specimen_10_patientes-02.png` and use **Vérifier les données Excel** in the summary before registration. These optional OCR values require their own explicit review and otherwise remain blank in the CSV. A cover or identification page alone contains no visit: include `-03.png` for visit blood pressure and gestational age. Reviewed identification or delivery data can still be exported from a registered document with no visits.

When running from the integration worktree, use the repository's existing Python environment (`..\..\.venv\Scripts\python.exe`) instead of `.venv\Scripts\python` in the command above. Open `http://127.0.0.1:8025/`. Use `--extractor fixture` with Part A as a prepared fallback if the OCR environment is unavailable.
