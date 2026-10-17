import json
from pathlib import Path

import duckdb
import pytest

from ppbp.alerts.evaluate import RULES_PATH, evaluate
from ppbp.privacy import leak_check
from ppbp.quality.pdf import render_pdf
from ppbp.quality.report import build_report, write_report
from ppbp.cohort.build import CONFIG_PATH as COHORT_CONFIG, build
from ppbp.quality.score import CONFIG_PATH, RuleResult, load_config, run_quality, score


# --- The scoring formula (DataTrust AI, adapted) ----------------------------


@pytest.fixture
def cfg() -> dict:
    return load_config(CONFIG_PATH)


def _rr(cfg: dict, rule_id: str, affected: int, evaluated: int) -> RuleResult:
    rule = next(r for r in cfg["rule"] if r["id"] == rule_id)
    return RuleResult(rule, evaluated, [("x", f"r{i}", None, None) for i in range(affected)])


def test_score_follows_datatrust_formula(cfg) -> None:
    results = [
        _rr(cfg, "HOME_MISSING_FIELD", 2, 100),     # missing_value, max_raw: 2% (no multiplier)
        _rr(cfg, "HOME_EXACT_DUPLICATE", 10, 100),  # duplicate_rows, Low: 10% x 0.5 = 5
        _rr(cfg, "HOME_NEAR_DUPLICATE", 4, 100),    #                 Medium: 4% x 1 = 4  -> worst = 5
        _rr(cfg, "HOME_UNKNOWN_UNIT", 1, 100),      # invalid_type, High: 1% x 2 = 2
        _rr(cfg, "HOME_IMPLAUSIBLE", 3, 100),       # outliers, High: 3% x 2 = 6
        _rr(cfg, "HOME_LATE", 10, 100),             # business, Low: 10 x 0.5 = 5
        _rr(cfg, "HOME_OUTSIDE_WINDOW", 10, 100),   # business, Medium: 10 x 1 = 10  -> sum = 15
    ]
    s = score(results, cfg)
    c = s["components"]
    assert (c["missing_value"]["penalty"], c["duplicate_rows"]["penalty"], c["invalid_type"]["penalty"],
            c["outliers"]["penalty"], c["business_rule_violations"]["penalty"], c["critical_failures"]["penalty"]) == \
        (2.0, 5.0, 2.0, 6.0, 15.0, 0.0)
    expected = 25 * 2 / 100 + 20 * 5 / 100 + 20 * 2 / 100 + 10 * 6 / 100 + 10 * 15 / 100
    assert s["deductions_total"] == pytest.approx(expected)
    assert s["overall_score"] == round(100 - expected, 2)
    # For "worst of" components only the worst rule counts; for sums, every flagged rule does.
    dup = {r["rule_id"]: r["counted"] for r in c["duplicate_rows"]["rule_results"] if r["affected"]}
    assert dup == {"HOME_EXACT_DUPLICATE": True, "HOME_NEAR_DUPLICATE": False}
    assert {r["rule_id"] for r in c["business_rule_violations"]["rule_results"] if r["counted"]} == \
        {"HOME_LATE", "HOME_OUTSIDE_WINDOW"}


def test_critical_rules_count_twice_and_cap_at_70(cfg) -> None:
    # DataTrust counts a Critical rule in its own component and again in critical_failures.
    s = score([_rr(cfg, "HOME_AFTER_DEATH", 10, 100), _rr(cfg, "HOME_UNKNOWN_PATIENT", 20, 100)], cfg)
    assert s["components"]["business_rule_violations"]["penalty"] == 90.0     # (10 + 20) x 3
    assert s["components"]["critical_failures"]["penalty"] == 70.0            # 90 capped at 70


def test_bad_quality_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "quality.toml"
    path.write_text(CONFIG_PATH.read_text().replace('component = "outliers"', 'component = "vibes"', 1))
    with pytest.raises(ValueError, match="unknown component"):
        load_config(path)


# --- The report on a full pipeline -----------------------------------------


def _q(db: Path, sql: str, params: list | None = None) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


@pytest.fixture
def report(world) -> dict:
    r = build_report(world["db"])
    json_path, pdf_path = write_report(r, world["out"] / "reports")
    return {"report": r, "json": json_path, "pdf": pdf_path, **world}


def test_every_deduction_names_rule_results_that_trace_to_records(report) -> None:
    r = report["report"]
    for comp, c in r["score"]["components"].items():
        counted = [x for x in c["rule_results"] if x["counted"]]
        assert bool(counted) == (c["deduction"] > 0), comp
        for x in counted:
            rows = _q(report["db"], "SELECT count(*) FROM quality.rule_result WHERE rule_id = ?", [x["rule_id"]])[0][0]
            assert rows == x["affected"] > 0, x["rule_id"]
    summary = dict(_q(report["db"], "SELECT rule_id, affected FROM quality.rule_summary"))
    per_record = dict(_q(report["db"], "SELECT rule_id, count(*) FROM quality.rule_result GROUP BY 1"))
    assert {k: v for k, v in summary.items() if v} == per_record


def test_counts_add_up_and_come_from_the_pipeline(report) -> None:
    sections = {s["id"]: s for s in report["report"]["sections"]}
    records = sections["records_by_stage"]["data"]
    assert records["all_checks_add_up"]
    assert records["home"]["accepted_unique_readings"] == _q(report["db"], "SELECT count(*) FROM home.reading")[0][0]
    assert records["home"]["readings_expected_by_protocol"] == 12 * 10 * 2       # nobody dies in this world
    cohort = sections["cohort"]["data"]
    assert (cohort["native_hdp"], cohort["augmented_hdp"]) == \
        tuple(_q(report["db"], "SELECT count(*) FILTER (WHERE hdp_source = 'native'), "
                               "count(*) FILTER (WHERE hdp_source = 'augmented') FROM cohort.postpartum_cohort")[0])
    held = sections["held_alerts"]["data"]
    assert held["held_severe"] == _q(report["db"], "SELECT count(*) FROM alerts.alert "
                                                   "WHERE status = 'held_back' AND rule_id = 'BP_SEVERE'")[0][0] > 0
    for sec in report["report"]["sections"]:
        assert sec["what_this_means"] and sec["sources"]


