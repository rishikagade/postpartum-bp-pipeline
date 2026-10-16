import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from ppbp.alerts.evaluate import (
    RULES_PATH, Reading, Window, classify, evaluate, evaluate_persistent, evaluate_silence, evaluate_thresholds,
    load_rules)
from ppbp.alerts.explain import AlertNotFound, explain_alert
from ppbp.db import connect
from ppbp.home.load import run as run_home
from ppbp.home.simulate import CONFIG_PATH as HOME_CONFIG, load_config, read_cohort, simulate, write_outputs

DISCHARGE = datetime(2020, 3, 1, 15, 0, tzinfo=UTC)          # 10:00 EST
DAY1 = datetime(2020, 3, 2, 5, 0, tzinfo=UTC)                # local midnight starting postpartum day 1


@pytest.fixture
def rules() -> dict:
    return load_rules(RULES_PATH)


def _rule(rules: dict, rule_id: str) -> dict:
    return next(r for r in rules["rule"] if r["id"] == rule_id)


def _reading(n: int, at: datetime, s: float, d: float, pid: str = "p1") -> Reading:
    return Reading(f"r#{n}", pid, at, (at - DISCHARGE).days + 1, (at - DISCHARGE).total_seconds() / 3600, s, d)


def _window(pid: str = "p1", days: int = 10, deceased: datetime | None = None) -> Window:
    end = DAY1 + timedelta(days=days)
    return Window(pid, "America/New_York", DISCHARGE, min(end, deceased) if deceased else end, deceased)


# --- Threshold rules -------------------------------------------------------


@pytest.mark.parametrize("s, d, severe, elevated", [
    (160, 100, "fired", "superseded"),
    (150, 110, "fired", "superseded"),
    (159, 109, "passed", "fired"),
    (140, 80, "passed", "fired"),
    (130, 90, "passed", "fired"),
    (139, 89, "passed", "passed"),
])
def test_threshold_boundaries(s, d, severe, elevated, rules) -> None:
    by_id = {r["id"]: r for r in rules["rule"]}
    r = [_reading(1, DAY1 + timedelta(hours=8), s, d)]
    sev, _ = classify(by_id["BP_SEVERE"], r, by_id)
    elv, _ = classify(by_id["BP_ELEVATED"], r, by_id)
    assert (sev["r#1"].outcome, elv["r#1"].outcome) == (severe, elevated)


def test_every_severe_reading_raises_its_own_alert(rules) -> None:
    by_id = {r["id"]: r for r in rules["rule"]}
    t = DAY1 + timedelta(hours=8)
    readings = [_reading(1, t, 165, 100), _reading(2, t + timedelta(hours=1), 170, 112),
                _reading(3, t + timedelta(hours=25), 162, 100)]
    _, alerts = evaluate_thresholds(by_id["BP_SEVERE"], readings, by_id)
    assert [[e[2] for e in a.evidence] for a in alerts] == [["r#1"], ["r#2"], ["r#3"]]
    assert alerts[1].summary.startswith("Severe-range reading: 170/112 mm Hg on postpartum day")


# --- Elevated episodes -----------------------------------------------------


def _elevated(rules: dict, readings: list[Reading], window: Window | None = None):
    by_id = {r["id"]: r for r in rules["rule"]}
    return evaluate_persistent(by_id["BP_ELEVATED"], readings, by_id, [window or _window()])


def test_one_elevated_reading_is_not_an_alert(rules) -> None:
    results, alerts = _elevated(rules, [_reading(1, DAY1 + timedelta(hours=8), 145, 92)])
    assert alerts == [] and results[0].outcome == "unconfirmed"


def test_elevated_alert_needs_two_readings_4_hours_apart(rules) -> None:
    t = DAY1 + timedelta(hours=8)
    readings = [_reading(1, t, 145, 92), _reading(2, t + timedelta(hours=2), 147, 93),
                _reading(3, t + timedelta(hours=5), 142, 91)]
    _, alerts = _elevated(rules, readings[:2])
    assert alerts == []                                       # 2 readings, but only 2 h apart

    _, alerts = _elevated(rules, readings)
    assert len(alerts) == 1
    assert alerts[0].triggered_at == t + timedelta(hours=5)   # confirmed by the third
    assert [e[2] for e in alerts[0].evidence] == ["r#1", "r#2", "r#3"]   # every contributing reading


def test_one_open_elevated_alert_per_patient_that_absorbs_new_readings(rules) -> None:
    t = DAY1 + timedelta(hours=8)
    readings = [_reading(i, t + timedelta(hours=12 * i), 145, 92) for i in range(18)]
    _, alerts = _elevated(rules, readings)
    assert len(alerts) == 1 and alerts[0].status == "open" and alerts[0].closed_at is None
    assert len(alerts[0].evidence) == 18 and alerts[0].updated_at == readings[-1].measured_at
    assert "18 readings on postpartum days" in alerts[0].summary


