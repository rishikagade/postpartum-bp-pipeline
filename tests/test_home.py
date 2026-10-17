import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from ppbp.db import connect
from ppbp.home.load import Enrollment, Rejected, clean_reading, deduplicate, run
from ppbp.home.reconcile import count_reconciliation, reconcile
from ppbp.home.simulate import CONFIG_PATH, load_config, read_cohort, simulate, write_outputs

NY = ZoneInfo("America/New_York")
DISCHARGE = datetime(2020, 3, 1, 15, 0)                  # naive UTC, as in the cohort table
DISCHARGE_UTC = DISCHARGE.replace(tzinfo=UTC)


# --- Fixtures --------------------------------------------------------------


def _config(tmp: Path, **overrides: str) -> Path:
    text = CONFIG_PATH.read_text()
    for old, new in overrides.items():
        assert old in text, old
        text = text.replace(old, new)
    path = tmp / "home.toml"
    path.write_text(text)
    return path


# Every issue at a high rate, so a small cohort shows all of them.
MESSY = {
    "dropout_fraction = 0.30": "dropout_fraction = 0.30",
    "missed_fraction = 0.10": "missed_fraction = 0.10",
    "duplicate_fraction = 0.05": "duplicate_fraction = 0.25",
    "late_fraction = 0.05": "late_fraction = 0.20",
    "wrong_timezone_fraction = 0.03": "wrong_timezone_fraction = 0.20",
    "impossible_value_fraction = 0.01": "impossible_value_fraction = 0.10",
    "unit_or_field_error_fraction = 0.01": "unit_or_field_error_fraction = 0.15",
}


@pytest.fixture
def cohort_db(tmp_path: Path) -> Path:
    """A store with only the cohort table filled: 20 patients, one dying on postpartum day 4."""
    db = tmp_path / "test.duckdb"
    with connect(db) as con:
        for i in range(20):
            deceased = DISCHARGE + timedelta(days=3, hours=6) if i == 0 else None
            con.execute("INSERT INTO cohort.postpartum_cohort VALUES (?, 1, ?, '66348005', ?, ?, ?, 'native', ?, "
                        "'398254007', ?, ?, false)",
                        [f"SID-p{i:02d}", f"proc{i}", f"enc{i}", DISCHARGE - timedelta(hours=1), DISCHARGE,
                         f"hdp{i}", DISCHARGE - timedelta(days=30), deceased])
            # The vendor knows patients by record number "p00".. (D30).
            con.execute("INSERT INTO phi.patient_key VALUES (?, ?, 'test')", [f"SID-p{i:02d}", f"p{i:02d}"])
    return db


def _pipeline(tmp_path: Path, db: Path, cfg_path: Path) -> tuple:
    out = tmp_path / "home"
    cfg = load_config(cfg_path)
    result = simulate(read_cohort(db), cfg)
    write_outputs(result, cfg, out)
    raw, stats = run(db, cfg_path, out)
    return out, result, raw, stats


def _query(db: Path, sql: str, params: list | None = None) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        return con.execute(sql, params or []).fetchall()


# --- Simulator -------------------------------------------------------------


def test_simulation_is_byte_identical_on_rerun(tmp_path: Path, cohort_db: Path) -> None:
    cfg = load_config(_config(tmp_path, **MESSY))
    for name in ("a", "b"):
        write_outputs(simulate(read_cohort(cohort_db), cfg), cfg, tmp_path / name)
    for f in ("enrollment.jsonl", "readings.jsonl", "injected_issues.json"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()


def test_no_readings_at_or_after_death_and_dropouts_stop(tmp_path: Path, cohort_db: Path) -> None:
    cfg = load_config(CONFIG_PATH)
    result = simulate(read_cohort(cohort_db), cfg)
    death = DISCHARGE_UTC + timedelta(days=3, hours=6)
    p00 = [datetime.fromisoformat(s.payload["measured_at"]) for s in result.submissions
           if s.payload["patient_ref"] == "p00" and not s.payload["measured_at"].endswith("Z")]
    assert p00 and max(p00) < death

    for plan in result.plans:
        if plan.stop_day is not None:
            days = {(datetime.fromisoformat(s.payload["measured_at"]).astimezone(NY).date()
                     - DISCHARGE_UTC.astimezone(NY).date()).days
                    for s in result.submissions if s.payload["patient_ref"] == plan.patient.patient_id
                    and not s.payload["measured_at"].endswith("Z")}
            assert not days or max(days) < plan.stop_day


def test_consent_and_profile_counts_follow_config(cohort_db: Path) -> None:
    result = simulate(read_cohort(cohort_db), load_config(CONFIG_PATH))
    assert sum(not p.consent for p in result.plans) == 2          # 10% of 20
    assert sum(p.profile == "severe" for p in result.plans) == 3  # 15% of 20
    assert sum(p.stop_day is not None for p in result.plans) == 6  # 30% of 20


def test_monitoring_days_above_max_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="monitoring.days"):
        load_config(_config(tmp_path, **{"days = 10": "days = 43"}))


