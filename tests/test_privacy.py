import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from ppbp.alerts.evaluate import evaluate
from ppbp.alerts.explain import redact_line
from ppbp.cohort.build import CONFIG_PATH as COHORT_CONFIG, build
from ppbp.db import connect
from ppbp.fhir.explore import redact
from ppbp.fhir.ingest import run_load
from ppbp.home.load import run as run_home
from ppbp.home.simulate import CONFIG_PATH as HOME_CONFIG, load_config, read_cohort, simulate, write_outputs
from ppbp.privacy import leak_check
from ppbp.privacy.access import AccessDenied, AccessLayer, Actor, load_policy
from ppbp.privacy.phi import build_contacts
from ppbp.privacy.pseudonym import ensure_study_ids, load_key, study_id

DELIVERY = datetime(2020, 3, 1, 10, 0)


# --- Pseudonymous IDs ------------------------------------------------------


def test_study_ids_are_keyed_stable_and_opaque(pseudonym_key) -> None:
    sid = study_id("a1b2c3d4-0000-4000-8000-000000000001", pseudonym_key)
    assert re.fullmatch(r"SID-[0-9a-f]{12}", sid)
    assert sid == study_id("a1b2c3d4-0000-4000-8000-000000000001", pseudonym_key)
    assert sid != study_id("a1b2c3d4-0000-4000-8000-000000000001", b"\x02" * 32)
    assert "a1b2c3d4" not in sid


