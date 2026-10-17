"""Phase 6: evaluate the data quality rules per record, and score them DataTrust's way (D35-D37).

Each rule in config/quality.toml checks one kind of record (its scope), and flags the records it
finds. The flags go to quality.rule_result, one row per record, and each rule's counts go to
quality.rule_summary (DataTrust's rule-result shape). The score is DataTrust AI's:

    penalty_i  = 0-100, from the component's rules: affected % x severity multiplier, aggregated
    score      = 100 - sum(weight_i x penalty_i / 100)

and every deduction lists the rule results it came from.

This is a measure of the pipeline's data, not of patients: clinical alerts are never scored, and
neither is the silence alert, which the report shows as a separate completeness figure (D35).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from ppbp import DB_PATH, PROJECT_ROOT
from ppbp.db import connect

CONFIG_PATH = PROJECT_ROOT / "config" / "quality.toml"
SCOPES = {"fhir_entry", "home_line", "clinical_panel", "delivery_patient", "latest_load"}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    data = path.read_bytes()
    cfg = tomllib.loads(data.decode())
    cfg["sha256"] = hashlib.sha256(data).hexdigest()
    ids = [r["id"] for r in cfg["rule"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate quality rule ids")
    for r in cfg["rule"]:
        if r["scope"] not in SCOPES:
            raise ValueError(f"Rule {r['id']}: unknown scope {r['scope']!r}")
        if r["component"] not in cfg["weights"]:
            raise ValueError(f"Rule {r['id']}: unknown component {r['component']!r}")
        if r["severity"] not in cfg["severity_multiplier"]:
            raise ValueError(f"Rule {r['id']}: unknown severity {r['severity']!r}")
    return cfg


# --- Rule evaluation -------------------------------------------------------


@dataclass
class RuleResult:
    rule: dict[str, Any]
    evaluated: int
    records: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)  # type, id, patient, detail

    @property
    def affected(self) -> int:
        return len(self.records)

    @property
    def affected_pct(self) -> float:
        return round(100.0 * self.affected / self.evaluated, 4) if self.evaluated else 0.0


def latest_inputs(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    # FHIR arrives from two sources, loaded separately: the Synthea bundles and the cohort augmentation
    # bundle. Score the latest load of each. The augmentation loads are the ones the cohort builds
    # recorded, so this doesn't depend on where the files live (regression-tested).
    fhir = {k: v for k, v in con.execute("""
        WITH aug AS (SELECT DISTINCT augmentation_load_id AS load_id FROM cohort.build_run)
        SELECT 'synthea', max(load_id) FROM ops.load_run
        WHERE kind = 'fhir' AND status = 'succeeded' AND load_id NOT IN (SELECT load_id FROM aug)
        UNION ALL
        SELECT 'augmentation', augmentation_load_id FROM cohort.build_run
        WHERE build_id = (SELECT max(build_id) FROM cohort.build_run)""").fetchall() if v is not None}
    return {
        "fhir_load_ids": [fhir[k] for k in ("synthea", "augmentation") if k in fhir] or None,
        "fhir_loads": fhir or None,
        "home_load_id": one("SELECT max(load_id) FROM ops.load_run WHERE kind = 'home' AND status = 'succeeded'"),
        "cohort_build_id": one("SELECT max(build_id) FROM cohort.build_run"),
        "clean_id": one("SELECT max(clean_id) FROM home.clean_run"),
        "alert_run_id": one("SELECT max(run_id) FROM alerts.rule_run"),
    }


def _fhir_ref(load_id: int, source_file: str, entry_index: int | None) -> str:
    # The source file name is identifying (D33), so it's referenced by hash.
    where = "file" if entry_index is None else f"entry {entry_index}"
    return f"load {load_id}, file {hashlib.sha256(source_file.encode()).hexdigest()[:12]}, {where}"


def evaluate_rule(con: duckdb.DuckDBPyConnection, rule: dict[str, Any], inputs: dict[str, Any]) -> RuleResult:
    scope, match = rule["scope"], rule["match"]
    reasons = [m for m in match if ":" not in m]
    flags = [m.removeprefix("flag:") for m in match if m.startswith("flag:")]

    if scope == "fhir_entry":
        loads = inputs["fhir_load_ids"]
        evaluated = con.execute("SELECT sum(records_in) FROM ops.load_run WHERE list_contains(?, load_id)",
                                [loads]).fetchone()[0]
        rows = con.execute("SELECT load_id, source_file, entry_index, reason FROM raw.load_rejection "
                           "WHERE list_contains(?, load_id) AND list_contains(?, reason)", [loads, reasons]).fetchall()
        return RuleResult(rule, evaluated, [("fhir_entry", _fhir_ref(l, f, i), None, r) for l, f, i, r in rows])

    if scope == "home_line":
        evaluated = con.execute("SELECT lines_in FROM home.clean_run WHERE clean_id = ?",
                                [inputs["clean_id"]]).fetchone()[0]
        records = [("home_reading", rid, pid, reason) for rid, pid, reason in con.execute(
            "SELECT reading_id, patient_id, reason FROM home.reading_rejection WHERE list_contains(?, reason) "
            "ORDER BY line_no", [reasons]).fetchall()]
        for flag in flags:
            records += [("home_reading", rid, pid, f"flag {flag}") for rid, pid in con.execute(
                "SELECT reading_id, patient_id FROM home.reading WHERE list_contains(flags, ?) ORDER BY line_no",
                [flag]).fetchall()]
        return RuleResult(rule, evaluated, records)

    if scope == "clinical_panel":
        evaluated = con.execute("SELECT (SELECT count(*) FROM timeline.bp_reading WHERE source = 'clinical') "
                                "+ (SELECT count(*) FROM timeline.skipped_panel)").fetchone()[0]
        rows = con.execute("SELECT record_id, patient_id, reason FROM timeline.skipped_panel "
                           "WHERE list_contains(?, reason)", [reasons]).fetchall()
        return RuleResult(rule, evaluated, [("clinical_panel", r, p, why) for r, p, why in rows])

    if scope == "delivery_patient":
        evaluated = con.execute("SELECT delivery_patients FROM cohort.build_run WHERE build_id = ?",
                                [inputs["cohort_build_id"]]).fetchone()[0]
        rows = con.execute("SELECT patient_id, reason FROM cohort.exclusion WHERE list_contains(?, reason)",
                           [reasons]).fetchall()
        return RuleResult(rule, evaluated, [("patient", p, p, r) for p, r in rows])

    # latest_load: the most recent load of each source
    rows = con.execute("""SELECT kind, load_id, status FROM ops.load_run
                          QUALIFY row_number() OVER (PARTITION BY kind ORDER BY load_id DESC) = 1""").fetchall()
    return RuleResult(rule, len(rows), [("load", f"{k} load {i}", None, s) for k, i, s in rows if s in match])


# --- Score (DataTrust AI, adapted) -----------------------------------------


def score(results: list[RuleResult], cfg: dict[str, Any]) -> dict[str, Any]:
    mult, agg = cfg["severity_multiplier"], cfg["aggregation"]
    components: dict[str, dict[str, Any]] = {}

    def entry(r: RuleResult, weighted: float, counted: bool) -> dict[str, Any]:
        return {"rule_id": r.rule["id"], "rule_name": r.rule["name"], "severity": r.rule["severity"],
                "affected": r.affected, "evaluated": r.evaluated, "affected_pct": r.affected_pct,
                "multiplier": 1.0 if agg.get(r.rule["component"]) == "max_raw" else mult[r.rule["severity"]],
                "weighted_pct": round(weighted, 4), "counted": counted}

    for comp, weight in cfg["weights"].items():
        if comp == "critical_failures":
            rules = [r for r in results if r.rule["severity"] == "Critical"]
            weighted = [r.affected_pct * mult["Critical"] for r in rules]
            penalty = min(agg["critical_failures_cap"], sum(weighted))
            contribs = [entry(r, w, w > 0) for r, w in zip(rules, weighted)]
            how = f"sum of every Critical rule's affected % x {mult['Critical']:g}, capped at {agg['critical_failures_cap']:g}"
        else:
            rules = [r for r in results if r.rule["component"] == comp]
            mode = agg[comp]
            weighted = [r.affected_pct * (1.0 if mode == "max_raw" else mult[r.rule["severity"]]) for r in rules]
            if mode == "sum_weighted":
                penalty = min(100.0, sum(weighted))
                contribs = [entry(r, w, w > 0) for r, w in zip(rules, weighted)]
                how = "sum of affected % x severity multiplier, capped at 100"
            else:
                top = max(weighted, default=0.0)
                penalty = min(100.0, top)
                first_top = next((i for i, w in enumerate(weighted) if w == top and w > 0), None)
                contribs = [entry(r, w, i == first_top) for i, (r, w) in enumerate(zip(rules, weighted))]
                how = ("worst affected %" if mode == "max_raw" else "worst affected % x severity multiplier") \
                    + ", capped at 100"
        penalty = round(penalty, 4)
        components[comp] = {
            "weight": weight, "penalty": penalty, "deduction": round(weight * penalty / 100.0, 4),
            "component_health": round(max(0.0, 100.0 - penalty), 4), "aggregation": how,
            "rule_results": contribs,
        }
    total = sum(c["deduction"] for c in components.values())
    return {"overall_score": max(0.0, round(100.0 - total, 2)), "deductions_total": round(total, 4),
            "components": components, "severity_multiplier": mult}


# --- Run -------------------------------------------------------------------


def run_quality(db_path: Path = DB_PATH, config_path: Path = CONFIG_PATH) -> tuple[int, dict[str, Any], list[RuleResult], dict]:
    cfg = load_config(config_path)
    with connect(db_path) as con:
        inputs = latest_inputs(con)
        missing = [k for k, v in inputs.items() if v is None]
        if missing:
            raise RuntimeError(f"Nothing to score yet: no {', '.join(missing)} (run the pipeline first)")
        results = [evaluate_rule(con, r, inputs) for r in cfg["rule"]]
        scored = score(results, cfg)
        con.begin()
        try:
            run_id = con.execute("SELECT nextval('quality.run_id_seq')").fetchone()[0]
            con.execute("DELETE FROM quality.rule_summary")
            con.execute("DELETE FROM quality.rule_result")
            con.executemany("INSERT INTO quality.rule_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (r.rule["id"], run_id, r.rule["name"], r.rule["scope"], r.rule["component"], r.rule["severity"],
                 r.evaluated, r.affected, r.affected_pct) for r in results])
            rows = [(run_id, r.rule["id"], *rec) for r in results for rec in r.records]
            if rows:
                con.executemany("INSERT INTO quality.rule_result VALUES (?, ?, ?, ?, ?, ?)", rows)
            con.execute("INSERT INTO quality.run VALUES (?, ?, ?, ?, ?, ?)",
                        [run_id, datetime.now(UTC), cfg["version"], cfg["sha256"], scored["overall_score"],
                         json.dumps(inputs)])
            con.commit()
        except Exception:
            con.rollback()
            raise
    return run_id, scored, results, inputs