def test_42_day_monitoring_is_allowed(tmp_path: Path, cohort_db: Path) -> None:
    cfg = load_config(_config(tmp_path, **{"days = 10": "days = 42"}))
    result = simulate(read_cohort(cohort_db), cfg)
    assert result.scheduled == 20 * 42 * 2


# --- Cleaning rules --------------------------------------------------------


@pytest.fixture
def cfg() -> dict:
    return load_config(CONFIG_PATH)


@pytest.fixture
def roster() -> dict[str, Enrollment]:
    end = DISCHARGE_UTC + timedelta(days=11)
    return {"p1": Enrollment("SID-p1", "America/New_York", True, DISCHARGE_UTC, end, None, "e", 1),
            "dead": Enrollment("SID-dead", "America/New_York", True, DISCHARGE_UTC,
                               DISCHARGE_UTC + timedelta(days=2), DISCHARGE_UTC + timedelta(days=2), "e", 2)}


def _rec(**kw) -> dict:
    base = {"submission_id": "s1", "patient_ref": "p1", "measured_at": "2020-03-02T08:00:00-05:00",
            "received_at": "2020-03-02T13:10:00Z", "systolic": 130, "diastolic": 85, "unit": "mmHg"}
    return {**base, **kw}


def _clean(rec: dict, cfg: dict, roster: dict, line_no: int = 1):
    return clean_reading(rec, roster, cfg, "readings.jsonl", line_no, "h")


@pytest.mark.parametrize("override, reason", [
    ({"systolic": 999}, "implausible_value"),
    ({"systolic": 0}, "implausible_value"),
    ({"diastolic": 140, "systolic": 120}, "implausible_value"),
    ({"unit": "cmHg"}, "unknown_unit"),
    ({"diastolic": None}, "missing_field"),
    ({"systolic": "13O?"}, "non_numeric"),
    ({"patient_ref": "stranger"}, "unknown_patient"),
    ({"measured_at": "2020-03-02T08:00:00"}, "invalid_timestamp"),
    ({"measured_at": "2020-02-27T08:00:00-05:00"}, "outside_monitoring_window"),
    ({"patient_ref": "dead", "measured_at": "2020-03-04T08:00:00-05:00"}, "after_death"),
])
def test_unusable_readings_are_rejected_with_a_reason(override, reason, cfg, roster) -> None:
    with pytest.raises(Rejected) as exc:
        _clean(_rec(**override), cfg, roster)
    assert exc.value.reason == reason


def test_kpa_is_converted_and_flagged(cfg, roster) -> None:
    c = _clean(_rec(systolic=17.3, diastolic=11.3, unit="kPa"), cfg, roster)
    assert (c.systolic, c.diastolic) == (130, 85)
    assert c.flags == ["unit_converted_kpa"]


def test_unit_spellings_are_normalized(cfg, roster) -> None:
    for unit in ("mmHg", "mm Hg", "mm[Hg]", "MMHG "):
        assert _clean(_rec(unit=unit), cfg, roster).flags == []


def test_local_time_labelled_utc_is_corrected_to_the_patient_zone(cfg, roster) -> None:
    c = _clean(_rec(measured_at="2020-03-02T08:00:00Z"), cfg, roster)
    assert c.measured_at == datetime(2020, 3, 2, 13, 0, tzinfo=UTC)   # 08:00 EST
    assert c.flags == ["tz_corrected"]
    assert c.measured_at_source == "2020-03-02T08:00:00Z"


