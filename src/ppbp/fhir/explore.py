"""Phase 1: explore the Synthea FHIR R4 bundles.

    python -m ppbp.fhir.explore                 # resource summary, examples, BP structure
    python -m ppbp.fhir.explore --patient <id>  # plain-language timeline of one patient

Read-only and standard library only; nothing is written to disk.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ppbp import FHIR_DIR

Resource = dict[str, Any]

FOCUS_TYPES = ("Patient", "Condition", "Observation", "Encounter")

LOINC_BP_PANEL = "85354-9"
LOINC_SYSTOLIC = "8480-6"
LOINC_DIASTOLIC = "8462-4"

# SNOMED codes seen in this dataset that matter for the postpartum cohort.
DELIVERY_PROCEDURES = {"66348005", "11466000", "236974004", "177157003"}
HDP_CONDITIONS = {"398254007", "198992004"}  # Preeclampsia, Antepartum eclampsia
PREGNANCY_ENCOUNTER_TYPES = {"424441002", "424619006", "183460006", "169762003"}


def iter_bundles(fhir_dir: Path) -> Iterator[tuple[Path, Resource]]:
    for path in sorted(fhir_dir.glob("*.json")):
        with path.open() as fh:
            yield path, json.load(fh)


def resources_in(bundle: Resource) -> list[Resource]:
    return [entry["resource"] for entry in bundle.get("entry", [])]


def first_code(concept: Resource) -> tuple[str, str]:
    coding = concept["coding"][0]
    return coding["code"], coding.get("display", "")


def is_bp_panel(resource: Resource) -> bool:
    return resource["resourceType"] == "Observation" and any(
        c.get("code") == LOINC_BP_PANEL for c in resource.get("code", {}).get("coding", [])
    )


def bp_components(observation: Resource) -> dict[str, Resource]:
    """Map LOINC code -> valueQuantity for each component of a BP panel."""
    out: dict[str, Resource] = {}
    for comp in observation.get("component", []):
        code, _ = first_code(comp["code"])
        out[code] = comp.get("valueQuantity", {})
    return out


# --- Dataset summary -------------------------------------------------------


@dataclass
class BpProfile:
    panels: int = 0
    patients: set[str] = field(default_factory=set)
    component_sets: Counter = field(default_factory=Counter)
    units: Counter = field(default_factory=Counter)
    systolic: list[float] = field(default_factory=list)
    diastolic: list[float] = field(default_factory=list)
    with_encounter: int = 0
    standalone_systolic: int = 0  # 8480-6 as its own Observation, outside a panel

    def add(self, obs: Resource) -> None:
        self.panels += 1
        self.patients.add(obs["subject"]["reference"])
        comps = bp_components(obs)
        self.component_sets[tuple(sorted(comps))] += 1
        for code, qty in comps.items():
            self.units[qty.get("code")] += 1
            if code == LOINC_SYSTOLIC:
                self.systolic.append(qty["value"])
            elif code == LOINC_DIASTOLIC:
                self.diastolic.append(qty["value"])
        if "encounter" in obs:
            self.with_encounter += 1


def summarize(fhir_dir: Path) -> None:
    files = 0
    bundle_kinds: Counter = Counter()
    types: Counter = Counter()
    examples: dict[str, Resource] = {}
    bp = BpProfile()

    for _, bundle in iter_bundles(fhir_dir):
        files += 1
        bundle_kinds[bundle.get("type")] += 1
        for res in resources_in(bundle):
            rtype = res["resourceType"]
            types[rtype] += 1
            if is_bp_panel(res):
                bp.add(res)
                examples.setdefault("Observation", res)  # show a BP panel as the example
            elif rtype in FOCUS_TYPES and rtype != "Observation":
                examples.setdefault(rtype, res)
            elif (
                rtype == "Observation"
                and first_code(res["code"])[0] == LOINC_SYSTOLIC
            ):
                bp.standalone_systolic += 1

    print(f"== Files: {files}  bundle types: {dict(bundle_kinds)}")
    print("   (one 'transaction' bundle per patient; 2 'batch' bundles hold shared")
    print("    hospital and practitioner records)\n")

    print("== Resource counts (focus types first)")
    for rtype in FOCUS_TYPES:
        print(f"   {rtype:<22} {types[rtype]:>8,}")
    for rtype, n in types.most_common():
        if rtype not in FOCUS_TYPES:
            print(f"   {rtype:<22} {n:>8,}")

    for rtype in FOCUS_TYPES:
        print(f"\n== Example {rtype}")
        print(json.dumps(examples[rtype], indent=2))

    print("\n== Blood pressure observations (LOINC 85354-9 panel)")
    print(f"   panels:                     {bp.panels:,}")
    print(f"   patients with >=1 panel:    {len(bp.patients):,}")
    print(f"   panels linked to encounter: {bp.with_encounter:,}")
    print(f"   component combinations:     {dict(bp.component_sets)}")
    print(f"   units used:                 {dict(bp.units)}")
    print(f"   systolic range:             {min(bp.systolic)} - {max(bp.systolic)}")
    print(f"   diastolic range:            {min(bp.diastolic)} - {max(bp.diastolic)}")
    print(f"   standalone 8480-6 obs:      {bp.standalone_systolic:,}")


# --- One patient, in plain language ---------------------------------------


def find_patient_file(fhir_dir: Path, patient_id: str) -> Path:
    matches = list(fhir_dir.glob(f"*{patient_id}.json"))
    if len(matches) != 1:
        raise SystemExit(f"Expected one bundle for patient {patient_id}, found {len(matches)}")
    return matches[0]


def walkthrough(fhir_dir: Path, patient_id: str) -> None:
    path = find_patient_file(fhir_dir, patient_id)
    resources = resources_in(json.loads(path.read_text()))
    patient = next(r for r in resources if r["resourceType"] == "Patient")
    encounters = {r["id"]: r for r in resources if r["resourceType"] == "Encounter"}

    print(f"== Bundle: {path.name}  ({len(resources)} resources)")
    print(f"   {dict(Counter(r['resourceType'] for r in resources).most_common())}\n")

    print("== Patient")
    print(f"   id:         {patient['id']}")
    print(f"   gender:     {patient.get('gender')}   born: {patient.get('birthDate')}")
    print(f"   deceased:   {patient.get('deceasedDateTime', 'no')}")
    print(f"   identifying fields present: {sorted(k for k in ('name', 'address', 'telecom') if k in patient)}\n")

    print("== Conditions (diagnoses), by onset date")
    conditions = sorted(
        (r for r in resources if r["resourceType"] == "Condition"),
        key=lambda r: r.get("onsetDateTime", ""),
    )
    for c in conditions:
        code, display = first_code(c["code"])
        status = first_code(c["clinicalStatus"])[0]
        ended = c.get("abatementDateTime", "")[:10]
        flag = "  <-- hypertensive disorder of pregnancy" if code in HDP_CONDITIONS else ""
        print(f"   {c.get('onsetDateTime', '')[:10]}  {display:<45} {status:<9} {('ended ' + ended) if ended else ''}{flag}")

    print("\n== Pregnancy-related encounters and deliveries")
    events: list[tuple[str, str]] = []
    for e in encounters.values():
        code, display = first_code(e["type"][0])
        if code in PREGNANCY_ENCOUNTER_TYPES:
            events.append((e["period"]["start"], f"Encounter  {display} [{e['class']['code']}] ends {e['period']['end']}"))
    for r in resources:
        if r["resourceType"] == "Procedure" and first_code(r["code"])[0] in DELIVERY_PROCEDURES:
            events.append((r["performedPeriod"]["start"], f"Procedure  {first_code(r['code'])[1]}"))
    for when, text in sorted(events):
        print(f"   {when[:16]}  {text}")

    print("\n== Blood pressure panels (systolic/diastolic mmHg)")
    for obs in sorted(filter(is_bp_panel, resources), key=lambda r: r["effectiveDateTime"]):
        comps = bp_components(obs)
        enc_id = obs["encounter"]["reference"].removeprefix("urn:uuid:")
        enc_type = first_code(encounters[enc_id]["type"][0])[1] if enc_id in encounters else "?"
        print(
            f"   {obs['effectiveDateTime'][:16]}  "
            f"{comps[LOINC_SYSTOLIC]['value']:>5}/{comps[LOINC_DIASTOLIC]['value']:<5}  during: {enc_type}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fhir-dir", type=Path, default=FHIR_DIR)
    parser.add_argument("--patient", help="Synthea patient UUID for a plain-language walkthrough")
    args = parser.parse_args()
    if args.patient:
        walkthrough(args.fhir_dir, args.patient)
    else:
        summarize(args.fhir_dir)


if __name__ == "__main__":
    main()