def test_key_file_is_created_once_with_owner_only_access(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PPBP_PSEUDONYM_KEY")
    path = tmp_path / "secrets" / "pseudonym.key"
    key = load_key(path)
    assert len(key) == 32 and load_key(path) == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_different_key_is_refused_rather_than_re_keying(tmp_path: Path) -> None:
    with connect(tmp_path / "t.duckdb") as con:
        ensure_study_ids(con, ["a", "b"], b"\x01" * 32)
        with pytest.raises(RuntimeError, match="different pseudonym key"):
            ensure_study_ids(con, ["c"], b"\x02" * 32)


# --- Redaction -------------------------------------------------------------


def test_explore_examples_withhold_identifying_fields() -> None:
    patient = {"resourceType": "Patient", "id": "mrn-1", "name": [{"given": ["Ana12"], "family": "Ruiz34"}],
               "telecom": [{"value": "555-123-4567"}], "address": [{"line": ["1 Elm St"]}],
               "identifier": [{"value": "999-11-2222"}], "birthDate": "1990-05-06", "gender": "female",
               "text": {"div": "<div>Ana12 Ruiz34</div>"}}
    out = json.dumps(redact(patient, patient=True))
    for value in ("mrn-1", "Ana12", "Ruiz34", "555-123-4567", "1 Elm St", "999-11-2222", "1990-05-06"):
        assert value not in out
    assert '"birthDate": "1990"' in out and '"gender": "female"' in out

    encounter = {"resourceType": "Encounter", "subject": {"reference": "urn:uuid:mrn-1", "display": "Ms. Ana12 Ruiz34"},
                 "type": [{"coding": [{"code": "424441002", "display": "Prenatal visit"}]}]}
    out = json.dumps(redact(encounter))
    assert "mrn-1" not in out and "Ana12" not in out and "Prenatal visit" in out


def test_raw_lines_are_redacted_in_responses() -> None:
    line = json.dumps({"submission_id": "s1", "patient_ref": "mrn-1", "device_id": "dev-9", "systolic": 120})
    redacted, fields = redact_line(line)
    assert "mrn-1" not in redacted and "dev-9" not in redacted and '"systolic": 120' in redacted
    assert fields == ["patient_ref", "device_id"]


# --- End to end: FHIR with identifying data -> alerts -> checks -------------


def _ts(dt: datetime) -> str:
    return dt.isoformat() + "-05:00"


def _bundle(i: int) -> dict:
    mrn = f"{i:08x}-aaaa-bbbb-cccc-{i:012x}"
    patient = {"resourceType": "Patient", "id": mrn,
               "identifier": [{"type": {"coding": [{"code": "MR"}]}, "value": mrn},
                              {"type": {"coding": [{"code": "SS"}]}, "value": f"999-{i:02d}-{1000 + i}"}],
               "name": [{"use": "official", "prefix": ["Mrs."], "given": [f"Given{i}7"], "family": f"Family{i}9"}],
               "telecom": [{"system": "phone", "value": f"555-{100 + i}-{2000 + i}"}],
               "address": [{"line": [f"{i} Testing Lane"], "city": "Boston"}],
               "birthDate": f"1990-01-{i + 1:02d}", "gender": "female"}
    resources = [patient,
                 {"resourceType": "Procedure", "id": f"proc-{i}", "code": {"coding": [{"code": "66348005"}]},
                  "subject": {"reference": f"urn:uuid:{mrn}"}, "encounter": {"reference": f"urn:uuid:enc-{i}"},
                  "performedPeriod": {"start": _ts(DELIVERY), "end": _ts(DELIVERY + timedelta(minutes=15))}},
                 {"resourceType": "Encounter", "id": f"enc-{i}",
                  "subject": {"reference": f"urn:uuid:{mrn}", "display": f"Mrs. Given{i}7 Family{i}9"},
                  "period": {"start": _ts(DELIVERY), "end": _ts(DELIVERY + timedelta(hours=1))}},
                 {"resourceType": "Condition", "id": f"hdp-{i}", "code": {"coding": [{"code": "398254007"}]},
                  "subject": {"reference": f"urn:uuid:{mrn}"}, "onsetDateTime": _ts(DELIVERY - timedelta(days=30))}]
    return {"resourceType": "Bundle", "type": "transaction", "entry": [{"resource": r} for r in resources]}


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """12 patients with names, phones, SSNs and addresses, run through every phase.
    Every patient has a severe profile, so the patient without consent has alerts to hold."""
    src = tmp_path / "fhir"
    src.mkdir()
    for i in range(12):
        (src / f"Given{i}7_Family{i}9_{i}.json").write_text(json.dumps(_bundle(i)))
    db = tmp_path / "t.duckdb"
    run_load([src], db)
    build(db, COHORT_CONFIG, tmp_path / "generated" / "aug.json")
    home_cfg = tmp_path / "home.toml"
    home_cfg.write_text(HOME_CONFIG.read_text().replace("severe_patient_fraction = 0.15", "severe_patient_fraction = 1.0"))
    cfg = load_config(home_cfg)
    write_outputs(simulate(read_cohort(db), cfg), cfg, tmp_path / "home")
    run_home(db, home_cfg, tmp_path / "home")
    evaluate(db)
    build_contacts(db)
    return {"db": db, "tmp": tmp_path, "out": tmp_path / "out"}


def _q(db: Path, sql: str, params: list | None = None) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def test_leak_check_passes_on_a_clean_pipeline(world) -> None:
    report = leak_check.run(world["db"], world["out"], world["out"] / "privacy" / "leak_check.json",
                            scan_repo=False)
    assert report["passed"], report["findings"]
    assert report["catalogue"]["record_numbers"] == 12 and report["scanned"]["access_responses"] > 12


def test_leak_check_catches_planted_leaks_without_repeating_them(world) -> None:
    assert _q(world["db"], "SELECT count(*) FROM home.reading_rejection")[0][0] > 0
    with connect(world["db"]) as con:
        con.execute("UPDATE home.reading_rejection SET detail = 'call Given37 back' WHERE line_no = "
                    "(SELECT min(line_no) FROM home.reading_rejection)")
        con.execute("INSERT INTO ops.load_run (load_id, kind, started_at, status, error) "
                    "VALUES (999, 'home', now(), 'failed', 'bad roster line for 555-105-2005')")
    world["out"].mkdir(parents=True, exist_ok=True)
    (world["out"] / "debug.log").write_text("loaded 00000002-aaaa-bbbb-cccc-000000000002 ok\n")
    report_path = world["out"] / "privacy" / "leak_check.json"

    report = leak_check.run(world["db"], world["out"], report_path, scan_repo=False)

    found = {(f["location"], k) for f in report["findings"] for k in f["kinds"]}
    assert ("home.reading_rejection", "name") in found
    assert ("ops.load_run", "identifier_or_phone") in found
    assert any(loc.endswith("debug.log") and k == "record_number" for loc, k in found)
    text = report_path.read_text()
    for value in ("Given37", "555-105-2005", "00000002-aaaa-bbbb-cccc-000000000002"):
        assert value not in text


def test_every_alert_for_a_patient_without_consent_is_held_back_with_a_reason(world) -> None:
    no_consent = {r[0] for r in _q(world["db"], "SELECT patient_id FROM home.enrollment WHERE NOT consent_home_monitoring")}
    assert len(no_consent) == 1                       # 10% of 12, rounded half up
    theirs = _q(world["db"], "SELECT alert_id, rule_id, status FROM alerts.alert WHERE patient_id IN "
                             "(SELECT patient_id FROM home.enrollment WHERE NOT consent_home_monitoring)")
    assert theirs and {r[2] for r in theirs} == {"held_back"}
    assert "BP_SEVERE" in {r[1] for r in theirs}      # severe alerts are held back too
    holds = dict(_q(world["db"], "SELECT alert_id, reason FROM alerts.alert_hold"))
    assert holds == {r[0]: "no_home_monitoring_consent" for r in theirs}
    assert _q(world["db"], "SELECT count(*) FROM alerts.alert WHERE status = 'held_back' AND patient_id NOT IN "
                           "(SELECT patient_id FROM home.enrollment WHERE NOT consent_home_monitoring)") == [(0,)]

    nurse = AccessLayer(Actor("nurse.1", "nurse"), world["db"])
    assert not {a["alert_id"] for a in nurse.list_alerts()} & set(holds)
    held_id, pid = theirs[0][0], next(iter(no_consent))
    with pytest.raises(AccessDenied):
        nurse.read_alert(held_id)
    with pytest.raises(AccessDenied):
        nurse.read_contact(pid, held_id)
    timeline = nurse.read_timeline(pid)
    assert timeline["home_readings_withheld"] == "no_home_monitoring_consent"
    assert all(p["source"] == "clinical" for p in timeline["readings"])
    explained = AccessLayer(Actor("eng.1", "engineer"), world["db"]).explain_alert(held_id)
    assert explained["hold"]["reason"] == "no_home_monitoring_consent"


def test_roles_can_do_exactly_what_the_policy_allows_and_every_call_is_logged(world) -> None:
    alert = AccessLayer(Actor("seed", "nurse"), world["db"]).list_alerts()[0]
    calls = {
        "list_alerts": lambda a: a.list_alerts(),
        "read_alert": lambda a: a.read_alert(alert["alert_id"]),
        "explain_alert": lambda a: a.explain_alert(alert["alert_id"]),
        "read_timeline": lambda a: a.read_timeline(alert["patient_id"]),
        "read_contact": lambda a: a.read_contact(alert["patient_id"], alert["alert_id"]),
        "read_access_log": lambda a: a.read_access_log(),
        "read_holds": lambda a: a.read_holds(),
    }
    policy = load_policy()
    before = _q(world["db"], "SELECT count(*) FROM audit.access_log")[0][0]
    expected = []
    for role, allowed in policy.items():
        layer = AccessLayer(Actor(f"{role}.test", role), world["db"])
        for action, call in calls.items():
            if action in allowed:
                call(layer)
            else:
                with pytest.raises(AccessDenied):
                    call(layer)
            expected.append((f"{role}.test", action, "allowed" if action in allowed else "denied"))
    logged = _q(world["db"], "SELECT actor, action, outcome FROM audit.access_log WHERE access_id > ? "
                             "ORDER BY access_id", [before])
    assert logged == expected

    # The log holds IDs and purposes, never contact details or names.
    contact = AccessLayer(Actor("n", "nurse"), world["db"]).read_contact(alert["patient_id"], alert["alert_id"])
    log_text = " ".join(str(v) for row in _q(world["db"], "SELECT COLUMNS(*)::VARCHAR FROM audit.access_log") for v in row)
    assert contact["display_name"] not in log_text and contact["phone"] not in log_text
    assert f"acting on {alert['alert_id']}" in log_text


def test_contact_lookup_needs_a_visible_alert_for_the_same_patient(world) -> None:
    nurse = AccessLayer(Actor("nurse.1", "nurse"), world["db"])
    alerts = nurse.list_alerts()
    a = alerts[0]
    other = next(x for x in alerts if x["patient_id"] != a["patient_id"])
    contact = nurse.read_contact(a["patient_id"], a["alert_id"])
    assert set(contact) == {"patient_id", "display_name", "phone"}       # minimum necessary
    with pytest.raises(AccessDenied, match="another patient"):
        nurse.read_contact(a["patient_id"], other["alert_id"])
    with pytest.raises(AccessDenied, match="no such alert"):
        nurse.read_contact(a["patient_id"], "ALR-000000000000")


def test_engineer_explanation_of_a_held_alert_uses_study_ids_only_and_is_logged(world) -> None:
    held = _q(world["db"], "SELECT alert_id, patient_id FROM alerts.alert WHERE status = 'held_back' ORDER BY 1")
    assert held
    with duckdb.connect(str(world["db"]), read_only=True) as con:
        cat = leak_check.build_catalogue(con)
    before = _q(world["db"], "SELECT coalesce(max(access_id), 0) FROM audit.access_log")[0][0]
    eng = AccessLayer(Actor("eng.1", "engineer"), world["db"])
    for alert_id, pid in held:
        text = json.dumps(eng.explain_alert(alert_id), default=str)
        assert set(re.findall(r'"patient_id": "([^"]+)"', text)) == {pid} and pid.startswith("SID-")
        assert not leak_check.find(text, cat, pid)
    assert _q(world["db"], "SELECT actor, action, resource_id, patient_id, outcome FROM audit.access_log "
                           "WHERE access_id > ? ORDER BY access_id", [before]) == \
        [("eng.1", "explain_alert", a, p, "allowed") for a, p in held]


def test_held_severe_alerts_are_counted_and_flagged_without_patient_details(world) -> None:
    held_severe = _q(world["db"], "SELECT count(*) FROM alerts.alert WHERE status = 'held_back' AND rule_id = 'BP_SEVERE'")[0][0]
    assert held_severe > 0
    holds = AccessLayer(Actor("po", "privacy_officer"), world["db"]).read_holds()
    summary = holds["summary"]
    assert summary["held_severe"] == held_severe
    assert summary["policy_review"][0]["flag"] == "POLICY_REVIEW" and summary["policy_review"][0]["alerts"] == held_severe
    assert "SID-" not in json.dumps(summary) and "patient" not in json.dumps(summary)   # counts only


def test_enrollment_numbers_follow_discharge_order_whatever_the_key(world, tmp_path, monkeypatch) -> None:
    def by_record_number(db: Path) -> dict[str, int]:
        return dict(_q(db, "SELECT k.patient_id, e.enrollment_no FROM home.enrollment e "
                           "JOIN phi.patient_key k ON k.study_id = e.patient_id"))

    first = by_record_number(world["db"])
    assert sorted(first.values()) == list(range(1, len(first) + 1))
    order = [r[0] for r in _q(world["db"], "SELECT enrollment_no FROM home.enrollment ORDER BY discharge_at, enrollment_no")]
    assert order == sorted(order)

    # Same data, different pseudonym key: different study IDs, same enrollment numbers.
    monkeypatch.setenv("PPBP_PSEUDONYM_KEY", "ab" * 32)
    db2 = tmp_path / "other_key.duckdb"
    run_load([world["tmp"] / "fhir"], db2)
    build(db2, COHORT_CONFIG, tmp_path / "gen2" / "aug.json")
    cfg = load_config(world["tmp"] / "home.toml")
    write_outputs(simulate(read_cohort(db2), cfg), cfg, tmp_path / "home2")
    run_home(db2, world["tmp"] / "home.toml", tmp_path / "home2")
    assert by_record_number(db2) == first
    assert not set(_q(db2, "SELECT patient_id FROM home.enrollment")) & set(_q(world["db"], "SELECT patient_id FROM home.enrollment"))


def test_truncated_record_number_prefixes_are_caught(world) -> None:
    with duckdb.connect(str(world["db"]), read_only=True) as con:
        cat = leak_check.build_catalogue(con)
    prefix = next(iter(cat.prefixes))
    assert leak_check.find(f'"reference": "urn:uuid:{prefix}-..."', cat)["record_number_prefix"] == 1
    # Part of a longer hex run (another resource's ID), not a record number.
    assert not leak_check.find(f'"id": "{prefix}a1-..."', cat)
