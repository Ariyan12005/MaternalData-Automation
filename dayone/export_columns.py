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