def test_clinical_alert_rules_do_not_move_the_score(report, tmp_path: Path) -> None:
    before = report["report"]["score"]["overall_score"]
    severe_before = _q(report["db"], "SELECT count(*) FROM alerts.alert WHERE rule_id = 'BP_SEVERE'")[0][0]
    # Lower the severe threshold so many more severe alerts fire; data quality is unchanged.
    rules = tmp_path / "rules.toml"
    rules.write_text(RULES_PATH.read_text().replace("systolic_at_least = 160", "systolic_at_least = 120"))
    evaluate(report["db"], rules)
    severe_after = _q(report["db"], "SELECT count(*) FROM alerts.alert WHERE rule_id = 'BP_SEVERE'")[0][0]
    assert severe_after > 2 * severe_before
    assert build_report(report["db"])["score"]["overall_score"] == before


def test_label_and_policy_review_appear_in_json_and_pdf(report) -> None:
    from pypdf import PdfReader

    data = json.loads(report["json"].read_text())
    assert "not a clinical or patient-safety measure" in data["label"]
    assert data["policy_review"][0]["flag"] == "POLICY_REVIEW"
    text = " ".join((p.extract_text() or "") for p in PdfReader(str(report["pdf"])).pages)
    text = " ".join(text.split())
    assert "not a clinical or patient-safety measure" in text
    assert "POLICY REVIEW" in text
    assert text.count("Synthetic data. Not for clinical use.") >= len(PdfReader(str(report["pdf"])).pages)
    assert text.count("What this means:") == len(data["sections"]) + 1


def test_the_pdf_holds_no_identifiers_and_the_leak_check_would_catch_one(report) -> None:
    out = report["out"]
    clean = leak_check.run(report["db"], out, out / "privacy" / "leak_check.json", scan_repo=False)
    assert clean["passed"], clean["findings"]

    tampered = json.loads(json.dumps(report["report"], default=str))
    sid = _q(report["db"], "SELECT patient_id FROM home.enrollment LIMIT 1")[0][0]
    tampered["sections"][0]["what_this_means"] += f" Worst patient: {sid}."
    render_pdf(tampered, out / "reports" / "tampered.pdf")
    found = leak_check.run(report["db"], out, out / "privacy" / "leak_check.json", scan_repo=False)
    assert any(f["location"].endswith("tampered.pdf") and "study_id" in f["kinds"] for f in found["findings"])


def test_silences_are_reported_but_not_scored(report, tmp_path: Path) -> None:
    cfg = load_config(CONFIG_PATH)
    assert not any("DATA_STALE" in str(r["match"]) for r in cfg["rule"])
    stale = next(s for s in report["report"]["sections"] if s["id"] == "stale_patients")
    assert stale["scored"] is False and "not counted in the score" in stale["what_this_means"]

    before = report["report"]["score"]["overall_score"]
    stale_before = stale["data"]["patients_with_a_silence"]
    # A much shorter silence limit flags many more patients; the score must not move.
    rules = tmp_path / "rules.toml"
    rules.write_text(RULES_PATH.read_text().replace("hours = 36", "hours = 13"))
    evaluate(report["db"], rules)
    again = build_report(report["db"])
    stale_after = next(s for s in again["sections"] if s["id"] == "stale_patients")["data"]["patients_with_a_silence"]
    assert stale_after > stale_before
    assert again["score"]["overall_score"] == before


def test_score_covers_the_latest_load_of_each_fhir_source(world) -> None:
    """Regression: the score once used only the most recent FHIR load, which is the small
    augmentation bundle that every cohort build reloads, so the Synthea bundles went unscored."""
    def fhir_rule(db):
        _, scored, results, inputs = run_quality(db)
        return next(r for r in results if r.rule["id"] == "FHIR_MISSING_FIELD"), inputs

    augmentation = _q(world["db"], "SELECT max(augmentation_load_id) FROM cohort.build_run")[0][0]
    synthea = _q(world["db"], "SELECT max(load_id) FROM ops.load_run WHERE kind = 'fhir' AND load_id <> ?",
                 [augmentation])[0][0]
    assert synthea < augmentation              # the augmentation bundle is the most recent load

    # Reload the augmentation bundle (a newer load); the Synthea load must still be scored.
    build(world["db"], COHORT_CONFIG, world["tmp"] / "generated" / "aug.json")
    rule, inputs = fhir_rule(world["db"])
    newest = _q(world["db"], "SELECT max(load_id) FROM ops.load_run WHERE kind = 'fhir'")[0][0]
    assert inputs["fhir_loads"] == {"synthea": synthea, "augmentation": newest}
    records = dict(_q(world["db"], "SELECT load_id, records_in FROM ops.load_run WHERE list_contains(?, load_id)",
                      [[synthea, newest]]))
    assert rule.evaluated == records[synthea] + records[newest]
    assert records[synthea] > records[newest]  # the Synthea bundles are the bulk of what's scored

    sections = {s["id"]: s for s in build_report(world["db"])["sections"]}
    steps = [c["step"] for c in sections["records_by_stage"]["data"]["checks"]]
    assert "EHR records in (Synthea bundles)" in steps and "EHR records in (augmentation bundle)" in steps
