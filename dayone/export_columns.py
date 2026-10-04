"""The 31 columns of data/maternal_registry_synthetic.csv and how far each can be produced (docs/excel-field-mapping.md).

This is the export side, kept apart from paper extraction (dayone/extended.py, dayone/ocr_extended.py): it names the
paper inputs of each column and the decisions that block it. It computes nothing. No export row exists yet, because
what a row is (D1), its generated ID (D2) and whether only reviewer-verified values are exported (D9) are open; the
pregnancy-level calculations (mean blood pressure, enrollment gestational age, one lab value per pregnancy, previous
cesarean) wait for D3, D4, D5 and D10.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import extended, schema

IMPLEMENTED_EVALUATED = "implemented and evaluated"
IMPLEMENTED_UNVERIFIED = "implemented but unverified"
REQUIRES_AGGREGATION = "requires aggregation"
UNAVAILABLE = "unavailable or awaiting definition"
CATEGORIES = (IMPLEMENTED_EVALUATED, IMPLEMENTED_UNVERIFIED, REQUIRES_AGGREGATION, UNAVAILABLE)


@dataclass(frozen=True)
class Column:
    number: int
    header: str  # exactly as in the CSV
    category: str
    scope: str
    inputs: tuple[str, ...]  # paper fields (v1.0 or extended) the column would be built from
    decisions: tuple[str, ...]  # open decisions (docs/excel-field-mapping.md) that block the export value
    note: str


COLUMNS = (
    Column(1, "id", UNAVAILABLE, "export row", (), ("D1", "D2"),
           "Generated at export, never from CIN, PAT- IDs or link keys. No export row is defined yet."),
    Column(2, "age (years)", IMPLEMENTED_EVALUATED, "pregnancy", ("maternal_age_years",), ("D1",),
           "Age as written on the identification page."),
    Column(3, "education level (0=none/primary,1=secondary,2=higher)", UNAVAILABLE, "patient",
           ("education_level_text",), ("crosswalk",),
           "Free text is extracted as written; no text-to-level crosswalk is agreed, so no level is chosen."),
    Column(4, "consanguinity", IMPLEMENTED_EVALUATED, "pregnancy", ("consanguinity_mark",), ("D5",),
           "Box state is extracted; whether an empty box means 0 and what 1 means is D5."),
    Column(5, "desired pregnancy", IMPLEMENTED_EVALUATED, "pregnancy", ("pregnancy_desired_mark",), ("D5",),
           "Box state is extracted; encoding is D5."),
    Column(6, "hypertension history", UNAVAILABLE, "patient", (), ("D8", "D5"),
           "No verified source. Family-history rows are never used as the woman's own history."),
    Column(7, "diabetes mellitus", UNAVAILABLE, "patient", (), ("D8", "D5"),
           "No verified source. Family-history rows are never used as the woman's own history."),
    Column(8, "gravidity (number)", IMPLEMENTED_EVALUATED, "pregnancy", ("gravidity",), ("D1",),
           "As written; always confirmed by a person (single handwritten digit)."),
    Column(9, "parity (number)", IMPLEMENTED_EVALUATED, "pregnancy", ("parity",), ("D1",),
           "As written; always confirmed by a person."),
    Column(10, "abortions (number)", IMPLEMENTED_UNVERIFIED, "pregnancy", ("abortions_count",), ("D1",),
           "Empty cell is NOT_PROVIDED, never 0; always confirmed by a person. Only 2 of the 10 specimen booklets "
           "have a written value, too few to call the reading evaluated."),
    Column(11, "living children (number)", IMPLEMENTED_EVALUATED, "pregnancy", ("living_children_count",), ("D1",),
           "As written; always confirmed by a person."),
    Column(12, "previous cesarean", REQUIRES_AGGREGATION, "pregnancy",
           ("previous_delivery_mode_text",), ("D5", "crosswalk"),
           "Modes of previous deliveries are extracted one by one as text; the yes/no across them is not computed."),
    Column(13, "bmi pregestational (kg/m2)", UNAVAILABLE, "pregnancy", ("height_cm",), (),
           "No pre-pregnancy weight or BMI on the forms; never computed from a pregnancy weight."),
    Column(14, "mean systolic bp (mmhg)", REQUIRES_AGGREGATION, "pregnancy", ("systolic_bp_mmhg",), ("D3",),
           "Per-visit values exist (v1.0); visit set, minimum count and rounding are D3."),
    Column(15, "mean diastolic bp (mmhg)", REQUIRES_AGGREGATION, "pregnancy", ("diastolic_bp_mmhg",), ("D3",),
           "Same visits as column 14 (D3)."),
    Column(16, "hemoglobin (g/dl)", REQUIRES_AGGREGATION, "pregnancy", ("hemoglobin_g_dl",), ("D10",),
           "Per-visit values are extracted; which visit gives the single value is D10."),
    Column(17, "first fasting glucose (mg/dl)", UNAVAILABLE, "pregnancy", ("blood_glucose_g_l",), ("D10",),
           "Bilan glycémique is extracted per visit in g/L, but the form never says fasting: never exported as fasting."),
    Column(18, "proteinuria", UNAVAILABLE, "pregnancy", ("albuminuria",), ("D10", "D5"),
           "Albuminurie is extracted per visit; whether it stands for proteinuria is not decided."),
    Column(19, "hiv test result", REQUIRES_AGGREGATION, "pregnancy", ("hiv_test",), ("D10", "D5"),
           "Per-visit results exist (v1.0); cross-visit rule and encoding are open."),
    Column(20, "syphilis test result", REQUIRES_AGGREGATION, "pregnancy", ("syphilis_test",), ("D10", "D5"),
           "Per-visit results exist (v1.0); cross-visit rule and encoding are open."),
    Column(21, "hepatitis c test result", UNAVAILABLE, "pregnancy", (), ("D8", "D5"),
           "No hepatitis C row on the specimen; Ag HBs is hepatitis B and is not used."),
    Column(22, "gestational age at enrollment (weeks)", REQUIRES_AGGREGATION, "pregnancy",
           ("gestational_age_days", "visit_date"), ("D4",),
           "Per-visit gestational age exists (v1.0); which visit is enrollment and rounding are D4."),
    Column(23, "gestational dm", UNAVAILABLE, "pregnancy", (), ("D8", "D5"),
           "No verified source; never derived from glucose values."),
    Column(24, "gestational age at birth (weeks)", IMPLEMENTED_EVALUATED, "delivery",
           ("delivery_gestational_age_days",), ("D1", "D4"),
           "Stored in days as written (weeks + days); days-to-weeks rounding for the CSV is D4."),
    Column(25, "preterm birth", UNAVAILABLE, "delivery", ("delivery_gestational_age_days",), ("D5", "definition"),
           "No documented definition; not derived from column 24."),
    Column(26, "type of delivery (0=vaginal,1=cesarean)", IMPLEMENTED_EVALUATED, "delivery", ("delivery_mode",),
           ("D1",), "Four form options; the header documents vaginal = 0 and cesarean = 1."),
    Column(27, "newborn sex (0=female,1=male)", IMPLEMENTED_EVALUATED, "newborn", ("newborn_sex",), ("D1", "D6"),
           "Encoding documented in the header; several newborns are D6."),
    Column(28, "child birth weight (g)", IMPLEMENTED_EVALUATED, "newborn", ("birth_weight_g",), ("D1", "D6"),
           "Weight at birth only, never a later consultation weight."),
    Column(29, "head circumference (cm)", IMPLEMENTED_EVALUATED, "newborn", ("birth_head_circumference_cm",),
           ("D1", "D6"), "Head circumference at birth only."),
    Column(30, "breastfeeding initiated", UNAVAILABLE, "newborn", ("feeding_mode",), ("D5",),
           "Feeding mode on the day of a later consultation is extracted; it is not proof of initiation at birth."),
    Column(31, "referral to higher care", UNAVAILABLE, "unresolved", (), ("D8", "D5"),
           "Mother or newborn, and which period, are not decided; nothing is extracted for it."),
)


def known_inputs() -> set[str]:
    return set(schema.FIELDS) | set(extended.FIELDS)


def coverage_counts() -> dict[str, int]:
    return {category: sum(c.category == category for c in COLUMNS) for category in CATEGORIES}


def coverage_markdown() -> str:
    """The coverage table of docs/excel-field-mapping.md, generated so that the document follows the code."""
    lines = ["| # | Exact header | Coverage | Scope | Paper inputs | Blocking decisions |",
             "|---|--------------|----------|-------|--------------|--------------------|"]
    for c in COLUMNS:
        inputs = ", ".join(f"`{name}`" for name in c.inputs) or "none"
        lines.append(f"| {c.number} | `{c.header}` | {c.category} | {c.scope} | {inputs} | "
                     f"{', '.join(c.decisions) or 'none'} |")
    return "\n".join(lines)


def compute_export_row(patient: dict, visits: list[dict], extended_draft: dict | None = None,
                       *, row_id: str | None = None, assumptions: dict | None = None) -> dict[str, object]:
    """Computes a single row corresponding to the 31 columns of maternal_registry_synthetic.csv.

    Fields that are unavailable or have unresolved open decisions (D1-D10) remain visibly None (empty).
    Deterministic aggregations (mean BP, delivery mode, newborn sex, gestational age) are computed
    from verified/known visit and extended paper inputs without inventing unmeasured data.
    """
    assumptions = assumptions or {}
    ext = extended_draft or {}
    pregnancy_ext = ext.get("pregnancy") or {}
    delivery_ext = ext.get("delivery") or {}
    newborns_ext = ext.get("newborns") or []
    prev_deliv_ext = ext.get("previous_deliveries") or []

    sorted_visits = sorted(
        [v for v in visits if v.get("visit_date")],
        key=lambda v: str(v.get("visit_date")),
    )

    sys_bps = [
        v["fields"]["systolic_bp_mmhg"]["value"]
        for v in visits
        if "systolic_bp_mmhg" in v.get("fields", {}) and v["fields"]["systolic_bp_mmhg"].get("value") is not None
    ]
    dia_bps = [
        v["fields"]["diastolic_bp_mmhg"]["value"]
        for v in visits
        if "diastolic_bp_mmhg" in v.get("fields", {}) and v["fields"]["diastolic_bp_mmhg"].get("value") is not None
    ]

    def _visit_lab(field_name: str) -> object:
        for v in sorted_visits:
            f = v.get("fields", {}).get(field_name)
            if f and f.get("value") is not None:
                return f.get("value")
        return None

    prev_cesarean = None
    if prev_deliv_ext:
        has_cesarean = any(
            "cesar" in str(d.get("fields", {}).get("previous_delivery_mode_text", {}).get("value", "")).lower()
            for d in prev_deliv_ext
        )
        all_vaginal = all(
            "basse" in str(d.get("fields", {}).get("previous_delivery_mode_text", {}).get("value", "")).lower()
            or "vag" in str(d.get("fields", {}).get("previous_delivery_mode_text", {}).get("value", "")).lower()
            for d in prev_deliv_ext
        )
        if has_cesarean:
            prev_cesarean = 1
        elif all_vaginal:
            prev_cesarean = 0

    delivery_mode_val = delivery_ext.get("delivery_mode", {}).get("value")
    delivery_type = None
    if delivery_mode_val:
        mode_str = str(delivery_mode_val).lower()
        if "cesar" in mode_str:
            delivery_type = 1
        elif "voie_basse" in mode_str or "vag" in mode_str:
            delivery_type = 0

    first_newborn = newborns_ext[0].get("fields", {}) if newborns_ext else {}
    nb_sex_val = first_newborn.get("newborn_sex", {}).get("value")
    nb_sex = None
    if nb_sex_val:
        sex_str = str(nb_sex_val).strip().upper()
        if sex_str in ("M", "MASCULIN", "GARÇON", "GARCON", "1"):
            nb_sex = 1
        elif sex_str in ("F", "FÉMININ", "FEMININ", "FILLE", "0"):
            nb_sex = 0

    hiv_val = _visit_lab("hiv_test")
    hiv_res = 1 if hiv_val == "POSITIVE" else (0 if hiv_val == "NEGATIVE" else None)
    syph_val = _visit_lab("syphilis_test")
    syph_res = 1 if syph_val == "POSITIVE" else (0 if syph_val == "NEGATIVE" else None)

    ga_enroll = None
    if sorted_visits:
        first_ga = sorted_visits[0].get("fields", {}).get("gestational_age_days", {}).get("value")
        if first_ga is not None:
            ga_enroll = round(first_ga / 7, 1) if assumptions.get("fractional_weeks") else first_ga // 7

    ga_birth_days = delivery_ext.get("delivery_gestational_age_days", {}).get("value")
    ga_birth = None
    if ga_birth_days is not None:
        ga_birth = round(ga_birth_days / 7, 1) if assumptions.get("fractional_weeks") else ga_birth_days // 7

    def _chk(val):
        if not assumptions.get("checkbox_0_1"):
            return None
        return 1 if val == "MARKED" else (0 if val == "UNMARKED" else None)

    return {
        "id": row_id or f"EXP-{patient.get('patient_id', 'UNKNOWN')}",
        "age (years)": pregnancy_ext.get("maternal_age_years", {}).get("value"),
        "education level (0=none/primary,1=secondary,2=higher)": None,
        "consanguinity": _chk(pregnancy_ext.get("consanguinity_mark", {}).get("value")),
        "desired pregnancy": _chk(pregnancy_ext.get("pregnancy_desired_mark", {}).get("value")),
        "hypertension history": None,
        "diabetes mellitus": None,
        "gravidity (number)": pregnancy_ext.get("gravidity", {}).get("value"),
        "parity (number)": pregnancy_ext.get("parity", {}).get("value"),
        "abortions (number)": pregnancy_ext.get("abortions_count", {}).get("value"),
        "living children (number)": pregnancy_ext.get("living_children_count", {}).get("value"),
        "previous cesarean": prev_cesarean,
        "bmi pregestational (kg/m2)": None,
        "mean systolic bp (mmhg)": round(sum(sys_bps) / len(sys_bps), 1) if sys_bps else None,
        "mean diastolic bp (mmhg)": round(sum(dia_bps) / len(dia_bps), 1) if dia_bps else None,
        "hemoglobin (g/dl)": _visit_lab("hemoglobin_g_dl"),
        "first fasting glucose (mg/dl)": None,
        "proteinuria": None,
        "hiv test result": hiv_res,
        "syphilis test result": syph_res,
        "hepatitis c test result": None,
        "gestational age at enrollment (weeks)": ga_enroll,
        "gestational dm": None,
        "gestational age at birth (weeks)": ga_birth,
        "preterm birth": None,
        "type of delivery (0=vaginal,1=cesarean)": delivery_type,
        "newborn sex (0=female,1=male)": nb_sex,
        "child birth weight (g)": first_newborn.get("birth_weight_g", {}).get("value"),
        "head circumference (cm)": first_newborn.get("birth_head_circumference_cm", {}).get("value"),
        "breastfeeding initiated": None,
        "referral to higher care": None,
    }


def export_to_csv(rows: list[dict[str, object]]) -> str:
    """Exports computed rows to standard CSV text matching the 31 headers."""
    import csv
    import io
    output = io.StringIO()
    headers = [c.header for c in COLUMNS]
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return output.getvalue()