def test_correct_offsets_are_left_alone_across_dst(cfg, roster) -> None:
    roster["p1"] = Enrollment("SID-p1", "America/New_York", True, datetime(2020, 3, 6, tzinfo=UTC),
                              datetime(2020, 3, 17, tzinfo=UTC), None, "e", 1)
    # 2020-03-08 is the US switch to daylight time: -05:00 before, -04:00 after.
    assert _clean(_rec(measured_at="2020-03-07T08:00:00-05:00"), cfg, roster).flags == []
    assert _clean(_rec(measured_at="2020-03-09T08:00:00-04:00", received_at="2020-03-09T12:30:00Z"),
                  cfg, roster).flags == []


def test_late_arrivals_are_accepted_and_flagged(cfg, roster) -> None:
    c = _clean(_rec(received_at="2020-03-04T13:10:00Z"), cfg, roster)
    assert c.flags == ["late"]


def test_dedup_keeps_earliest_received_and_spares_distinct_readings(cfg, roster) -> None:
    first = _clean(_rec(), cfg, roster, 1)
    resend = _clean(_rec(received_at="2020-03-02T13:40:00Z"), cfg, roster, 2)
    near = _clean(_rec(submission_id="s2", measured_at="2020-03-02T08:02:00-05:00", systolic=133,
                       received_at="2020-03-02T13:20:00Z"), cfg, roster, 3)
    evening = _clean(_rec(submission_id="s3", measured_at="2020-03-02T18:00:00-05:00",
                          received_at="2020-03-02T23:05:00Z"), cfg, roster, 4)
    kept, dupes = deduplicate([resend, near, evening, first], cfg)
    assert [k.line_no for k in kept] == [1, 4]
    assert {(d.line_no, reason, dup_of) for d, reason, _, dup_of in dupes} == {
        (2, "duplicate_exact", "readings.jsonl#1"), (3, "duplicate_near", "readings.jsonl#1")}


# --- End to end ------------------------------------------------------------


def test_every_line_is_accounted_for_and_every_injected_issue_is_handled(tmp_path: Path, cohort_db: Path) -> None:
    out, result, raw, stats = _pipeline(tmp_path, cohort_db, _config(tmp_path, **MESSY))
    accepted, rejected = _query(cohort_db, "SELECT (SELECT count(*) FROM home.reading), "
                                           "(SELECT count(*) FROM home.reading_rejection)")[0]
    assert accepted + rejected == len(result.submissions) == stats.lines_in

    results = reconcile(cohort_db, out)
    assert "(no injected cause)" not in results
    for label, outcomes in results.items():
        assert "missed" not in outcomes, label
    for label in ("wrong_timezone", "late", "duplicate_exact", "duplicate_near", "impossible_value",
                  "unit_or_field_error"):
        assert results[label]["as expected"] > 0, label

    assert _query(cohort_db, """SELECT count(*) FROM home.reading r JOIN home.enrollment e USING (patient_id)
        WHERE e.deceased_at IS NOT NULL AND r.measured_at >= e.deceased_at""") == [(0,)]
    days = _query(cohort_db, "SELECT min(postpartum_day), max(postpartum_day) FROM home.reading")[0]
    assert days[0] >= 1 and days[1] <= 10


def test_rerun_is_idempotent(tmp_path: Path, cohort_db: Path) -> None:
    cfg_path = _config(tmp_path, **MESSY)
    _pipeline(tmp_path, cohort_db, cfg_path)
    rows_sql = "SELECT COLUMNS(* EXCLUDE (clean_id))::VARCHAR FROM home.reading ORDER BY reading_id"
    first = _query(cohort_db, rows_sql)

    _, _, raw, _ = _pipeline(tmp_path, cohort_db, cfg_path)

    assert raw.changes["new"] == raw.changes["changed"] == raw.changes["removed"] == 0
    assert _query(cohort_db, rows_sql) == first


