"""Paper values beyond the 12 v1.0 fields, grouped by scope (docs/schema.md, docs/excel-field-mapping.md).

Only values read directly from one page are here. Nothing is derived across visits or pages, converted to a CSV
encoding, or filled in: pregnancy-level export columns are declared in dayone/export_columns.py and not computed.
A field is present when a page of its source type was read; an absent field or section was not captured.
No identifier is stored here: list items are numbered by their position on the page, never by CIN or name.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import schema
from .schema import FieldSpec

CATALOG_VERSION = "excel-ext-1"

# A checkbox is stored as what the page shows. Whether an empty box means "no" is decision D5, so UNMARKED is
# never turned into a yes/no answer here.
MARK = (("MARKED", ("case cochée", "cochée", "x")), ("UNMARKED", ("case vide", "vide", "non cochée")))


@dataclass(frozen=True)
class Source:
    page: str  # pages[].section the value is read from
    label: str  # printed label on the specimen layout (decision D7: specimen pages only)
    note: str = ""


@dataclass(frozen=True)
class Section:
    name: str
    scope: str
    label: str
    item_key: str | None  # None: one object of fields; otherwise a list of {item_key: ..., "fields": {...}}
    item_keys: tuple = ()
    fields: tuple[FieldSpec, ...] = ()


def _spec(name, scope, kind, label, unit=None, minimum=None, maximum=None, **extra) -> FieldSpec:
    return FieldSpec(name, scope, kind, label, unit, minimum, maximum, **extra)


SECTIONS = {s.name: s for s in (
    Section("patient", "patient", "Patiente", None, fields=(
        _spec("education_level_text", "patient", "text", "Niveau d'instruction (tel qu'écrit)"),
    )),
    Section("pregnancy", "pregnancy", "Grossesse", None, fields=(
        _spec("maternal_age_years", "pregnancy", "int", "Âge de la femme", "years", 10, 60),
        _spec("gravidity", "pregnancy", "int", "Gestation", None, 0, 20),
        _spec("parity", "pregnancy", "int", "Parité", None, 0, 20),
        _spec("living_children_count", "pregnancy", "int", "Nombre d'enfants vivants", None, 0, 20),
        _spec("abortions_count", "pregnancy", "int", "Avortements (nombre)", None, 0, 20),
        _spec("consanguinity_mark", "pregnancy", "choice", "Case « Consanguinité »", choices=MARK),
        _spec("pregnancy_desired_mark", "pregnancy", "choice", "Case « Grossesse désirée »", choices=MARK),
        _spec("height_cm", "pregnancy", "int", "Taille de la femme", "cm", 120, 200),
    )),
    Section("previous_deliveries", "previous_delivery", "Accouchements antérieurs", "column", (1, 2, 3, 4, 5), (
        _spec("previous_delivery_date", "previous_delivery", "date", "Date de l'accouchement antérieur"),
        _spec("previous_delivery_mode_text", "previous_delivery", "text", "Modalité d'extraction (telle qu'écrite)"),
    )),
    Section("visit_labs", "visit", "Examens biologiques par visite", "slot",
            tuple(slot for slot in schema.SLOTS if slot != "MANUAL"), (
        _spec("hemoglobin_g_dl", "visit", "decimal", "Hémoglobine", "g/dL", 3, 25),
        _spec("blood_glucose_g_l", "visit", "decimal", "Bilan glycémique (à jeun non précisé)", "g/L", 0.2, 6,
              decimals=2),
        _spec("albuminuria", "visit", "enum", "Albuminurie"),
    )),
    Section("delivery", "delivery", "Accouchement", None, fields=(
        _spec("delivery_date", "delivery", "date", "Date de l'accouchement"),
        _spec("delivery_gestational_age_days", "delivery", "gestational_age", "Âge gestationnel à la naissance",
              "days", 140, 315),
        _spec("delivery_mode", "delivery", "choice", "Mode de l'accouchement", choices=(
            ("VAGINAL_NON_INSTRUMENTAL", ("voie basse non instrumentale",)),
            ("VAGINAL_INSTRUMENTAL", ("voie basse instrumentale",)),
            ("CESAREAN_PLANNED", ("césarienne programmée",)),
            ("CESAREAN_EMERGENCY", ("césarienne en urgence", "urgence")),
        )),
    )),
    Section("newborns", "newborn", "Nouveau-né(s)", "index", tuple(range(1, 10)), (
        _spec("newborn_sex", "newborn", "choice", "Sexe du nouveau-né", choices=(
            ("FEMALE", ("F", "féminin", "fille")), ("MALE", ("M", "masculin", "garçon")),
        )),
        _spec("birth_weight_g", "newborn", "int", "Poids à la naissance", "g", 300, 7000),
        _spec("birth_head_circumference_cm", "newborn", "decimal", "Périmètre crânien à la naissance", "cm", 20, 50),
    )),
    Section("newborn_consultations", "newborn_consultation", "Consultations post-partum du nouveau-né", "period",
            ("EARLY", "LATE"), (
        _spec("consultation_date", "newborn_consultation", "date", "Date de la consultation"),
        _spec("feeding_mode", "newborn_consultation", "choice", "Allaitement le jour de la consultation", choices=(
            ("EXCLUSIVE_BREASTFEEDING", ("exclusivement au sein", "sein")),
            ("ARTIFICIAL", ("artificiel",)), ("MIXED", ("mixte",)),
        )),
    )),
)}
FIELDS = {spec.name: spec for section in SECTIONS.values() for spec in section.fields}
SECTION_OF = {spec.name: section.name for section in SECTIONS.values() for spec in section.fields}
PERIODS = {"EARLY": "post-partum précoce", "LATE": "post-partum tardif"}

SOURCES = {
    "education_level_text": Source("identification", "Niveau d'instruction :",
                                   "Texte libre ; aucune correspondance avec les 3 niveaux du tableur n'est décidée."),
    "maternal_age_years": Source("identification", "Age :", "Tel qu'écrit ; jamais calculé depuis une date de naissance."),
    "gravidity": Source("identification", "Gestation :", "La fiche ne dit pas si la grossesse en cours est comptée."),
    "parity": Source("identification", "Parité :"),
    "living_children_count": Source("identification", "Nombre d'enfants vivants :"),
    "abortions_count": Source("identification", "Antécédents obstétricaux, ligne « Avortement », colonne « Nombre »",
                              "Case vide : non renseigné, jamais 0."),
    "consanguinity_mark": Source("identification", "Case « Consanguinité »",
                                 "État de la case ; case vide ≠ « non » (décision D5)."),
    "pregnancy_desired_mark": Source("identification", "Case « Grossesse désirée »",
                                     "État de la case ; case vide ≠ « non » (décision D5)."),
    "height_cm": Source("current_pregnancy", "Taille :", "Taille de la femme, pas du nouveau-né."),
    "previous_delivery_date": Source("identification", "Déroulement des accouchements antérieurs, ligne « Date »"),
    "previous_delivery_mode_text": Source("identification",
                                          "Déroulement des accouchements antérieurs, ligne « Modalité d'extraction »",
                                          "Texte libre ; « césarienne antérieure » n'est pas déduit."),
    "hemoglobin_g_dl": Source("current_pregnancy", "Examen biologique, ligne « Hémoglobine »"),
    "blood_glucose_g_l": Source("current_pregnancy", "Examen biologique, ligne « Bilan glycémique »",
                                "La fiche ne dit pas si le prélèvement était à jeun : jamais traité comme glycémie à jeun."),
    "albuminuria": Source("current_pregnancy", "Examen biologique, ligne « Albuminurie »",
                          "Albuminurie, pas protéinurie : l'équivalence n'est pas décidée (D10)."),
    "delivery_date": Source("delivery", "Date de l'accouchement :"),
    "delivery_gestational_age_days": Source("delivery", "État du nouveau-né, « Âge gestationnel : »",
                                            "Semaines + jours convertis en jours ; pas recalculé depuis la DDR."),
    "delivery_mode": Source("delivery", "Mode de l'accouchement (cases)",
                            "Une seule case cochée donne la valeur ; plusieurs ou aucune : à vérifier."),
    "newborn_sex": Source("delivery", "État du nouveau-né, « Sexe : »"),
    "birth_weight_g": Source("delivery", "État du nouveau-né, « Poids à la naissance : »",
                             "Les poids des consultations post-partum ne sont pas des poids de naissance."),
    "birth_head_circumference_cm": Source("delivery", "État du nouveau-né, « Périmètre crânien à la naissance : »"),
    "consultation_date": Source("newborn", "Date de la consultation :"),
    "feeding_mode": Source("newborn", "Allaitement : exclusivement au sein / artificiel / mixte (cases)",
                           "Mode d'allaitement le jour de la consultation ; ne prouve pas l'allaitement "
                           "commencé à la naissance."),
}


def catalog() -> dict:
    return {
        "version": CATALOG_VERSION,
        "sections": {s.name: {"scope": s.scope, "label": s.label, "item_key": s.item_key,
                              "item_keys": list(s.item_keys), "fields": [f.name for f in s.fields]}
                     for s in SECTIONS.values()},
        "fields": {spec.name: {"scope": spec.scope, "kind": spec.kind, "label": spec.label, "unit": spec.unit,
                               "required": False, "choices": [value for value, _spellings in spec.choices],
                               "source": {"page": SOURCES[spec.name].page, "label": SOURCES[spec.name].label,
                                          "note": SOURCES[spec.name].note}}
                   for spec in FIELDS.values()},
        "periods": PERIODS,
    }


def empty() -> dict:
    return {"catalog_version": CATALOG_VERSION}


def validate(ext: object, err) -> None:
    if not isinstance(ext, dict):
        err("extended", "object expected")
        return
    if ext.get("catalog_version") != CATALOG_VERSION:
        err("extended.catalog_version", f"expected {CATALOG_VERSION}")
    for key in sorted(set(ext) - {"catalog_version"} - set(SECTIONS)):
        err(f"extended.{key}", "unknown section")
    for name, section in SECTIONS.items():
        if name not in ext:
            continue
        path = f"extended.{name}"
        if section.item_key is None:
            _validate_fields(path, ext[name], section, err)
            continue
        items = ext[name]
        if not isinstance(items, list):
            err(path, "list expected")
            continue
        seen = set()
        for i, item in enumerate(items):
            item_path = f"{path}[{i}]"
            if not isinstance(item, dict) or set(item) != {section.item_key, "fields"}:
                err(item_path, f"object with {section.item_key} and fields expected")
                continue
            key = item[section.item_key]
            if key not in section.item_keys:
                err(f"{item_path}.{section.item_key}", "unknown value")
            elif key in seen:
                err(f"{item_path}.{section.item_key}", "duplicate")
            seen.add(key)
            _validate_fields(f"{item_path}.fields", item["fields"], section, err)


def _validate_fields(path: str, fields: object, section: Section, err) -> None:
    if not isinstance(fields, dict):
        err(path, "object expected")
        return
    specs = {spec.name: spec for spec in section.fields}
    for name in sorted(set(fields) - set(specs)):
        err(f"{path}.{name}", "unknown field")
    for name, fv in fields.items():
        if name in specs:
            schema._validate_field(f"{path}.{name}", specs[name], fv, err)


def iter_fields(draft: dict):
    """Yield (section, item_index, item_key, name, field_value) in catalog order."""
    ext = draft.get("extended") or {}
    for name, section in SECTIONS.items():
        if name not in ext:
            continue
        if section.item_key is None:
            items = [(None, None, ext[name])]
        else:
            items = [(i, item[section.item_key], item["fields"]) for i, item in enumerate(ext[name])]
        for index, key, fields in items:
            for spec in section.fields:
                if spec.name in fields:
                    yield name, index, key, spec.name, fields[spec.name]


def get_field(draft: dict, section: str, item_index: int | None, name: str) -> dict:
    """KeyError when the section, item or field is not in this draft."""
    if SECTION_OF.get(name) != section:
        raise KeyError(name)
    data = (draft.get("extended") or {})[section]
    if SECTIONS[section].item_key is not None:
        if not isinstance(item_index, int) or not 0 <= item_index < len(data):
            raise KeyError("item_index")
        data = data[item_index]["fields"]
    return data[name]