def test_elevated_alert_closes_after_48_quiet_hours_and_a_new_one_can_open(rules) -> None:
    t = DAY1 + timedelta(hours=8)
    first = [_reading(1, t, 145, 92), _reading(2, t + timedelta(hours=12), 146, 91)]
    normal = [_reading(3 + i, t + timedelta(hours=24 + 12 * i), 125, 80) for i in range(6)]
    later = [_reading(10, t + timedelta(hours=120), 150, 95), _reading(11, t + timedelta(hours=132), 149, 94)]
    _, alerts = _elevated(rules, first + normal + later)

    assert len(alerts) == 2
    assert alerts[0].status == "closed" and alerts[0].closed_at == first[-1].measured_at + timedelta(hours=48)
    assert alerts[0].closed_at <= alerts[1].triggered_at     # never two open at once
    assert "closed after 48 h" in alerts[0].summary
    assert [e[2] for e in alerts[1].evidence] == ["r#10", "r#11"]


def test_elevated_changes_never_suppress_a_severe_alert(rules) -> None:
    by_id = {r["id"]: r for r in rules["rule"]}
    t = DAY1 + timedelta(hours=8)
    # Severe readings interleaved with an elevated episode.
    readings = [_reading(i, t + timedelta(hours=12 * i), *bp) for i, bp in enumerate(
        [(145, 92), (165, 100), (146, 93), (150, 112), (144, 91), (139, 85), (172, 116)])]
    _, severe = evaluate_thresholds(by_id["BP_SEVERE"], readings, by_id)
    elv_results, elevated = _elevated(rules, readings)

    assert [a.evidence[0][2] for a in severe] == ["r#1", "r#3", "r#6"]
    assert {r.record_id for r in elv_results if r.outcome == "superseded"} == {"r#1", "r#3", "r#6"}
    # Severe readings never count toward, or hide inside, an elevated alert.
    assert not {e[2] for a in elevated for e in a.evidence} & {"r#1", "r#3", "r#6"}


# --- Silence rule ----------------------------------------------------------


def test_regular_readings_never_go_quiet(rules) -> None:
    readings = [_reading(i, DAY1 + timedelta(hours=8 + 12 * i), 120, 80) for i in range(20)]
    results, alerts = evaluate_silence(_rule(rules, "DATA_STALE"), readings, [_window()])
    assert results[0].outcome == "passed" and alerts == []


def test_a_gap_alerts_36_hours_after_the_last_reading(rules) -> None:
    t = DAY1 + timedelta(hours=8)
    readings = [_reading(1, t, 120, 80), _reading(2, t + timedelta(hours=40), 120, 80)] + \
               [_reading(3 + i, t + timedelta(hours=52 + 12 * i), 120, 80) for i in range(14)]
    _, alerts = evaluate_silence(_rule(rules, "DATA_STALE"), readings, [_window()])
    assert len(alerts) == 1
    assert alerts[0].triggered_at == t + timedelta(hours=36)
    assert ("last_reading_before_silence", "home_reading", "r#1") in alerts[0].evidence
    assert "readings resumed after 40 hours" in alerts[0].summary


def test_clock_starts_at_day_1_not_discharge(rules) -> None:
    # Discharged at 10:00 local; the first reading at 20:00 on day 1 is 34 h after discharge
    # but only 20 h into day 1, so no alert.
    readings = [_reading(i, DAY1 + timedelta(hours=20 + 12 * i), 120, 80) for i in range(18)]
    _, alerts = evaluate_silence(_rule(rules, "DATA_STALE"), readings, [_window()])
    assert alerts == []


def test_no_readings_at_all_alerts_once(rules) -> None:
    _, alerts = evaluate_silence(_rule(rules, "DATA_STALE"), [], [_window()])
    assert len(alerts) == 1 and alerts[0].triggered_at == DAY1 + timedelta(hours=36)
    assert "no readings since discharge" in alerts[0].summary


def test_silence_never_alerts_after_death(rules) -> None:
    rule = _rule(rules, "DATA_STALE")
    t = DAY1 + timedelta(hours=8)
    readings = [_reading(i, t + timedelta(hours=12 * i), 120, 80) for i in range(4)]
    last = readings[-1].measured_at

    # Dies 30 h after the last reading: the silence never reaches 36 h before death.
    _, alerts = evaluate_silence(rule, readings, [_window(deceased=last + timedelta(hours=30))])
    assert alerts == []

    # Dies 50 h after: one alert at 36 h, before death, and nothing after it.
    death = last + timedelta(hours=50)
    results, alerts = evaluate_silence(rule, readings, [_window(deceased=death)])
    assert len(alerts) == 1 and alerts[0].triggered_at < death
    assert "until death" in results[0].detail


