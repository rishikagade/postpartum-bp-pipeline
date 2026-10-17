"""Phase 3: build the postpartum cohort from the raw FHIR store.

    python -m ppbp.cohort.build

Steps (DECISIONS D3, D4, D12):
1. Anchor each patient on their most recent delivery Procedure. Discharge is the end of
   that Procedure's Encounter.
2. Exclude patients who died before discharge. Later deaths stay in, with `deceased_at` (D15).
3. Native HDP: a Preeclampsia / Antepartum eclampsia Condition with onset from
   `hdp_lookback_days` before delivery up to discharge. An onset before that window sets
   `prior_hdp`, which is recorded but never used for selection (D14).
4. Augmented HDP: a seeded fraction of the remaining eligible patients get a new Condition
   tagged as synthetic. It is written to its own bundle and loaded through the normal FHIR
   ingest, so it has the same lineage as Synthea data. Synthea files are never edited.
5. Write cohort.postpartum_cohort and cohort.exclusion. Every delivery patient ends up in
   exactly one of them.

Deterministic: the same raw store and config give the same bundle bytes and the same cohort.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from ppbp import DATA_DIR, DB_PATH, PROJECT_ROOT
from ppbp.db import connect
from ppbp.fhir.ingest import canonical_hash, run_load
from ppbp.privacy.pseudonym import ensure_study_ids

Resource = dict[str, Any]

CONFIG_PATH = PROJECT_ROOT / "config" / "cohort.toml"
AUGMENTATION_BUNDLE = DATA_DIR / "generated" / "cohort_augmentation.json"

AUGMENTATION_TAG = {
    "system": "urn:ppbp:provenance",
    "code": "synthetic-cohort-augmentation",
    "display": "Synthetic cohort augmentation (not from Synthea)",
}
# Fixed namespace, so an augmented Condition keeps the same id on every run.
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:ppbp:cohort-augmentation")


@dataclass(frozen=True)
class CohortConfig:
    version: str
    sha256: str
    delivery_codes: list[str]
    native_hdp_codes: list[str]
    hdp_lookback_days: int
    seed: int
    fraction: float
    augmentation_code: str
    augmentation_display: str
    onset_days_before_delivery: tuple[int, int]


def load_config(path: Path = CONFIG_PATH) -> CohortConfig:
    data = path.read_bytes()
    cfg = tomllib.loads(data.decode())
    aug = cfg["augmentation"]
    if not 0 <= aug["fraction"] <= 1:
        raise ValueError(f"augmentation.fraction must be between 0 and 1, got {aug['fraction']}")
    lo, hi = aug["onset_days_before_delivery"]
    return CohortConfig(
        version=cfg["version"],
        sha256=hashlib.sha256(data).hexdigest(),
        delivery_codes=cfg["delivery_codes"],
        native_hdp_codes=cfg["native_hdp_codes"],
        hdp_lookback_days=cfg["hdp_lookback_days"],
        seed=aug["seed"],
        fraction=aug["fraction"],
        augmentation_code=aug["code"],
        augmentation_display=aug["display"],
        onset_days_before_delivery=(lo, hi),
    )


# --- Reading the raw store -------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    patient_id: str
    procedure_id: str
    delivery_code: str
    encounter_id: str
    delivery_start: datetime          # naive UTC, like every datetime in this module
    discharge_at: datetime | None
    deceased_at: datetime | None
    native_hdp: tuple[str, str, datetime] | None  # (condition_id, code, onset)
    prior_hdp: bool                                # HDP before the lookback window (D14)


# References look like "urn:uuid:<id>" in Synthea, or "Patient/<id>" elsewhere.
REF_ID = r"'([^/:]+)$'"

DELIVERIES_SQL = f"""
WITH r AS (
    SELECT v.resource_type, v.resource_id, v.resource_json AS j
    FROM raw.fhir_resource_current c
    JOIN raw.fhir_resource_version v USING (resource_type, resource_id, content_hash)
),
latest AS (
    SELECT resource_id AS procedure_id,
           regexp_extract(j->>'$.subject.reference', {REF_ID}, 1) AS patient_id,
           regexp_extract(j->>'$.encounter.reference', {REF_ID}, 1) AS encounter_id,
           list_filter(json_extract_string(j, '$.code.coding[*].code'),
                       x -> list_contains($delivery_codes, x))[1] AS delivery_code,
           (j->>'$.performedPeriod.start')::TIMESTAMPTZ AS delivery_start
    FROM r
    WHERE resource_type = 'Procedure'
      AND json_extract_string(j, '$.code.coding[*].code') && $delivery_codes
    QUALIFY row_number() OVER (PARTITION BY patient_id ORDER BY delivery_start DESC, procedure_id) = 1
),
discharge AS (
    SELECT l.patient_id, (e.j->>'$.period.end')::TIMESTAMPTZ AS discharge_at
    FROM latest l
    JOIN r e ON e.resource_type = 'Encounter' AND e.resource_id = l.encounter_id
),
hdp AS (
    -- Our own augmented Conditions are never "native", whatever their code.
    SELECT l.patient_id, c.resource_id AS condition_id,
           list_filter(json_extract_string(c.j, '$.code.coding[*].code'),
                       x -> list_contains($native_codes, x))[1] AS code,
           (c.j->>'$.onsetDateTime')::TIMESTAMPTZ AS onset,
           l.delivery_start - to_days($lookback_days) AS window_start
    FROM latest l
    JOIN r c ON c.resource_type = 'Condition'
            AND regexp_extract(c.j->>'$.subject.reference', {REF_ID}, 1) = l.patient_id
    WHERE json_extract_string(c.j, '$.code.coding[*].code') && $native_codes
      AND NOT coalesce(list_contains(json_extract_string(c.j, '$.meta.tag[*].code'), $tag), false)
),
native AS (
    SELECT h.patient_id, h.condition_id, h.code, h.onset
    FROM hdp h
    JOIN discharge d USING (patient_id)
    WHERE h.onset BETWEEN h.window_start AND d.discharge_at
    QUALIFY row_number() OVER (PARTITION BY h.patient_id ORDER BY h.onset DESC, h.condition_id) = 1
),
prior AS (
    SELECT DISTINCT patient_id FROM hdp WHERE onset < window_start
)
SELECT l.patient_id, l.procedure_id, l.delivery_code, l.encounter_id,
       l.delivery_start::TIMESTAMP,
       d.discharge_at::TIMESTAMP,
       (p.j->>'$.deceasedDateTime')::TIMESTAMPTZ::TIMESTAMP,
       n.condition_id, n.code, n.onset::TIMESTAMP,
       pr.patient_id IS NOT NULL AS prior_hdp
