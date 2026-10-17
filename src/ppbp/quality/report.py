"""Phase 6: the data quality report, as JSON and PDF (D35-D39).

    python -m ppbp.quality.report

Scores the latest pipeline run (ppbp.quality.score), then builds the report from the pipeline's
own tables only. Each section has a one-line "what this means" for a clinical or operations
lead, and lists the tables its numbers came from. Where counts should add up, the report
checks that they do. The PDF holds counts only: no patient, alert or record identifiers.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from ppbp import DATA_DIR, DB_PATH, PROJECT_ROOT
from ppbp.home.simulate import CONFIG_PATH as HOME_CONFIG, load_config as load_home_config
from ppbp.privacy.access import hold_summary
from ppbp.quality.score import CONFIG_PATH, run_quality

OUT_DIR = DATA_DIR / "out" / "reports"
LABEL = ("Data quality measure for this pipeline's data. It is not a clinical or patient-safety measure: "
         "it says how complete, consistent and well-formed the data is, not how patients are doing. "
         "Synthetic data. Not for clinical use.")


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> Any:
    return con.execute(sql, params or []).fetchone()[0]


def _check(label: str, value: int, parts: dict[str, int]) -> dict[str, Any]:
    return {"step": label, "value": value, "parts": parts, "adds_up": value == sum(parts.values())}


def pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


# --- Sections ---------------------------------------------------------------


def section_records(con: duckdb.DuckDBPyConnection, inputs: dict[str, Any]) -> dict[str, Any]:
    loads = inputs["fhir_loads"]
    fhir_by_source = {}
    for source, load_id in loads.items():
        row = _rows(con, "SELECT load_id, files_in, records_in, new, changed, unchanged FROM ops.load_run "
                         "WHERE load_id = ?", [load_id])[0]
        row["rejected_entries"] = _one(con, "SELECT count(*) FROM raw.load_rejection WHERE load_id = ? "
                                            "AND entry_index IS NOT NULL", [load_id])
        fhir_by_source[source] = row
    build = _rows(con, "SELECT delivery_patients, included, native_hdp, augmented_hdp, excluded FROM cohort.build_run "
                       "WHERE build_id = ?", [inputs["cohort_build_id"]])[0]
    clean = _rows(con, "SELECT lines_in, accepted, rejected, duplicates FROM home.clean_run WHERE clean_id = ?",
                  [inputs["clean_id"]])[0]
    home_files = {f.rsplit("/", 1)[-1]: n for f, n in con.execute(
        "SELECT source_file, count(*) FROM raw.home_line_current GROUP BY 1").fetchall()}
    timeline = dict(con.execute("SELECT source, count(*) FROM timeline.bp_reading GROUP BY 1").fetchall())
    skipped = _one(con, "SELECT count(*) FROM timeline.skipped_panel")

    # Readings the protocol asks for: every slot of every monitoring day before monitoring ends
    # (which is capped at death). The simulator's own counts are not used: a real pipeline can't see them.
    mon = load_home_config(HOME_CONFIG)["monitoring"]
    expected = 0
    for tz, discharge, end in con.execute("SELECT timezone, discharge_at::TIMESTAMP, monitoring_end::TIMESTAMP "
                                          "FROM home.enrollment").fetchall():
        zone = ZoneInfo(tz)
        day0 = discharge.replace(tzinfo=UTC).astimezone(zone).date()
        for day in range(1, mon["days"] + 1):
            for slot in mon["slots"]:
                at = datetime.combine(day0 + timedelta(days=day), time.fromisoformat(slot), zone)
                expected += at < end.replace(tzinfo=UTC)
    reading_lines = home_files.get("readings.jsonl", 0)
    enrollment_lines = home_files.get("enrollment.jsonl", 0)
    other_rejects = clean["rejected"] - clean["duplicates"]
    unique_received = clean["accepted"]

    names = {"synthea": "EHR records in (Synthea bundles)", "augmentation": "EHR records in (augmentation bundle)"}
    checks = [
        _check(names[src], f["records_in"],
               {"new": f["new"], "changed": f["changed"], "unchanged": f["unchanged"],
                "rejected entries": f["rejected_entries"]})
        for src, f in fhir_by_source.items()
    ] + [
        _check("delivery patients", build["delivery_patients"], {"in cohort": build["included"], "excluded": build["excluded"]}),
        _check("in cohort", build["included"], {"native HDP": build["native_hdp"], "augmented HDP": build["augmented_hdp"]}),
        _check("home export lines loaded", reading_lines + enrollment_lines,
               {"reading lines": reading_lines, "enrollment lines (one per patient)": enrollment_lines}),
        _check("reading lines cleaned", clean["lines_in"], {"accepted": clean["accepted"], "rejected": clean["rejected"]}),
        _check("rejected", clean["rejected"], {"duplicates": clean["duplicates"], "other reasons": other_rejects}),
        _check("timeline points", sum(timeline.values()),
               {"home readings": timeline.get("home", 0), "clinical BP panels": timeline.get("clinical", 0)}),
    ]
    data = {
        "fhir_loads": fhir_by_source,
        "cohort": build,
        "home": {"readings_expected_by_protocol": expected, "reading_lines_received": reading_lines,
                 "accepted_unique_readings": unique_received, "rejected_lines": clean["rejected"],
                 "completeness_pct": pct(unique_received, expected)},
        "timeline": {"home": timeline.get("home", 0), "clinical": timeline.get("clinical", 0),
                     "clinical_panels_skipped": skipped},
        "checks": checks,
        "all_checks_add_up": all(c["adds_up"] for c in checks),
    }
    means = (f"Every record is accounted for at each step. Of the {expected:,} home readings the protocol asked for, "
             f"{unique_received:,} usable ones arrived ({data['home']['completeness_pct']}%). The gap is mostly "
             f"patients who stopped sending or skipped readings, not data the pipeline lost.")
    return {"id": "records_by_stage", "title": "Records in and out at each stage", "what_this_means": means,
            "data": data, "sources": ["ops.load_run", "raw.load_rejection (counts only)", "cohort.build_run",
                                      "raw.home_line_current (counts only)", "home.clean_run", "home.enrollment",
                                      "timeline.bp_reading", "timeline.skipped_panel", "config/home.toml (protocol)"]}


def section_rejections(con: duckdb.DuckDBPyConnection, inputs: dict[str, Any]) -> dict[str, Any]:
    lines = _one(con, "SELECT lines_in FROM home.clean_run WHERE clean_id = ?", [inputs["clean_id"]])
    home = [{"reason": r["reason"], "lines": r["n"], "share_pct": pct(r["n"], lines)} for r in
            _rows(con, "SELECT reason, count(*) n FROM home.reading_rejection GROUP BY 1 ORDER BY 2 DESC, 1")]
    fhir = _rows(con, "SELECT reason, count(*) n FROM raw.load_rejection WHERE list_contains(?, load_id) "
                      "GROUP BY 1 ORDER BY 2 DESC", [inputs["fhir_load_ids"]])
    total = sum(r["lines"] for r in home)
    dupes = sum(r["lines"] for r in home if r["reason"].startswith("duplicate"))
    fhir_total = sum(r["n"] for r in fhir)
    ehr = "No EHR records were rejected." if not fhir_total else f"{fhir_total} EHR records were rejected."
    means = (f"{total} of {lines} home reading lines ({pct(total, lines)}%) were set aside, each with a recorded "
             f"reason. {dupes} were repeats of a reading already kept, so no information was lost. {ehr}")
    return {"id": "rejections_by_reason", "title": "Rejections by reason", "what_this_means": means,
            "data": {"home_lines_checked": lines, "home": home,
                     "fhir": [{"reason": r["reason"], "records": r["n"]} for r in fhir]},
            "sources": ["home.reading_rejection", "home.clean_run", "raw.load_rejection (counts only)"]}


def section_pass_rates(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rules = _rows(con, """SELECT rule_id, name, component, severity, evaluated, affected, affected_pct
                          FROM quality.rule_summary ORDER BY affected_pct DESC, rule_id""")
    for r in rules:
        r["pass_rate_pct"] = round(100.0 - r["affected_pct"], 2)
    worst = rules[0] if rules and rules[0]["affected"] else None
    means = (f"{sum(1 for r in rules if not r['affected'])} of {len(rules)} quality checks found nothing. "
             + (f"The weakest is \"{worst['name']}\" ({worst['pass_rate_pct']}% pass). " if worst else "")
             + "These are checks on the data, not on patients' blood pressure.")
    return {"id": "validation_pass_rates", "title": "Validation pass rates", "what_this_means": means,
            "data": {"rules": rules}, "sources": ["quality.rule_summary", "quality.rule_result"]}


def section_alerts(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = _rows(con, """SELECT severity, label, status, count(*) alerts, count(DISTINCT patient_id) patients
                         FROM alerts.alert GROUP BY ALL ORDER BY min(severity_rank), label, status""")
    by_sev = {}
    for r in rows:
        by_sev.setdefault(r["severity"], {"severity": r["severity"], "label": r["label"], "open": 0, "closed": 0,
                                          "held_back": 0, "total": 0})
        by_sev[r["severity"]][r["status"]] += r["alerts"]
        by_sev[r["severity"]]["total"] += r["alerts"]
    patients = {r["severity"]: r["n"] for r in _rows(con, "SELECT severity, count(DISTINCT patient_id) n "
                                                          "FROM alerts.alert GROUP BY 1")}
    table = [v | {"patients": patients.get(k, 0)} for k, v in by_sev.items()]
    crit = by_sev.get("Critical", {}).get("total", 0)
    means = (f"{sum(r['total'] for r in table)} alerts in total, {crit} of them for a severe-range reading. "
             "Alerts reflect patients' blood pressure, not data quality, so they don't affect the score.")
    return {"id": "alerts_by_severity", "title": "Alerts by severity", "what_this_means": means,
            "data": {"by_severity": table}, "sources": ["alerts.alert"]}


def section_stale(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    evaluated = _one(con, "SELECT count(*) FROM alerts.validation_result WHERE rule_id = 'DATA_STALE'")
    stale = _one(con, "SELECT count(*) FROM alerts.validation_result WHERE rule_id = 'DATA_STALE' AND outcome = 'fired'")
    silences = _one(con, "SELECT count(*) FROM alerts.alert WHERE rule_id = 'DATA_STALE'")
    hours = _one(con, "SELECT rules.params->>'$.hours' FROM alerts.rule rules WHERE rule_id = 'DATA_STALE'")
    # Patients whose last accepted reading came more than the silence limit before monitoring ended:
    # they stopped sending, rather than missing a few readings.
    stopped = _one(con, """SELECT count(*) FROM home.enrollment e
                           LEFT JOIN (SELECT patient_id, max(measured_at) AS last_at FROM home.reading GROUP BY 1) r USING (patient_id)
                           WHERE r.last_at IS NULL OR e.monitoring_end - r.last_at >= to_hours(?::INTEGER)""", [hours])
    means = (f"{stale} of {evaluated} patients ({pct(stale, evaluated)}%) went {hours}+ hours without a usable "
             f"reading at least once; {stopped} of them stopped sending before monitoring ended. During a gap, a rise "
             f"in blood pressure could go unseen, so each gap raises an alert for the care team. Like other alerts, "
             f"this is reported here as a completeness figure, not counted in the score.")
    return {"id": "stale_patients", "title": "Completeness: patients with 36+ hours without a reading",
            "what_this_means": means, "scored": False,
            "data": {"patients_evaluated": evaluated, "patients_with_a_silence": stale, "silence_alerts": silences,
                     "patients_who_stopped_sending": stopped, "silence_threshold_hours": int(hours)},
            "sources": ["alerts.validation_result", "alerts.alert", "alerts.rule", "home.enrollment", "home.reading"]}


def section_holds(db_path: Path) -> dict[str, Any]:
    h = hold_summary(db_path)
    means = (f"{h['held_back_total']} alerts were held back because the patient hadn't consented to home monitoring"
             + (f", including {h['held_severe']} for a severe-range reading. Those need a policy decision: a real "
                f"service would need a safe way to act on them." if h["held_severe"] else "."))
    return {"id": "held_alerts", "title": "Alerts held back for consent", "what_this_means": means,
            "data": h, "policy_review": h["policy_review"], "sources": ["alerts.alert_hold", "alerts.alert"]}


def section_cohort(con: duckdb.DuckDBPyConnection, inputs: dict[str, Any]) -> dict[str, Any]:
    b = _rows(con, "SELECT delivery_patients, included, native_hdp, augmented_hdp, excluded FROM cohort.build_run "
                   "WHERE build_id = ?", [inputs["cohort_build_id"]])[0]
    excluded = _rows(con, "SELECT reason, count(*) patients FROM cohort.exclusion GROUP BY 1 ORDER BY 2 DESC")
    prior = _one(con, "SELECT count(*) FROM cohort.postpartum_cohort WHERE prior_hdp")
    means = (f"{b['included']} patients are monitored: {b['native_hdp']} with a hypertensive disorder recorded in the "
             f"source data, and {b['augmented_hdp']} given one synthetically so the pipeline has enough patients to "
             f"exercise. Results for the synthetic group say nothing about real patients.")
    return {"id": "cohort", "title": "Native and augmented cohort", "what_this_means": means,
            "data": b | {"excluded_by_reason": excluded, "with_prior_hdp": prior},
            "sources": ["cohort.build_run", "cohort.exclusion", "cohort.postpartum_cohort"]}


# --- Report -----------------------------------------------------------------


def build_report(db_path: Path = DB_PATH, config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    run_id, scored, _, inputs = run_quality(db_path, config_path)
    with duckdb.connect(str(db_path), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        run = _rows(con, "SELECT run_at::VARCHAR AS run_at, config_version, config_sha256 FROM quality.run "
                         "WHERE run_id = ?", [run_id])[0]
        sections = [section_records(con, inputs), section_rejections(con, inputs), section_pass_rates(con),
                    section_alerts(con), section_stale(con), section_holds(db_path), section_cohort(con, inputs)]
    components = scored["components"]
    comp, biggest = max(components.items(), key=lambda kv: kv[1]["deduction"])
    top = max((r for r in biggest["rule_results"] if r["counted"]), key=lambda r: r["weighted_pct"], default=None)
    driver = f", mostly \"{top['rule_name'].lower()}\"" if top else ""
    score_means = (f"Higher is better. The biggest deduction ({biggest['deduction']:.2f} points) comes from "
                   f"{comp.replace('_', ' ')}{driver}. Every deduction names the checks behind it.")
    return {
        "title": "Postpartum BP pipeline: data quality report",
        "label": LABEL,
        "quality_run": {"run_id": run_id, **run, "inputs": inputs},
        "score": {"what_this_means": score_means, **scored},
        "sections": sections,
        "policy_review": next(s["policy_review"] for s in sections if s["id"] == "held_alerts"),
    }


def write_report(report: dict[str, Any], out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    from ppbp.quality.pdf import render_pdf

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, pdf_path = out_dir / "quality_report.json", out_dir / "quality_report.pdf"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    render_pdf(report, pdf_path)
    return json_path, pdf_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    report = build_report(args.db)
    json_path, pdf_path = write_report(report, args.out)
    s = report["score"]
    print(f"== Data quality report (quality run {report['quality_run']['run_id']})")
    print(f"   score: {s['overall_score']} / 100 (data quality only; not a clinical or safety measure)")
    for comp, c in s["components"].items():
        print(f"     {comp:<26} weight {c['weight']:>4g}  penalty {c['penalty']:>7.3f}  deduction {c['deduction']:.3f}")
    records = next(x for x in report["sections"] if x["id"] == "records_by_stage")["data"]
    print(f"   record counts add up at every stage: {records['all_checks_add_up']}")
    for p in report["policy_review"]:
        print(f"   POLICY REVIEW: {p['alerts']} {p['issue']}")
    for path in (json_path, pdf_path):
        print(f"   {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}")
    if not records["all_checks_add_up"]:
        raise SystemExit("Record counts don't add up: see records_by_stage.checks in the JSON report")


if __name__ == "__main__":
    main()
