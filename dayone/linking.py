"""Facility-scoped patient candidates and visit matching (see docs/identity-strategy.md)."""

from __future__ import annotations

import json
import sqlite3

from .schema import KEY_FIELDS


def find_candidates(db: sqlite3.Connection, facility_id: str, draft: dict) -> list[dict]:
    fields = draft["document_fields"]
    draft_keys = {key: fields[key]["value"] for key in KEY_FIELDS if fields[key]["value"] is not None}
    if not draft_keys:
        return []

    patient_ids: set[str] = set()
    for key, value in draft_keys.items():
        rows = db.execute(
            f"SELECT patient_id FROM patients WHERE facility_id = ? AND {key} = ?",  # key from KEY_FIELDS
            (facility_id, value),
        )
        patient_ids.update(row["patient_id"] for row in rows)

    candidates = []
    for patient_id in sorted(patient_ids):
        patient = db.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
        matched = [key for key, value in draft_keys.items() if patient[key] == value]
        conflicting = [key for key, value in draft_keys.items() if patient[key] is not None and patient[key] != value]
        stats = db.execute(
            "SELECT COUNT(*) AS visits, MAX(visit_date) AS last_visit FROM visits WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        candidates.append({
            "patient_id": patient_id,
            "keys": {key: patient[key] for key in KEY_FIELDS},
            "matched_keys": matched,
            "conflicting_keys": conflicting,
            "strength": "CONFLICT" if conflicting else "STRONG",
            "flags": ["KEY_MISMATCH"] if conflicting else [],
            "visit_count": stats["visits"],
            "last_visit_date": stats["last_visit"],
        })

    if len(candidates) > 1:
        for candidate in candidates:
            candidate["strength"] = "CONFLICT"
            candidate["flags"].append("MULTIPLE_CANDIDATES")
    return candidates


def suggested_patient(candidates: list[dict], draft: dict) -> str | None:
    """Auto-link suggestion: one STRONG candidate and every present key read as KNOWN."""
    strong = [c for c in candidates if c["strength"] == "STRONG"]
    if len(strong) != 1:
        return None
    fields = draft["document_fields"]
    present = [key for key in KEY_FIELDS if fields[key]["value"] is not None]
    if not all(fields[key]["field_status"] == "KNOWN" for key in present):
        return None
    return strong[0]["patient_id"]


def selection_warnings(selection: dict | None, candidates: list[dict]) -> list[str]:
    if not selection or selection["choice"] != "EXISTING":
        return []
    match = next((c for c in candidates if c["patient_id"] == selection["patient_id"]), None)
    if match is None:
        return ["NO_KEY_MATCH"]
    return list(match["flags"])


def encounter_matches(db: sqlite3.Connection, patient_id: str | None, draft: dict) -> list[dict]:
    """Classify each encounter as NEW, EXISTS_SAME or EXISTS_DIFFERENT for the given patient."""
    results = []
    for index, encounter in enumerate(draft["encounters"]):
        fields = encounter["fields"]
        visit_date = fields["visit_date"]["value"]
        result = {
            "encounter_index": index, "slot": encounter["slot"], "visit_date": visit_date,
            "outcome": "NEW", "existing_visit_id": None, "diffs": [], "flags": [],
        }
        if visit_date is None:
            result["outcome"] = "MISSING_DATE"
        elif patient_id is not None:
            row = db.execute(
                "SELECT visit_id, slot, fields_json FROM visits "
                "WHERE patient_id = ? AND encounter_type = ? AND visit_date = ?",
                (patient_id, encounter["encounter_type"], visit_date),
            ).fetchone()
            if row is not None:
                existing = json.loads(row["fields_json"])
                diffs = [
                    {"field": name, "existing": existing.get(name, {}).get("value"), "draft": fv["value"]}
                    for name, fv in fields.items()
                    if name != "visit_date" and fv["value"] is not None
                    and existing.get(name, {}).get("value") != fv["value"]
                ]
                result.update(
                    outcome="EXISTS_DIFFERENT" if diffs else "EXISTS_SAME",
                    existing_visit_id=row["visit_id"],
                    diffs=diffs,
                    flags=["SLOT_DIFFERS"] if row["slot"] != encounter["slot"] else [],
                )
        results.append(result)
    return results