FROM latest l
LEFT JOIN discharge d USING (patient_id)
LEFT JOIN r p ON p.resource_type = 'Patient' AND p.resource_id = l.patient_id
LEFT JOIN native n USING (patient_id)
LEFT JOIN prior pr USING (patient_id)
ORDER BY l.patient_id
"""


def find_deliveries(con: duckdb.DuckDBPyConnection, cfg: CohortConfig) -> list[Delivery]:
    rows = con.execute(DELIVERIES_SQL, {
        "delivery_codes": cfg.delivery_codes,
        "native_codes": cfg.native_hdp_codes,
        "tag": AUGMENTATION_TAG["code"],
        "lookback_days": cfg.hdp_lookback_days,
    }).fetchall()
    return [
        Delivery(*row[:7], native_hdp=(row[7], row[8], row[9]) if row[7] else None, prior_hdp=row[10])
        for row in rows
    ]


# --- Deciding who is in ----------------------------------------------------


@dataclass
class CohortPlan:
    native: list[Delivery] = field(default_factory=list)
    augmented: list[Delivery] = field(default_factory=list)
    exclusions: list[tuple[Delivery, str, str]] = field(default_factory=list)  # (delivery, reason, detail)


def plan_cohort(deliveries: list[Delivery], cfg: CohortConfig) -> CohortPlan:
    plan = CohortPlan()
    pool: list[Delivery] = []

    for d in sorted(deliveries, key=lambda d: d.patient_id):
        if d.discharge_at is None:
            plan.exclusions.append((d, "delivery_encounter_missing", f"encounter {d.encounter_id}"))
        elif d.deceased_at is not None and d.deceased_at < d.discharge_at:
            # Later deaths stay in, with deceased_at for later phases to respect (D15).
            plan.exclusions.append((d, "died_before_discharge",
                                    f"died {d.deceased_at:%Y-%m-%d %H:%M}, discharged {d.discharge_at:%Y-%m-%d %H:%M}"))
        elif d.native_hdp:
            plan.native.append(d)
        else:
            pool.append(d)

    # Rounded half up, so the count doesn't depend on Python's banker's rounding.
    k = math.floor(cfg.fraction * len(pool) + 0.5)
    chosen = {d.patient_id for d in random.Random(cfg.seed).sample(pool, k)}
    for d in pool:
        if d.patient_id in chosen:
            plan.augmented.append(d)
        else:
            plan.exclusions.append((d, "no_hdp_not_augmented", f"not in the seeded {cfg.fraction:.0%} sample"))
    return plan


# --- The augmentation bundle -----------------------------------------------


def augmentation_condition(d: Delivery, cfg: CohortConfig) -> Resource:
    """A synthetic, tagged HDP Condition for one patient. Same inputs give the same resource."""
    lo, hi = cfg.onset_days_before_delivery
    days = random.Random(f"{cfg.seed}:{d.patient_id}").randint(lo, hi)
    onset = (d.delivery_start - timedelta(days=days)).replace(tzinfo=UTC).isoformat()
    return {
        "resourceType": "Condition",
        "id": str(uuid.uuid5(ID_NAMESPACE, f"{d.patient_id}:{d.procedure_id}")),
        "meta": {"tag": [AUGMENTATION_TAG]},
        "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "verificationStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-category",
            "code": "problem-list-item", "display": "Problem List Item"}]}],
        "code": {
            "coding": [{"system": "http://snomed.info/sct", "code": cfg.augmentation_code,
                        "display": cfg.augmentation_display}],
            "text": cfg.augmentation_display,
        },
        "subject": {"reference": f"urn:uuid:{d.patient_id}"},
        "onsetDateTime": onset,
        "recordedDate": onset,
    }


def write_bundle(conditions: list[Resource], path: Path) -> None:
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "meta": {"tag": [AUGMENTATION_TAG]},
        "entry": [
            {"fullUrl": f"urn:uuid:{c['id']}", "resource": c,
             "request": {"method": "POST", "url": "Condition"}}
            for c in conditions
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2) + "\n")


# --- Writing the cohort ----------------------------------------------------


def check_loaded(con: duckdb.DuckDBPyConnection, conditions: list[Resource]) -> None:
    """Every augmented Condition must be in the raw store exactly as generated."""
    current = dict(con.execute(
        "SELECT resource_id, content_hash FROM raw.fhir_resource_current WHERE resource_type = 'Condition' "
        "AND resource_id IN (SELECT unnest($ids))", {"ids": [c["id"] for c in conditions] or [""]},
    ).fetchall())
    for c in conditions:
        if current.get(c["id"]) != canonical_hash(c):
            raise RuntimeError(f"Augmented Condition {c['id']} is missing from the raw store or differs from it")


def write_cohort(con: duckdb.DuckDBPyConnection, plan: CohortPlan, conditions: list[Resource],
                 cfg: CohortConfig, augmentation_load_id: int) -> int:
    con.begin()
    try:
        build_id = con.execute("SELECT nextval('cohort.build_id_seq')").fetchone()[0]
        con.execute("DELETE FROM cohort.postpartum_cohort")
        con.execute("DELETE FROM cohort.exclusion")
        # Selection above ran on record numbers inside the raw zone. What leaves it uses study IDs (D30).
        sid = ensure_study_ids(con, [d.patient_id for d in plan.native + plan.augmented]
                               + [d.patient_id for d, _, _ in plan.exclusions])

        rows = [(sid[d.patient_id], build_id, d.procedure_id, d.delivery_code, d.encounter_id,
                 d.delivery_start, d.discharge_at, "native", *d.native_hdp, d.deceased_at, d.prior_hdp) for d in plan.native]
        for d, c in zip(plan.augmented, conditions, strict=True):
            onset = datetime.fromisoformat(c["onsetDateTime"]).replace(tzinfo=None)
            rows.append((sid[d.patient_id], build_id, d.procedure_id, d.delivery_code, d.encounter_id,
                         d.delivery_start, d.discharge_at, "augmented", c["id"], cfg.augmentation_code, onset,
                         d.deceased_at, d.prior_hdp))
        if rows:
            con.executemany("INSERT INTO cohort.postpartum_cohort VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        if plan.exclusions:
            con.executemany("INSERT INTO cohort.exclusion VALUES (?, ?, ?, ?, ?)",
                            [(sid[d.patient_id], build_id, d.procedure_id, reason, detail)
                             for d, reason, detail in plan.exclusions])

        con.execute(
            "INSERT INTO cohort.build_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [build_id, datetime.now(UTC), cfg.version, cfg.sha256, augmentation_load_id,
             len(plan.native) + len(plan.augmented) + len(plan.exclusions),
             len(rows), len(plan.native), len(plan.augmented), len(plan.exclusions)],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return build_id


def build(db_path: Path = DB_PATH, config_path: Path = CONFIG_PATH,
          bundle_path: Path = AUGMENTATION_BUNDLE) -> tuple[int, CohortPlan]:
    cfg = load_config(config_path)
    with connect(db_path) as con:
        plan = plan_cohort(find_deliveries(con, cfg), cfg)

    conditions = [augmentation_condition(d, cfg) for d in plan.augmented]
    write_bundle(conditions, bundle_path)
    load = run_load([bundle_path], db_path)  # opens its own connection
    print(f"   augmentation bundle: load {load.load_id}, "
          f"{load.new} new, {load.changed} changed, {load.unchanged} unchanged, {load.rejected} rejected")

    with connect(db_path) as con:
        check_loaded(con, conditions)
        build_id = write_cohort(con, plan, conditions, cfg, load.load_id)
    return build_id, plan


def print_report(build_id: int, plan: CohortPlan) -> None:
    reasons: dict[str, int] = {}
    for _, reason, _ in plan.exclusions:
        reasons[reason] = reasons.get(reason, 0) + 1
    included = len(plan.native) + len(plan.augmented)
    print(f"== Cohort build {build_id}")
    print(f"   delivery patients: {included + len(plan.exclusions)}")
    print(f"   in cohort:         {included}  (native HDP {len(plan.native)}, augmented {len(plan.augmented)})")
    print(f"   excluded:          {len(plan.exclusions)}")
    for reason, n in sorted(reasons.items()):
        print(f"     {reason:<36} {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()
    print_report(*build(args.db, args.config))


if __name__ == "__main__":
    main()
