"""Field catalog, statuses and draft validation (see docs/schema.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = "1.0"
LAYOUT_ID = "ma-fiche-surveillance-grossesse-v1"
KNOWN_CONFIDENCE_THRESHOLD = 0.75

FIELD_STATUSES = ("KNOWN", "NEEDS_REVIEW", "ILLEGIBLE", "NOT_PROVIDED", "UNKNOWN", "NOT_APPLICABLE")
MISSING_STATUSES = ("ILLEGIBLE", "NOT_PROVIDED", "UNKNOWN", "NOT_APPLICABLE")
VERIFICATION_STATES = ("UNVERIFIED", "CONFIRMED", "CORRECTED")
ENCOUNTER_TYPES = ("ANTENATAL",)
PII_ACTIONS = ("NOT_EXTRACTED", "REDACTED")

SLOTS = {
    "T1_V1": "1er trimestre – visite 1",
    "T1_V2": "1er trimestre – visite 2",
    "T1_V3": "1er trimestre – visite 3",
    "T2_V1": "2e trimestre – visite 1",
    "T2_V2": "2e trimestre – visite 2",
    "T2_V3": "2e trimestre – visite 3",
    "M7": "7e mois",
    "M8": "8e mois",
    "M9": "9e mois",
    "MANUAL": "Saisie manuelle",
}

FIELD_VALUE_KEYS = {
    "raw_text", "value", "unit", "confidence", "field_status",
    "validation_flags", "source", "verification", "corrections",
}
DRAFT_KEYS = {
    "schema_version", "layout_id", "notes", "extraction", "pages",
    "pii_detected", "document_fields", "encounters",
}


class InvalidValue(ValueError):
    """Reviewer input that cannot be normalized; message is shown to the reviewer."""


_ENUM_SYNONYMS = {
    "NEGATIVE": {"negative", "negatif", "négatif", "neg", "nég", "-"},
    "POSITIVE": {"positive", "positif", "pos", "+"},
}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    scope: str  # "document" or "encounter"
    kind: str  # digits, code, text, date, gestational_age, decimal, int, bp, enum
    label: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False

    def parse(self, raw: object) -> object:
        """Normalize reviewer input (e.g. '19/12/25', '28SA+1j', '12') into the stored value."""
        text = "" if raw is None else str(raw).strip()
        if not text:
            raise InvalidValue("Valeur vide : saisissez une valeur ou choisissez un statut.")
        value = _PARSERS[self.kind](self, text)
        problem = self.type_error(value) or self.range_error(value)
        if problem:
            raise InvalidValue(problem)
        return value

    def type_error(self, value: object) -> str | None:
        kind = self.kind
        if kind in ("digits", "code", "text"):
            if not isinstance(value, str):
                return "texte attendu"
            pattern = {"digits": r"\d{3,12}", "code": r"[A-Z0-9-]{3,20}", "text": r".{2,80}"}[kind]
            if not re.fullmatch(pattern, value):
                return {
                    "digits": "3 à 12 chiffres attendus",
                    "code": "3 à 20 caractères (lettres, chiffres, tiret)",
                    "text": "2 à 80 caractères",
                }[kind]
            return None
        if kind == "date":
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return "date ISO AAAA-MM-JJ attendue"
            try:
                date.fromisoformat(value)
            except ValueError:
                return "date inexistante"
            return None
        if kind in ("int", "bp", "gestational_age"):
            return None if isinstance(value, int) and not isinstance(value, bool) else "nombre entier attendu"
        if kind == "decimal":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return None if ok else "nombre attendu"
        if kind == "enum":
            return None if value in _ENUM_SYNONYMS else "négatif ou positif attendu"
        return f"type inconnu {kind}"

    def range_error(self, value: object) -> str | None:
        if self.kind == "date":
            if date.fromisoformat(value) > date.today():
                return "date dans le futur"
            return None
        if self.minimum is not None and value < self.minimum:
            return f"valeur trop basse (min {self.minimum:g})"
        if self.maximum is not None and value > self.maximum:
            return f"valeur trop haute (max {self.maximum:g})"
        return None


def _parse_digits(spec: FieldSpec, text: str) -> str:
    return re.sub(r"[\s./-]", "", text)


def _parse_code(spec: FieldSpec, text: str) -> str:
    text = re.sub(r"^CM\s*[:\-]?\s*", "", text.upper())
    return re.sub(r"\s", "", text)


def _parse_text(spec: FieldSpec, text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _parse_date(spec: FieldSpec, text: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        candidate = text
    else:
        match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", text)
        if not match:
            raise InvalidValue("Format de date attendu : jj/mm/aaaa.")
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        candidate = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    try:
        date.fromisoformat(candidate)
    except ValueError:
        raise InvalidValue("Cette date n'existe pas.") from None
    return candidate


def _number(text: str, unit: str | None) -> float:
    cleaned = text.lower().replace(",", ".")
    if unit:
        cleaned = cleaned.replace(unit.lower(), "")
    cleaned = cleaned.strip()
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise InvalidValue("Nombre attendu.")
    return float(cleaned)


def _parse_decimal(spec: FieldSpec, text: str) -> float | int:
    number = _number(text, spec.unit)
    return int(number) if number.is_integer() else round(number, 1)


def _parse_int(spec: FieldSpec, text: str) -> int:
    number = _number(text, spec.unit)
    if not number.is_integer():
        raise InvalidValue("Nombre entier attendu.")
    return int(number)


def _parse_bp(spec: FieldSpec, text: str) -> int:
    value = _parse_int(spec, text)
    # Moroccan fiches record TA in cmHg ("12/7"): 12 means 120 mmHg.
    return value * 10 if value < 30 else value


def _parse_gestational_age(spec: FieldSpec, text: str) -> int:
    match = re.fullmatch(r"(\d{1,2})\s*(?:sa)?\s*(?:\+\s*(\d)\s*j?)?", text.lower())
    if not match:
        raise InvalidValue("Format attendu : 28SA+1j, 28+1 ou 28 (semaines).")
    weeks, days = int(match.group(1)), int(match.group(2) or 0)
    if days > 6:
        raise InvalidValue("Les jours doivent être entre 0 et 6.")
    return weeks * 7 + days


def _parse_enum(spec: FieldSpec, text: str) -> str:
    lowered = text.lower()
    for value, synonyms in _ENUM_SYNONYMS.items():
        if lowered in synonyms or lowered == value.lower():
            return value
    raise InvalidValue("Réponse attendue : négatif ou positif.")


_PARSERS = {
    "digits": _parse_digits,
    "code": _parse_code,
    "text": _parse_text,
    "date": _parse_date,
    "decimal": _parse_decimal,
    "int": _parse_int,
    "bp": _parse_bp,
    "gestational_age": _parse_gestational_age,
    "enum": _parse_enum,
}

DOCUMENT_FIELDS = (
    FieldSpec("registry_file_number", "document", "digits", "N° de la fiche"),
    FieldSpec("midwife_patient_code", "document", "code", "Code patiente (sage-femme)"),
    FieldSpec("facility_name", "document", "text", "Établissement"),
    FieldSpec("last_menstrual_period", "document", "date", "DDR (dernières règles)"),
)
ENCOUNTER_FIELDS = (
    FieldSpec("visit_date", "encounter", "date", "Date de visite", required=True),
    FieldSpec("gestational_age_days", "encounter", "gestational_age", "Âge gestationnel", "days", 0, 315),
    FieldSpec("weight_kg", "encounter", "decimal", "Poids", "kg", 30, 200),
    FieldSpec("systolic_bp_mmhg", "encounter", "bp", "TA systolique", "mmHg", 60, 250),
    FieldSpec("diastolic_bp_mmhg", "encounter", "bp", "TA diastolique", "mmHg", 30, 150),
    FieldSpec("fundal_height_cm", "encounter", "int", "Hauteur utérine", "cm", 5, 50),
    FieldSpec("syphilis_test", "encounter", "enum", "Syphilis (TPHA/VDRL)"),
    FieldSpec("hiv_test", "encounter", "enum", "Sérologie VIH"),
)
FIELDS = {spec.name: spec for spec in DOCUMENT_FIELDS + ENCOUNTER_FIELDS}
KEY_FIELDS = ("registry_file_number", "midwife_patient_code")


def catalog() -> dict:
    """Field metadata for clients (labels, units, kinds)."""
    return {
        "fields": {
            spec.name: {
                "scope": spec.scope, "kind": spec.kind, "label": spec.label,
                "unit": spec.unit, "required": spec.required,
            }
            for spec in FIELDS.values()
        },
        "document_fields": [spec.name for spec in DOCUMENT_FIELDS],
        "encounter_fields": [spec.name for spec in ENCOUNTER_FIELDS],
        "slots": SLOTS,
        "key_fields": list(KEY_FIELDS),
    }


def iter_fields(draft: dict):
    """Yield (scope, encounter_index, slot, name, field_value) in review order."""
    for name in (spec.name for spec in DOCUMENT_FIELDS):
        yield "document", None, None, name, draft["document_fields"][name]
    for index, encounter in enumerate(draft["encounters"]):
        for name in (spec.name for spec in ENCOUNTER_FIELDS):
            yield "encounter", index, encounter["slot"], name, encounter["fields"][name]


def get_field(draft: dict, scope: str, name: str, encounter_index: int | None) -> dict:
    if scope == "document":
        return draft["document_fields"][name]
    if not isinstance(encounter_index, int) or not 0 <= encounter_index < len(draft["encounters"]):
        raise KeyError("encounter_index")
    return draft["encounters"][encounter_index]["fields"][name]


def blocking_fields(draft: dict) -> list[dict]:
    """Fields that must be resolved by a reviewer before registration."""
    blocking = []
    for scope, index, slot, name, fv in iter_fields(draft):
        reason = None
        if FIELDS[name].required and fv["value"] is None:
            reason = "REQUIRED_MISSING"
        elif fv["verification"]["state"] == "UNVERIFIED" and fv["field_status"] in ("NEEDS_REVIEW", "ILLEGIBLE"):
            reason = fv["field_status"]
        if reason:
            blocking.append({"scope": scope, "encounter_index": index, "slot": slot, "field": name, "reason": reason})

    seen: dict[str, int] = {}
    for index, encounter in enumerate(draft["encounters"]):
        visit_date = encounter["fields"]["visit_date"]["value"]
        if visit_date is None:
            continue
        if visit_date in seen:
            blocking.append({
                "scope": "encounter", "encounter_index": index, "slot": encounter["slot"],
                "field": "visit_date", "reason": "DUPLICATE_ENCOUNTER_DATE",
            })
        seen.setdefault(visit_date, index)
    return blocking


def empty_field(spec: FieldSpec) -> dict:
    return {
        "raw_text": None, "value": None, "unit": spec.unit, "confidence": None,
        "field_status": "NEEDS_REVIEW", "validation_flags": ["MANUAL_ENTRY"], "source": None,
        "verification": {"state": "UNVERIFIED", "by": None, "at": None}, "corrections": [],
    }


def manual_draft(page_refs: list[str], processed_at: str) -> dict:
    """Blank draft for full manual entry when extraction is unavailable."""
    return {
        "schema_version": SCHEMA_VERSION,
        "layout_id": LAYOUT_ID,
        "extraction": {"extractor": "manual", "extractor_version": "manual-1", "processed_at": processed_at},
        "pages": [{"page_ref": ref, "section": "unknown"} for ref in page_refs],
        "pii_detected": [],
        "document_fields": {spec.name: empty_field(spec) for spec in DOCUMENT_FIELDS},
        "encounters": [{
            "encounter_type": "ANTENATAL",
            "slot": "MANUAL",
            "fields": {spec.name: empty_field(spec) for spec in ENCOUNTER_FIELDS},
        }],
    }


def validate_draft(draft: object) -> list[str]:
    """Return schema violations; an empty list means the draft follows docs/schema.md."""
    errors: list[str] = []

    def err(path: str, message: str) -> None:
        errors.append(f"{path}: {message}")

    if not isinstance(draft, dict):
        return ["$: object expected"]
    for key in sorted(set(draft) - DRAFT_KEYS):
        err(key, "unknown key")
    if draft.get("schema_version") != SCHEMA_VERSION:
        err("schema_version", f"expected {SCHEMA_VERSION}")
    if draft.get("layout_id") != LAYOUT_ID:
        err("layout_id", f"expected {LAYOUT_ID}")
    if "notes" in draft and not isinstance(draft["notes"], str):
        err("notes", "string expected")

    extraction = draft.get("extraction")
    if not isinstance(extraction, dict):
        err("extraction", "object expected")
    else:
        for key in ("extractor", "extractor_version"):
            if not isinstance(extraction.get(key), str) or not extraction.get(key):
                err(f"extraction.{key}", "non-empty string expected")
        if extraction.get("processed_at") is not None and not isinstance(extraction["processed_at"], str):
            err("extraction.processed_at", "string or null expected")

    pages = draft.get("pages")
    if not isinstance(pages, list) or not pages:
        err("pages", "non-empty list expected")
    else:
        for i, page in enumerate(pages):
            if not isinstance(page, dict) or not isinstance(page.get("page_ref"), str):
                err(f"pages[{i}]", "object with page_ref expected")

    pii = draft.get("pii_detected")
    if not isinstance(pii, list):
        err("pii_detected", "list expected")
    else:
        for i, item in enumerate(pii):
            if not isinstance(item, dict) or item.get("action") not in PII_ACTIONS:
                err(f"pii_detected[{i}]", f"action must be one of {PII_ACTIONS}")

    doc_fields = draft.get("document_fields")
    if not isinstance(doc_fields, dict):
        err("document_fields", "object expected")
    else:
        _validate_field_set("document_fields", doc_fields, DOCUMENT_FIELDS, err)

    encounters = draft.get("encounters")
    if not isinstance(encounters, list):
        err("encounters", "list expected")
    else:
        for i, encounter in enumerate(encounters):
            path = f"encounters[{i}]"
            if not isinstance(encounter, dict):
                err(path, "object expected")
                continue
            if encounter.get("encounter_type") not in ENCOUNTER_TYPES:
                err(f"{path}.encounter_type", f"one of {ENCOUNTER_TYPES}")
            if encounter.get("slot") not in SLOTS:
                err(f"{path}.slot", "unknown slot")
            if not isinstance(encounter.get("fields"), dict):
                err(f"{path}.fields", "object expected")
            else:
                _validate_field_set(f"{path}.fields", encounter["fields"], ENCOUNTER_FIELDS, err)
    return errors


def _validate_field_set(path: str, fields: dict, specs: tuple[FieldSpec, ...], err) -> None:
    expected = {spec.name for spec in specs}
    for name in sorted(expected - set(fields)):
        err(f"{path}.{name}", "missing field")
    for name in sorted(set(fields) - expected):
        err(f"{path}.{name}", "unknown field")
    for spec in specs:
        if spec.name in fields:
            _validate_field(f"{path}.{spec.name}", spec, fields[spec.name], err)


def _validate_field(path: str, spec: FieldSpec, fv: object, err) -> None:
    if not isinstance(fv, dict):
        err(path, "field value object expected")
        return
    missing, extra = FIELD_VALUE_KEYS - set(fv), set(fv) - FIELD_VALUE_KEYS
    if missing or extra:
        err(path, f"keys mismatch (missing {sorted(missing)}, unknown {sorted(extra)})")
        return

    status, value, confidence = fv["field_status"], fv["value"], fv["confidence"]
    if status not in FIELD_STATUSES:
        err(path, f"field_status must be one of {FIELD_STATUSES}")
        return
    if confidence is not None and (
        not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1
    ):
        err(path, "confidence must be null or within [0, 1]")
    if fv["raw_text"] is not None and not isinstance(fv["raw_text"], str):
        err(path, "raw_text must be string or null")
    if fv["unit"] != spec.unit:
        err(path, f"unit must be {spec.unit!r}")
    if not isinstance(fv["validation_flags"], list) or not all(isinstance(f, str) for f in fv["validation_flags"]):
        err(path, "validation_flags must be a list of strings")
    if fv["source"] is not None and not (isinstance(fv["source"], dict) and "page_ref" in fv["source"]):
        err(path, "source must be null or an object with page_ref")
    verification = fv["verification"]
    if not isinstance(verification, dict) or verification.get("state") not in VERIFICATION_STATES:
        err(path, f"verification.state must be one of {VERIFICATION_STATES}")
        return
    if not isinstance(fv["corrections"], list):
        err(path, "corrections must be a list")

    if status in MISSING_STATUSES and value is not None:
        err(path, f"value must be null when status is {status}")
    if status == "KNOWN" and value is None:
        err(path, "KNOWN requires a value")
    if value is not None:
        problem = spec.type_error(value)
        if problem:
            err(path, f"invalid value ({problem})")
        elif status == "KNOWN" and spec.range_error(value):
            err(path, f"out of range ({spec.range_error(value)})")
    if (
        status == "KNOWN" and verification["state"] == "UNVERIFIED"
        and confidence is not None and confidence < KNOWN_CONFIDENCE_THRESHOLD
    ):
        err(path, f"confidence below {KNOWN_CONFIDENCE_THRESHOLD} must be NEEDS_REVIEW")
