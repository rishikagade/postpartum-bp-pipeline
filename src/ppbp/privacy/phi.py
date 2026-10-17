"""Phase 5b: build the restricted contact table used by the nurse's logged lookup (D32).

    python -m ppbp.privacy.phi

Copies the minimum needed to reach a cohort patient (display name and phone) from the raw FHIR
Patient into phi.patient_contact, keyed by study ID. Nothing else about the person is copied.
Rebuilt in full on each run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ppbp import DB_PATH
from ppbp.db import connect


def display_name(patient: dict[str, Any]) -> str:
    names = patient.get("name", [])
    name = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    parts = [*name.get("prefix", [])[:1], *name.get("given", []), name.get("family", "")]
    return " ".join(p for p in parts if p) or "(no name on record)"


def phone(patient: dict[str, Any]) -> str | None:
    return next((t.get("value") for t in patient.get("telecom", []) if t.get("system") == "phone"), None)


def build_contacts(db_path: Path = DB_PATH) -> int:
    with connect(db_path) as con:
        rows = con.execute("""
            SELECT k.study_id, v.resource_json, v.content_hash
            FROM cohort.postpartum_cohort c
            JOIN phi.patient_key k ON k.study_id = c.patient_id
            JOIN raw.fhir_resource_current cur ON cur.resource_type = 'Patient' AND cur.resource_id = k.patient_id
            JOIN raw.fhir_resource_version v USING (resource_type, resource_id, content_hash)""").fetchall()
        contacts = [(sid, display_name(json.loads(j)), phone(json.loads(j)), h) for sid, j, h in rows]
        con.begin()
        try:
            con.execute("DELETE FROM phi.patient_contact")
            if contacts:
                con.executemany("INSERT INTO phi.patient_contact VALUES (?, ?, ?, ?)", contacts)
            con.commit()
        except Exception:
            con.rollback()
            raise
    return len(contacts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    print(f"== phi.patient_contact: {build_contacts(args.db)} cohort patients (display name and phone only)")


if __name__ == "__main__":
    main()