# --- Config ----------------------------------------------------------------


@pytest.mark.parametrize("old, new, message", [
    ('id = "BP_ELEVATED"', 'id = "BP_SEVERE"', "Duplicate rule ids"),
    ('severity = "High"', 'severity = "Urgent"', "unknown severity"),
    ('unless_rule = "BP_SEVERE"', 'unless_rule = "NOPE"', "unless_rule"),
])
def test_bad_rules_config_is_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    path = tmp_path / "rules.toml"
    path.write_text(RULES_PATH.read_text().replace(old, new, 1))
    with pytest.raises(ValueError, match=message):
        load_rules(path)


# --- End to end on a simulated cohort --------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> dict:
    """Phase 4 output for 20 patients (one dies on day 3), ready for rules."""
    db = tmp_path / "test.duckdb"
    discharge = datetime(2020, 3, 1, 15, 0)
    with connect(db) as con:
        for i in range(20):
            deceased = discharge + timedelta(days=3, hours=6) if i == 0 else None
            con.execute("INSERT INTO cohort.postpartum_cohort VALUES (?, 1, ?, '66348005', ?, ?, ?, 'native', ?, "
                        "'398254007', ?, ?, false)",
                        [f"p{i:02d}", f"proc{i}", f"enc{i}", discharge - timedelta(hours=1), discharge,
                         f"hdp{i}", discharge - timedelta(days=30), deceased])
    out = tmp_path / "home"
    cfg = load_config(HOME_CONFIG)
    write_outputs(simulate(read_cohort(db), cfg), cfg, out)
    run_home(db, HOME_CONFIG, out)
    return {"db": db, "tmp": tmp_path}


def _query(db: Path, sql: str, params: list | None = None) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        return con.execute(sql, params or []).fetchall()


def test_every_reading_and_patient_gets_one_result_per_rule(store) -> None:
    stats = evaluate(store["db"])
    readings = _query(store["db"], "SELECT count(*) FROM home.reading")[0][0]
    assert _query(store["db"], "SELECT rule_id, count(*) FROM alerts.validation_result GROUP BY 1 ORDER BY 1") == [
        ("BP_ELEVATED", readings), ("BP_SEVERE", readings), ("DATA_STALE", 20)]
    assert len(stats.results) == 2 * readings + 20
    # The DataTrust-shaped summary agrees with the per-record results.
    for rule_id, affected, evaluated, pct in _query(store["db"], """
            SELECT rule_id, affected_count, evaluated_count, affected_pct FROM alerts.rule_summary"""):
        fired = _query(store["db"], "SELECT count(*) FROM alerts.validation_result WHERE rule_id = ? "
                                    "AND outcome = 'fired'", [rule_id])[0][0]
        assert (affected, round(100 * fired / evaluated, 2)) == (fired, float(pct))


def test_alerts_fire_and_are_listed_most_urgent_first(store) -> None:
    evaluate(store["db"])
    alerts = _query(store["db"], "SELECT severity_rank, triggered_at::VARCHAR, rule_id FROM alerts.alert "
                                 "ORDER BY severity_rank, triggered_at, alert_id")
    assert {r[2] for r in alerts} == {"BP_SEVERE", "BP_ELEVATED", "DATA_STALE"}
    assert [r[0] for r in alerts] == sorted(r[0] for r in alerts)
    assert _query(store["db"], """SELECT count(*) FROM alerts.alert a JOIN home.enrollment e USING (patient_id)
        WHERE e.deceased_at IS NOT NULL AND a.triggered_at >= e.deceased_at""") == [(0,)]


def test_every_alert_has_evidence_that_exists(store) -> None:
    evaluate(store["db"])
    assert _query(store["db"], """SELECT count(*) FROM alerts.alert a
        WHERE NOT EXISTS (SELECT 1 FROM alerts.alert_evidence e WHERE e.alert_id = a.alert_id)""") == [(0,)]
    assert _query(store["db"], """SELECT count(*) FROM alerts.alert_evidence e
        WHERE e.record_type = 'home_reading'
          AND NOT EXISTS (SELECT 1 FROM home.reading r WHERE r.reading_id = e.record_id)""") == [(0,)]


