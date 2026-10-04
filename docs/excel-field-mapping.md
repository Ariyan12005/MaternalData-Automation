# Excel / CSV field mapping (31 columns)

**Status:** proposal for a shared extension. Nothing here is implemented. The completed MVP covers 12 fields (`docs/schema.md`, `dayone/schema.py`) and has no export. Schema changes start only after the decisions in [Unresolved decisions](#unresolved-decisions) are agreed.

**Dataset:** `data/maternal_registry_synthetic.csv` (200 rows, 31 columns) and `data/maternal_registry_synthetic.xlsx` (one sheet, `maternal_registry_synthetic`, same 31 headers). Both are read-only and listed in `manifest.json`. Neither file contains a data dictionary, and `consignes-fr-en.pdf` describes the variable groups but not the column encodings.

**Rules for this document**

- Headers are quoted exactly as they appear in the CSV.
- An encoding is stated only when the header documents it. Three headers do (`education level`, `type of delivery`, `newborn sex`); every other `0`/`1` meaning is marked **undocumented**.
- A registry source is listed only when the label was seen on a page in `data/Paper Registry/`. Anything else is **not verified**, with candidates described in the notes.
- No clinical value is derived, filled, or defaulted unless the rule is listed under unresolved decisions and agreed.

## Definitions

**Coverage**

| Value | Meaning |
|-------|---------|
| Supported | The MVP captures exactly this value at the column's scope. No column qualifies today. |
| Requires aggregation | The MVP captures the inputs (per visit), but one CSV value needs a rule across visits or a unit conversion. |
| Not implemented | Neither the value nor its inputs exist in the MVP schema. |

`hiv test result` and `syphilis test result` are listed as *requires aggregation*: the MVP stores one result per visit, while a CSV row holds a single value.

**Scope:** patient, pregnancy episode, visit, delivery, newborn. The MVP only has patient-level link keys and visit-level encounters. Pregnancy-episode, delivery and newborn entities do not exist yet.

**Source verification:** checked on the sample booklet `1-1.jpg` … `1-5.jpg` and on specimen patient 1 (`dossiers_specimen_10_patientes-01.png` … `-08.png`). The specimen is a different template ("Modèle de formulaire neutre") from the MVP layout `ma-fiche-surveillance-grossesse-v1`. Its pages are:

| Page | Section |
|------|---------|
| `-01` | Cover, *Grossesse classée à risque* |
| `-02` | *Identification et antécédents* |
| `-03` | *Grossesse actuelle* (visit grid) |
| `-04` | *Déroulement de l'accouchement* |
| `-05` / `-07` | Post-partum consultations, mother (*précoce* / *tardif*) |
| `-06` / `-08` | Post-partum consultations, newborn (*précoce* / *tardif*) |

The other nine specimen patients were not checked.

**Owners**

- **Fatma:** extraction of the source value from the page, with status and confidence.
- **Ariyan:** storage, derived values, and the export column.
- **Aymane:** review question and display in the back-office.

## Missing values and uncertainty (applies to every column)

- **Internal:** a missing value is `null` with a `field_status` (`NOT_PROVIDED`, `ILLEGIBLE`, `UNKNOWN`, `NOT_APPLICABLE`, `NEEDS_REVIEW`), as in the MVP. `raw_text` is kept (for example `RAS`, `Neg`, `Lycée`).
- **Export:** a missing value is an empty cell. The CSV cannot carry the reason for a blank, so the status is lost on export unless a companion column is added (decision D9).
- **Derived values:** a derived value is empty when any input is missing or unverified. Nothing is imputed.
- **Checkboxes:** an empty checkbox is not assumed to mean `0` (decision D5).
- **Direct identifiers** (name, husband's name, CIN, phone, address) appear on the same pages as several of these fields (`1-2.jpg`, `-02`, `-04`). They are never extracted or exported.

## Summary

| # | Exact header | Coverage | Scope | Source |
|---|--------------|----------|-------|--------|
| 1 | `id` | Not implemented | Export row (D1) | Generated, not on paper |
| 2 | `age (years)` | Not implemented | Pregnancy episode | Verified |
| 3 | `education level (0=none/primary,1=secondary,2=higher)` | Not implemented | Patient | Verified (free text on form) |
| 4 | `consanguinity` | Not implemented | Pregnancy episode | Verified |
| 5 | `desired pregnancy` | Not implemented | Pregnancy episode | Verified |
| 6 | `hypertension history` | Not implemented | Patient | Not verified |
| 7 | `diabetes mellitus` | Not implemented | Patient | Not verified |
| 8 | `gravidity (number)` | Not implemented | Pregnancy episode | Verified |
| 9 | `parity (number)` | Not implemented | Pregnancy episode | Verified |
| 10 | `abortions (number)` | Not implemented | Pregnancy episode | Verified |
| 11 | `living children (number)` | Not implemented | Pregnancy episode | Verified |
| 12 | `previous cesarean` | Not implemented | Pregnancy episode | Verified (needs aggregation of previous deliveries) |
| 13 | `bmi pregestational (kg/m2)` | Not implemented | Pregnancy episode | Not verified (height only) |
| 14 | `mean systolic bp (mmhg)` | Requires aggregation | Pregnancy episode, from visits | Verified (*TA*) |
| 15 | `mean diastolic bp (mmhg)` | Requires aggregation | Pregnancy episode, from visits | Verified (*TA*) |
| 16 | `hemoglobin (g/dl)` | Not implemented | Visit | Verified |
| 17 | `first fasting glucose (mg/dl)` | Not implemented | Visit | Partly verified (fasting not stated, unit g/L) |
| 18 | `proteinuria` | Not implemented | Visit | Not verified (*Albuminurie* candidate) |
| 19 | `hiv test result` | Requires aggregation | Visit | Verified |
| 20 | `syphilis test result` | Requires aggregation | Visit | Verified |
| 21 | `hepatitis c test result` | Not implemented | Visit | Not verified (no dedicated row) |
| 22 | `gestational age at enrollment (weeks)` | Requires aggregation | Pregnancy episode, from first visit | Verified (*Âge probable*) |
| 23 | `gestational dm` | Not implemented | Pregnancy episode | Not verified |
| 24 | `gestational age at birth (weeks)` | Not implemented | Delivery | Verified (specimen only) |
| 25 | `preterm birth` | Not implemented | Delivery | Not verified as a field; derivable from column 24 |
| 26 | `type of delivery (0=vaginal,1=cesarean)` | Not implemented | Delivery | Verified (specimen only) |
| 27 | `newborn sex (0=female,1=male)` | Not implemented | Newborn | Verified (specimen only) |
| 28 | `child birth weight (g)` | Not implemented | Newborn | Verified (specimen only) |
| 29 | `head circumference (cm)` | Not implemented | Newborn | Verified (specimen only) |
| 30 | `breastfeeding initiated` | Not implemented | Newborn | Not verified (feeding mode at day 7 only) |
| 31 | `referral to higher care` | Not implemented | Unresolved | Not verified |

**Totals:** 0 supported, 5 requiring aggregation, 26 not implemented.

## Column details

### 1. `id`

- **Coverage:** Not implemented.
- **Proposed field:** `export_row_id`, integer, no unit. Generated at export time (decision D2).
- **Scope:** one export row (decision D1).
- **Source:** none; not on any form.
- **Missing / uncertainty:** never empty. It must not be derived from `PAT-`/`DOC-` IDs, `registry_file_number`, `midwife_patient_code` or any personal data.
- **Owner:** Ariyan. No extraction or review.
- **Data note:** CSV values are 1–200, unique and sequential.

### 2. `age (years)`

- **Coverage:** Not implemented.
- **Proposed field:** `maternal_age_years`, integer, years. Store the number as written; do not compute it from a birth date.
- **Scope:** pregnancy episode (age when the booklet was filled in).
- **Source:** verified. *Identification*, row *Age*, on `1-2.jpg` and specimen `-02`.
- **Missing / uncertainty:** `null` + status. The form does not say when the age was recorded (booking or delivery); keep as written.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** CSV 15–43, integers, no blanks.

### 3. `education level (0=none/primary,1=secondary,2=higher)`

- **Coverage:** Not implemented.
- **Proposed field:** `education_level`, enum `NONE_OR_PRIMARY` / `SECONDARY` / `HIGHER`, exported as `0` / `1` / `2` (encoding documented in the header).
- **Scope:** patient.
- **Source:** verified. *Niveau d'instruction* on `1-2.jpg` and specimen `-02`. The form takes free text (specimen: `Lycée`), not a 3-level code.
- **Missing / uncertainty:** keep `raw_text`. A text-to-level crosswalk must be agreed before any value becomes `KNOWN`; until then the value stays `NEEDS_REVIEW`.
- **Owner:** Fatma extraction (raw text) · Ariyan crosswalk + export · Aymane review (pick the level).
- **Data note:** 18 blanks in the CSV.

### 4. `consanguinity`

- **Coverage:** Not implemented.
- **Proposed field:** `consanguinity`, boolean. CSV `0`/`1` meaning is **undocumented** (D5).
- **Scope:** pregnancy episode (recorded once per booklet; relates to the current partner).
- **Source:** verified. Checkbox *Consanguinité* on `1-2.jpg` and specimen `-02`.
- **Missing / uncertainty:** an empty checkbox is not read as "no" until D5 is decided; until then it is `NEEDS_REVIEW` or `NOT_PROVIDED`.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** no blanks in the CSV.

### 5. `desired pregnancy`

- **Coverage:** Not implemented.
- **Proposed field:** `pregnancy_desired`, boolean. CSV `0`/`1` meaning is **undocumented** (D5).
- **Scope:** pregnancy episode.
- **Source:** verified. Checkbox *Grossesse désirée* on `1-2.jpg` and specimen `-02`.
- **Missing / uncertainty:** same checkbox rule as column 4.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** no blanks in the CSV.

### 6. `hypertension history`

- **Coverage:** Not implemented.
- **Proposed field:** `history_hypertension`, boolean. CSV `0`/`1` meaning is **undocumented** (D5).
- **Scope:** patient.
- **Source:** **not verified.** Candidates, none confirmed as the CSV meaning:
  - row *HTA* under *Antécédents héréditaires et familiaux*, which is family history with two columns (*Famille de la femme*, *Mari/famille du mari*), on `1-2.jpg` and specimen `-02`;
  - free text *Antécédents de la femme → Médicaux*;
  - checkbox *H.T.A* under *Grossesse classée à risque* on specimen `-01`.
- **Missing / uncertainty:** `RAS` in a free-text cell is kept as `raw_text`. Its conversion to a boolean is part of D5.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review. The team must first agree which section is the source.

### 7. `diabetes mellitus`

- **Coverage:** Not implemented.
- **Proposed field:** `history_diabetes`, boolean. CSV `0`/`1` meaning is **undocumented** (D5).
- **Scope:** patient.
- **Source:** **not verified.** Same candidates as column 6:
  - row *Diabète* (family history);
  - *Antécédents de la femme → Médicaux*;
  - checkbox *Diabète* under *Grossesse classée à risque* on `-01`.

  Whether the column means pre-existing diabetes only, as distinct from column 23, is not documented.
- **Missing / uncertainty:** as column 6.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.

### 8. `gravidity (number)`

- **Coverage:** Not implemented.
- **Proposed field:** `gravidity`, integer, count. Whether it includes the current pregnancy is not stated on the form or in the header.
- **Scope:** pregnancy episode (snapshot at booking).
- **Source:** verified. *Gestation* on `1-3.jpg` and specimen `-02`.
- **Missing / uncertainty:** `null` + status. Handwritten marks like `1G` on `1-3.jpg` keep their `raw_text`.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** CSV 1–7, no blanks; parity ≤ gravidity on every row.

### 9. `parity (number)`

- **Coverage:** Not implemented.
- **Proposed field:** `parity`, integer, count.
- **Scope:** pregnancy episode.
- **Source:** verified. *Parité* on `1-3.jpg` and specimen `-02`.
- **Missing / uncertainty:** `null` + status. A blank *Parité* (as on `1-3.jpg`) is `NOT_PROVIDED`, not 0.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** CSV 0–7, no blanks.

### 10. `abortions (number)`

- **Coverage:** Not implemented.
- **Proposed field:** `abortions_count`, integer, count.
- **Scope:** pregnancy episode.
- **Source:** verified. *Antécédents obstétricaux*, row *Avortement*, column *Nombre*, on `1-3.jpg` and specimen `-02`.
- **Missing / uncertainty:** an empty *Nombre* cell is `NOT_PROVIDED`, not 0.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** 11 blanks in the CSV.

### 11. `living children (number)`

- **Coverage:** Not implemented.
- **Proposed field:** `living_children_count`, integer, count.
- **Scope:** pregnancy episode.
- **Source:** verified. *Nombre d'enfants vivants* on `1-3.jpg` and specimen `-02`.
- **Missing / uncertainty:** `null` + status.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** CSV 0–7, no blanks.

### 12. `previous cesarean`

- **Coverage:** Not implemented.
- **Proposed field:** `previous_cesarean`, boolean, derived. CSV `0`/`1` meaning is **undocumented** (D5).
- **Scope:** pregnancy episode.
- **Source:** verified. Section *Déroulement des accouchements antérieurs*, rows *Modalité d'extraction* and *Si césarienne : indication*, columns *Accouchement 1–5*, on `1-3.jpg` and specimen `-02`. There is no single checkbox.
- **Missing / uncertainty:** each previous delivery is stored separately first. The derived value is empty when any listed delivery has an unreadable mode.
- **Owner:** Fatma extraction (per previous delivery) · Ariyan derivation/export · Aymane review.
- **Data note:** no row has `previous cesarean` set with parity 0.

### 13. `bmi pregestational (kg/m2)`

- **Coverage:** Not implemented.
- **Proposed field:** `bmi_pregestational_kg_m2`, decimal, kg/m². The input field `height_cm` (integer, cm) would also be new.
- **Scope:** pregnancy episode.
- **Source:** **not verified.** *Taille* (height) is on the *Grossesse actuelle* header (`1-4.jpg`, specimen `-03`: `155 cm`). No pre-pregnancy weight field was found; visit *Poids* is weight during pregnancy.
- **Missing / uncertainty:** stays empty unless a written pre-pregnancy BMI or weight is found on the form. It is never computed from a pregnancy weight without an agreed rule.
- **Owner:** Fatma extraction (height) · Ariyan storage/export · Aymane review.
- **Data note:** 16 blanks; 15.0 is both the minimum and appears 3 times.

### 14. `mean systolic bp (mmhg)`

- **Coverage:** Requires aggregation. Built from the MVP field `systolic_bp_mmhg`.
- **Proposed field:** `mean_systolic_bp_mmhg`, decimal (CSV uses one decimal), mmHg. Export only; not stored as a field.
- **Scope:** pregnancy episode, computed from visits.
- **Source:** verified. Row *TA* of the visit grid (`1-4.jpg`, `1-5.jpg`, specimen `-03`). Post-partum consultations also have *TA* (`-05`, `-07`), so the visit set matters (D3).
- **Missing / uncertainty:** only verified visit values are used. The rounding rule and the minimum number of visits are part of D3.
- **Owner:** Fatma extraction (already in the MVP) · Ariyan aggregation/export · Aymane shows which visits were averaged.
- **Data note:** CSV 72.6–130.7, no blanks.

### 15. `mean diastolic bp (mmhg)`

- **Coverage:** Requires aggregation. Built from the MVP field `diastolic_bp_mmhg`.
- **Proposed field:** `mean_diastolic_bp_mmhg`, decimal, mmHg. Export only.
- **Scope:** pregnancy episode, computed from visits.
- **Source:** verified. Row *TA*, as column 14.
- **Missing / uncertainty:** it must use the same visits as column 14 (D3).
- **Owner:** as column 14.
- **Data note:** CSV 42.3–83.4, no blanks.

### 16. `hemoglobin (g/dl)`

- **Coverage:** Not implemented.
- **Proposed field:** `hemoglobin_g_dl`, decimal, g/dL, per visit. Export needs a visit-selection rule (D10).
- **Scope:** visit.
- **Source:** verified. *Examen biologique*, row *Hémoglobine*, on `1-4.jpg` and specimen `-03`. The specimen has two values, in the 1st and 2nd trimester.
- **Missing / uncertainty:** `null` + status per visit. The export is empty until D10 is decided.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** 23 blanks.

### 17. `first fasting glucose (mg/dl)`

- **Coverage:** Not implemented.
- **Proposed field:** `blood_glucose_g_l`, decimal, g/L as written on the form, per visit. Export converts to mg/dL (×100, exact unit conversion).
- **Scope:** visit; export uses the first visit with a value (D10).
- **Source:** partly verified. The only glucose row is *Bilan glycémique* on `1-4.jpg` and specimen `-03` (written as `0.93 g/L`). The form does not say whether the test was fasting.
- **Missing / uncertainty:** the fasting status is unknown. Exporting *Bilan glycémique* under a "fasting" header needs agreement (D10). *Glucosurie* (urine) is a different test and is not used.
- **Owner:** Fatma extraction · Ariyan conversion/export · Aymane review.
- **Data note:** 35 blanks.

### 18. `proteinuria`

- **Coverage:** Not implemented.
- **Proposed field:** `albuminuria`, enum, per visit. Values are taken from form entries; only `Neg` was observed, so the set is not final. Export `proteinuria` is derived; the CSV `0`/`1` meaning is **undocumented** (D5).
- **Scope:** visit.
- **Source:** **not verified.** The grid has *Albuminurie* (`1-4.jpg`, specimen `-03`), not *Protéinurie*. Equivalence and the cross-visit rule (any positive, first, last) are not confirmed (D10).
- **Missing / uncertainty:** `null` + status per visit. The export is empty until D5 and D10 are decided.
- **Owner:** Fatma extraction · Ariyan derivation/export · Aymane review.
- **Data note:** no blanks.

### 19. `hiv test result`

- **Coverage:** Requires aggregation. The MVP has `hiv_test` (`NEGATIVE` / `POSITIVE`) per visit.
- **Proposed field:** existing `hiv_test`; export `hiv_test_result`. Whether `1` means positive is **undocumented** (D5).
- **Scope:** visit; export needs a rule across visits (D10).
- **Source:** verified. *Sérologie VIH* on `1-4.jpg`, `1-5.jpg` and specimen `-03`.
- **Missing / uncertainty:** visits without a result are `NOT_PROVIDED`. Notes like "test rapide" stay in `raw_text`.
- **Owner:** Fatma extraction (exists) · Ariyan aggregation/export · Aymane review (exists).
- **Data note:** 10 blanks.

### 20. `syphilis test result`

- **Coverage:** Requires aggregation. The MVP has `syphilis_test` (`NEGATIVE` / `POSITIVE`) per visit.
- **Proposed field:** existing `syphilis_test`; export `syphilis_test_result`. Encoding **undocumented** (D5).
- **Scope:** visit; export rule as column 19 (D10).
- **Source:** verified. *Syphilis (TPHA/VDRL)* on `1-4.jpg`, `1-5.jpg` and specimen `-03`.
- **Missing / uncertainty:** as column 19.
- **Owner:** as column 19.
- **Data note:** 7 blanks.

### 21. `hepatitis c test result`

- **Coverage:** Not implemented.
- **Proposed field:** `hepatitis_c_test`, enum `NEGATIVE` / `POSITIVE` (same pattern as the MVP tests), per visit. Export encoding **undocumented** (D5).
- **Scope:** visit; export rule as column 19 (D10).
- **Source:** **not verified.** Neither template has a hepatitis C row. *Ag HBs* is hepatitis B and is not used. On `1-4.jpg`, an HCV result is handwritten in the free-text *Autres* cell; the specimen has nothing.
- **Missing / uncertainty:** a result found only in *Autres* is extracted as `NEEDS_REVIEW`.
- **Owner:** Fatma extraction (free text) · Ariyan storage/export · Aymane review.
- **Data note:** 5 blanks.

### 22. `gestational age at enrollment (weeks)`

- **Coverage:** Requires aggregation. Built from the MVP field `gestational_age_days`.
- **Proposed field:** `enrollment_gestational_age_days`, integer, days (MVP convention). Export converts to decimal weeks (days / 7; rounding in D4).
- **Scope:** pregnancy episode, from the enrollment visit.
- **Source:** verified. *Âge probable (de la grossesse)* on `1-4.jpg` and specimen `-03`.
- **Missing / uncertainty:** which visit counts as enrollment, and whether to fall back on *DDR*, is D4. The value is empty if the chosen visit has no verified GA.
- **Owner:** Fatma extraction (exists) · Ariyan derivation/export · Aymane review.
- **Data note:** CSV 6.0–24.9, no blanks; 6.0 is the minimum and appears 7 times. Enrollment GA ≤ birth GA on every row.

### 23. `gestational dm`

- **Coverage:** Not implemented.
- **Proposed field:** `gestational_diabetes`, boolean. Encoding **undocumented** (D5).
- **Scope:** pregnancy episode.
- **Source:** **not verified.** No field says *diabète gestationnel*. The cover checkbox *Grossesse classée à risque → Diabète* (`-01`) does not distinguish gestational from pre-existing diabetes.
- **Missing / uncertainty:** never derived from glucose values (that would be a clinical judgement).
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review, once a source is agreed.

### 24. `gestational age at birth (weeks)`

- **Coverage:** Not implemented.
- **Proposed field:** `delivery_gestational_age_days`, integer, days. Export in decimal weeks (D4).
- **Scope:** delivery.
- **Source:** verified on the specimen only. *Déroulement de l'accouchement → État du nouveau-né → Âge gestationnel*, on `-04` (written `40 SA`). The sample booklet has no delivery page in the repo.
- **Missing / uncertainty:** the form value may be whole weeks while the CSV has decimals (22 of 200 rows are whole numbers). Whether to compute from *DDR* and the delivery date instead is D4.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** CSV 34.6–42.0, no blanks.

### 25. `preterm birth`

- **Coverage:** Not implemented.
- **Proposed field:** `preterm_birth`, boolean, derived from column 24. Encoding **undocumented** (D5).
- **Scope:** delivery.
- **Source:** not verified as a delivery field. The specimen newborn post-partum pages (`-06`, `-08`) have a checkbox *Nouveau-né prématuré*.
- **Missing / uncertainty:** empty when column 24 is empty. If the checkbox and the derived value disagree, the value goes to review; no automatic choice.
- **Owner:** Ariyan derivation/export · Fatma extraction (checkbox) · Aymane review of conflicts.
- **Data note:** in all 200 rows, `1` coincides with GA at birth < 37. This is an observation, not a documented definition.

### 26. `type of delivery (0=vaginal,1=cesarean)`

- **Coverage:** Not implemented.
- **Proposed field:** `delivery_mode`, enum from the form options: `VAGINAL_NON_INSTRUMENTAL`, `VAGINAL_INSTRUMENTAL`, `CESAREAN_PLANNED`, `CESAREAN_EMERGENCY`. Export `0` = vaginal (both vaginal values) and `1` = cesarean (both cesarean values), as the header documents.
- **Scope:** delivery.
- **Source:** verified on the specimen only. *Mode de l'accouchement* on `-04`.
- **Missing / uncertainty:** a cesarean with no planned/emergency box ticked still exports as `1`, but stays `NEEDS_REVIEW` internally.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** no blanks.

### 27. `newborn sex (0=female,1=male)`

- **Coverage:** Not implemented.
- **Proposed field:** `newborn_sex`, enum `FEMALE` / `MALE`; export `0` / `1` (documented in the header).
- **Scope:** newborn.
- **Source:** verified on the specimen only. *État du nouveau-né → Sexe* on `-04` (written `F`).
- **Missing / uncertainty:** `null` + status. Any other written value goes to review; no third code is invented.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** no blanks.

### 28. `child birth weight (g)`

- **Coverage:** Not implemented.
- **Proposed field:** `birth_weight_g`, integer, grams.
- **Scope:** newborn.
- **Source:** verified on the specimen only. *Poids à la naissance* on `-04`. The post-partum pages (`-06`, `-08`) record weight at later ages; those are not birth weight.
- **Missing / uncertainty:** `null` + status.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** CSV 2234–4800, no blanks. 4800 is the maximum and appears 37 times. That may be a cap in the synthetic data, but this is not confirmed, so do not use it as a validation limit.

### 29. `head circumference (cm)`

- **Coverage:** Not implemented.
- **Proposed field:** `birth_head_circumference_cm`, decimal, cm.
- **Scope:** newborn.
- **Source:** verified on the specimen only. *Périmètre crânien à la naissance* on `-04`. The post-partum pages (`-06`, `-08`) have *Périmètre crânien* at later ages; those are not used.
- **Missing / uncertainty:** `null` + status.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** 23 blanks; 82 values are non-integer. 40 is the maximum and appears 91 times; possible cap, not confirmed.

### 30. `breastfeeding initiated`

- **Coverage:** Not implemented.
- **Proposed field:** `newborn_feeding_mode`, enum `EXCLUSIVE_BREASTFEEDING` / `ARTIFICIAL` / `MIXED`, from the form options, per newborn consultation. Export `breastfeeding_initiated` is derived; encoding **undocumented** (D5).
- **Scope:** newborn.
- **Source:** **not verified** for "initiated". The specimen newborn pages have *Allaitement : exclusivement au sein / artificiel / mixte* at the early (`-06`, day 7) and late (`-08`, day 43) consultations. The delivery page has no breastfeeding field.
- **Missing / uncertainty:** whether feeding mode at day 7 can stand for "initiated" must be agreed (D5); until then the export is empty.
- **Owner:** Fatma extraction · Ariyan derivation/export · Aymane review.
- **Data note:** no blanks.

### 31. `referral to higher care`

- **Coverage:** Not implemented.
- **Proposed field:** `referral_to_higher_care`, boolean. Encoding **undocumented** (D5).
- **Scope:** unresolved: mother or newborn, and which period.
- **Source:** **not verified.** Candidates:
  - *Transfert* + *établissement de référence* on the newborn post-partum pages (`-06`, `-08`);
  - *Référée* on the maternal post-partum pages (`-05`, `-07`), but it sits under *Planification familiale*, so it is probably not the same concept.

  Nothing seen on the antenatal or delivery pages.
- **Missing / uncertainty:** export empty until the source and scope are agreed.
- **Owner:** Fatma extraction · Ariyan storage/export · Aymane review.
- **Data note:** no blanks.

## Unresolved decisions

None of these are decided. Each needs agreement from Fatma, Ariyan and Aymane, and organizer input where noted.

| # | Decision | What we know | Open question |
|---|----------|--------------|---------------|
| D1 | **What one export row represents** | Each CSV row has one delivery and one newborn, which suggests one pregnancy episode per row. This is not documented. The MVP has no pregnancy-episode entity; visits attach directly to the patient. | One row per pregnancy episode, per patient, or per newborn? Ask the organizers. |
| D2 | **Generated export ID** | CSV `id` is 1–200, sequential. | Sequential per export, or stable across exports (needs a private mapping table)? It must never expose `PAT-` IDs, `registry_file_number`, `midwife_patient_code` or any personal data. |
| D3 | **Visits included in mean BP** | *TA* appears on antenatal visit columns and on maternal post-partum consultations. | Antenatal visits only? All verified visits, or only `CONFIRMED`/`CORRECTED`? Minimum number of visits? Rounding (CSV has one decimal)? Systolic and diastolic must use the same visits. |
| D4 | **Gestational-age rules** | Enrollment: the MVP stores `gestational_age_days` per visit (*Âge probable*). Delivery: specimen `-04` gives *Âge gestationnel* in SA. CSV uses decimal weeks. | Which visit is "enrollment": the first dated *Venue le* or the first visit with a GA? Use the written GA, or compute from *DDR*? Same choice for delivery GA (written versus *DDR* + delivery date). Days-to-weeks rounding. |
| D5 | **Boolean and test-result encodings** | Only three headers document their codes. The `0`/`1` meaning is undocumented for 13 columns: `consanguinity`, `desired pregnancy`, `hypertension history`, `diabetes mellitus`, `previous cesarean`, `proteinuria`, `hiv test result`, `syphilis test result`, `hepatitis c test result`, `gestational dm`, `preterm birth`, `breastfeeding initiated`, `referral to higher care`. | Confirm with the organizers whether `1` = yes/positive. Does an empty checkbox mean `0` or missing? How do free-text entries (`RAS`, `Neg`, `Néant`) convert? |
| D6 | **Multiple pregnancies and multiple newborns** | A woman can have several pregnancies over time (several booklets). Specimen `-04` has a single newborn block; the CSV has one newborn per row. | How are separate pregnancies of the same patient told apart (new booklet, delivery date)? For twins: one row per newborn, first-born only, or flag and exclude? |
| D7 | Specimen template as a second layout | The specimen pages differ from `ma-fiche-surveillance-grossesse-v1` (other sections, other positions). | Register a second layout ID, or extend v1? |
| D8 | Source for history and risk fields | Columns 6, 7, 23 and 31 have only candidate sources (above). | Which section feeds each column? |
| D9 | Missing-value reasons in the export | The CSV has blanks only; internal statuses distinguish `NOT_PROVIDED` / `ILLEGIBLE` / `UNKNOWN`. | Add a companion status column, or export blanks only? Export only reviewer-verified values? |
| D10 | Cross-visit rules for labs | Hemoglobin, glucose, albuminuria, HIV, syphilis and HCV are per visit; the CSV has one value. | First, last, or any-positive per column? Is *Bilan glycémique* acceptable as "first fasting glucose"? Is *Albuminurie* acceptable as "proteinuria"? |

## Data observations (no conclusions drawn)

| Observation | Columns |
|-------------|---------|
| Blank cells | education 18, abortions 11, BMI 16, hemoglobin 23, glucose 35, HIV 10, syphilis 7, hepatitis C 5, head circumference 23. All other columns have none. |
| Values piled at the range limit | birth weight 4800 (max) × 37; head circumference 40 (max) × 91; GA at enrollment 6.0 (min) × 7; BMI 15.0 (min) × 3 |
| Consistency checks, all passing | `preterm birth` = 1 exactly when GA at birth < 37; GA at enrollment ≤ GA at birth; parity ≤ gravidity; no previous cesarean with parity 0 |
| Linkage | The CSV rows are not linked to the specimen patients or to the sample booklet. No row is known to correspond to any image. |

The clusters may come from clipping in the synthetic generator. Until the organizers confirm, they are not used as validation limits.