def test_a_changed_export_is_a_snapshot(tmp_path: Path, cohort_db: Path) -> None:
    cfg_path = _config(tmp_path, **MESSY)
    out, _, _, stats = _pipeline(tmp_path, cohort_db, cfg_path)
    readings = out / "readings.jsonl"
    lines = readings.read_text().splitlines()
    readings.write_text("\n".join(lines[:-1] + ["{not json"]) + "\n")

    raw, after = run(cohort_db, cfg_path, out)

    assert raw.changes["changed"] == 1
    assert after.lines_in == stats.lines_in
    assert _query(cohort_db, f"SELECT reason FROM home.reading_rejection WHERE line_no = {len(lines)}") == \
        [("invalid_json",)]
    assert _query(cohort_db, "SELECT count(*) FROM raw.home_line_version WHERE line_no = ?", [len(lines)]) == [(2,)]


def test_enrollment_carries_consent_for_phase_5b(tmp_path: Path, cohort_db: Path) -> None:
    _pipeline(tmp_path, cohort_db, CONFIG_PATH)
    assert _query(cohort_db, "SELECT count(*) FILTER (WHERE NOT consent_home_monitoring), count(*) "
                             "FROM home.enrollment") == [(2, 20)]


def test_counts_add_up_from_scheduled_to_accepted_and_rejected(tmp_path: Path, cohort_db: Path) -> None:
    out, result, _, _ = _pipeline(tmp_path, cohort_db, _config(tmp_path, **MESSY))
    steps = {s.label.strip(): s for s in count_reconciliation(cohort_db, out)}

    assert all(s.ok for s in steps.values()), [s for s in steps.values() if not s.ok]
    assert steps["scheduled readings"].value == 20 * 10 * 2
    # The raw load holds both files: every reading line plus one enrollment line per patient.
    assert steps["raw lines loaded"].value == len(result.submissions) + 20
    key = json.loads((out / "injected_issues.json").read_text())["counts"]
    assert key["after_death"] > 0 and key["after_dropout"] > 0 and key["missed"] > 0 and key["duplicate_lines"] > 0


def test_count_reconciliation_catches_a_mismatch(tmp_path: Path, cohort_db: Path) -> None:
    out, _, _, _ = _pipeline(tmp_path, cohort_db, CONFIG_PATH)
    key_path = out / "injected_issues.json"
    key = json.loads(key_path.read_text())
    key["counts"]["missed"] += 1
    key_path.write_text(json.dumps(key))
    assert not all(s.ok for s in count_reconciliation(cohort_db, out))


def test_every_time_zone_correction_is_visible_on_the_record(tmp_path: Path, cohort_db: Path) -> None:
    out, _, _, _ = _pipeline(tmp_path, cohort_db, _config(tmp_path, **MESSY))
    corrected = _query(cohort_db, """
        SELECT r.reading_id, r.measured_at_source, r.measured_at::TIMESTAMP, list_contains(t.flags, 'tz_corrected')
        FROM home.reading r JOIN timeline.bp_reading t ON t.source = 'home' AND t.record_id = r.reading_id
        WHERE list_contains(r.flags, 'tz_corrected')""")
    assert corrected
    for _, source, measured, on_timeline in corrected:
        assert on_timeline
        # The record keeps what was submitted (local time labelled Z) next to the corrected UTC time.
        assert source.endswith("Z")
        assert datetime.fromisoformat(source).replace(tzinfo=None) != measured   # both UTC
    injected = reconcile(cohort_db, out)["wrong_timezone"]
    assert injected["as expected"] == len(corrected)
    assert _query(cohort_db, """SELECT count(*) FROM home.reading
        WHERE measured_at_source LIKE '%Z' AND NOT list_contains(flags, 'tz_corrected')""") == [(0,)]


def test_cleaned_tables_carry_study_ids_not_record_numbers(tmp_path: Path, cohort_db: Path) -> None:
    _pipeline(tmp_path, cohort_db, _config(tmp_path, **MESSY))
    for table in ("home.enrollment", "home.reading", "timeline.bp_reading"):
        ids = {r[0] for r in _query(cohort_db, f"SELECT DISTINCT patient_id FROM {table}")}
        assert ids and all(i.startswith("SID-p") for i in ids), table
    assert {r[0] for r in _query(cohort_db, "SELECT DISTINCT patient_id FROM home.reading_rejection "
                                            "WHERE patient_id IS NOT NULL")} <= {f"SID-p{i:02d}" for i in range(20)}
    cols = [r[0] for r in _query(cohort_db, "DESCRIBE home.enrollment")]
    assert "device_id" not in cols     # a device serial number identifies the patient (D30)