def test_explain_alert_traces_down_to_the_raw_line(store) -> None:
    evaluate(store["db"])
    for rule_id in ("BP_SEVERE", "DATA_STALE"):
        alert_id = _query(store["db"], "SELECT alert_id FROM alerts.alert WHERE rule_id = ? ORDER BY alert_id "
                                       "LIMIT 1", [rule_id])[0][0]
        e = explain_alert(alert_id, store["db"])

        assert e["rule"]["rule_id"] == rule_id and e["rule"]["rule_version"] == "1"
        assert e["rule"]["rules_file_sha256"] == hashlib.sha256(RULES_PATH.read_bytes()).hexdigest()
        assert e["cleaning_run"]["config_sha256"] == hashlib.sha256(HOME_CONFIG.read_bytes()).hexdigest()
        for item in e["evidence"]:
            if item["record_type"] != "home_reading":
                continue
            raw = item["raw"]
            assert hashlib.sha256(raw["raw_line"].encode()).hexdigest() == raw["line_sha256"]
            assert raw["loaded_by"]["file_sha256"]
            assert item["reading"]["submission_id"] in raw["raw_line"]
        triggers = [i for i in e["evidence"] if i["role"] == "trigger"]
        if rule_id == "BP_SEVERE":
            assert triggers and all(i["rule_result"]["outcome"] == "fired" for i in triggers)
            assert e["context"]["readings"][-1]["record_id"] == triggers[-1]["record_id"] or \
                len(triggers) > 1


def test_explain_alert_carries_no_identifying_fields(store) -> None:
    evaluate(store["db"])
    alert_id = _query(store["db"], "SELECT alert_id FROM alerts.alert LIMIT 1")[0][0]
    # FHIR's identifying Patient/Encounter fields (D6) must not appear anywhere in the output.
    forbidden = {"given", "family", "address", "telecom", "birthdate", "identifier", "display", "text"}

    def keys(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k.lower(), f"{path}.{k}"
                yield from keys(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v, path)

    found = [p for k, p in keys(explain_alert(alert_id, store["db"])) if k in forbidden]
    assert found == []


def test_unknown_alert_id_raises(store) -> None:
    evaluate(store["db"])
    with pytest.raises(AlertNotFound):
        explain_alert("ALR-000000000000", store["db"])


def test_rerun_gives_identical_alerts(store) -> None:
    evaluate(store["db"])
    sql = "SELECT COLUMNS(* EXCLUDE (run_id))::VARCHAR FROM alerts.alert ORDER BY alert_id"
    first = _query(store["db"], sql)
    evaluate(store["db"])
    assert _query(store["db"], sql) == first


def test_rule_version_is_recorded_on_results_and_alerts(store) -> None:
    rules = store["tmp"] / "rules.toml"
    rules.write_text(RULES_PATH.read_text().replace(
        'id = "BP_SEVERE"\nversion = "1"', 'id = "BP_SEVERE"\nversion = "2"').replace(
        "systolic_at_least = 160", "systolic_at_least = 155"))
    evaluate(store["db"], rules)
    assert _query(store["db"], "SELECT DISTINCT rule_version FROM alerts.validation_result "
                               "WHERE rule_id = 'BP_SEVERE'") == [("2",)]
    assert _query(store["db"], "SELECT DISTINCT rule_version FROM alerts.alert WHERE rule_id = 'BP_SEVERE'") == [("2",)]


def test_severe_alerts_match_severe_readings_whatever_the_elevated_settings(store) -> None:
    severe_readings = {r[0] for r in _query(store["db"], """
        SELECT reading_id FROM home.reading WHERE systolic >= 160 OR diastolic >= 110""")}
    severe_sql = """SELECT e.record_id FROM alerts.alert a JOIN alerts.alert_evidence e USING (alert_id)
                    WHERE a.rule_id = 'BP_SEVERE'"""
    for name, old, new in [("default", "", ""),
                           ("strict", "min_readings = 2", "min_readings = 50"),
                           ("long", "close_after_hours = 48", "close_after_hours = 400")]:
        rules = store["tmp"] / f"rules_{name}.toml"
        rules.write_text(RULES_PATH.read_text().replace(old, new) if old else RULES_PATH.read_text())
        evaluate(store["db"], rules)
        assert {r[0] for r in _query(store["db"], severe_sql)} == severe_readings, name
        assert _query(store["db"], "SELECT count(*) FROM alerts.alert WHERE rule_id = 'BP_SEVERE'") == \
            [(len(severe_readings),)]


def test_elevated_alerts_link_every_contributing_reading_and_one_is_open_per_patient(store) -> None:
    evaluate(store["db"])
    assert _query(store["db"], """SELECT count(*) FROM alerts.validation_result v
        WHERE v.rule_id = 'BP_ELEVATED' AND v.outcome = 'fired' AND NOT EXISTS (
            SELECT 1 FROM alerts.alert_evidence e JOIN alerts.alert a USING (alert_id)
            WHERE a.rule_id = 'BP_ELEVATED' AND e.record_id = v.record_id)""") == [(0,)]
    assert _query(store["db"], """SELECT coalesce(max(n), 0) FROM (SELECT count(*) n FROM alerts.alert
        WHERE rule_id = 'BP_ELEVATED' AND status = 'open' GROUP BY patient_id)""")[0][0] <= 1
