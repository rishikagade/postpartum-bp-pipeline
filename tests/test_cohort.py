import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from ppbp.cohort.build import AUGMENTATION_TAG, CONFIG_PATH, build
from ppbp.fhir.ingest import run_load

CHILDBIRTH, PREECLAMPSIA = "66348005", "398254007"
DELIVERY = datetime(2020, 3, 1, 10, 0)
# As text: TIMESTAMPTZ values would need pytz to reach Python.
ROWS_AS_TEXT = "SELECT COLUMNS(* EXCLUDE (build_id))::VARCHAR FROM cohort.postpartum_cohort ORDER BY patient_id"


def _ts(dt: datetime) -> str:
    return dt.isoformat() + "-05:00"  # Synthea writes local offsets


def _patient_resources(pid: str, *, delivery: datetime | None = DELIVERY, hdp_onset: datetime | None = None,
                       died: datetime | None = None, encounter: bool = True) -> list[dict]:
    patient = {"resourceType": "Patient", "id": pid, "gender": "female"}
    if died:
        patient["deceasedDateTime"] = _ts(died)
    out = [patient]
    if delivery:
        out.append({"resourceType": "Procedure", "id": f"proc-{pid}-{delivery:%Y}",
                    "code": {"coding": [{"system": "http://snomed.info/sct", "code": CHILDBIRTH}]},
                    "subject": {"reference": f"urn:uuid:{pid}"},
                    "encounter": {"reference": f"urn:uuid:enc-{pid}-{delivery:%Y}"},
                    "performedPeriod": {"start": _ts(delivery), "end": _ts(delivery + timedelta(minutes=15))}})
        if encounter:
            out.append({"resourceType": "Encounter", "id": f"enc-{pid}-{delivery:%Y}",
                        "period": {"start": _ts(delivery), "end": _ts(delivery + timedelta(hours=1))}})
    if hdp_onset:
        out.append({"resourceType": "Condition", "id": f"hdp-{pid}",
                    "code": {"coding": [{"system": "http://snomed.info/sct", "code": PREECLAMPSIA}]},
                    "subject": {"reference": f"urn:uuid:{pid}"}, "onsetDateTime": _ts(hdp_onset)})
    return out


def _write_bundle(path: Path, resources: list[dict]) -> None:
    path.write_text(json.dumps({"resourceType": "Bundle", "type": "transaction",
                                "entry": [{"resource": r} for r in resources]}))


@pytest.fixture
def store(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "fhir"
    src.mkdir()
    bundles = {
        "native": _patient_resources("native", hdp_onset=DELIVERY - timedelta(days=30)),
        "old_hdp": _patient_resources("old_hdp", hdp_onset=DELIVERY - timedelta(days=700)),
        # Discharge is delivery + 1 hour.
        "died_before_discharge": _patient_resources("died_before_discharge", died=DELIVERY + timedelta(minutes=30)),
        "died_after_discharge": _patient_resources("died_after_discharge", died=DELIVERY + timedelta(days=5)),
        # HDP in this pregnancy and in an earlier one.
        "native_and_prior": (_patient_resources("native_and_prior", hdp_onset=DELIVERY - timedelta(days=30))
                             + [{"resourceType": "Condition", "id": "hdp-native_and_prior-old",
                                 "code": {"coding": [{"system": "http://snomed.info/sct", "code": "198992004"}]},
                                 "subject": {"reference": "urn:uuid:native_and_prior"},
                                 "onsetDateTime": _ts(DELIVERY - timedelta(days=900))}]),
        "no_encounter": _patient_resources("no_encounter", encounter=False),
        "never_pregnant": _patient_resources("never_pregnant", delivery=None),
        # Two deliveries: the 2020 one is the anchor, and HDP before the 2018 one doesn't count.
        "two_deliveries": (_patient_resources("two_deliveries", delivery=datetime(2018, 1, 1),
                                              hdp_onset=datetime(2017, 12, 1))
                           + _patient_resources("two_deliveries")[1:]),
        **{f"pool{i}": _patient_resources(f"pool{i}") for i in range(4)},
    }
    for name, resources in bundles.items():
        _write_bundle(src / f"{name}.json", resources)
    db = tmp_path / "test.duckdb"
    run_load([src], db)
    return {"db": db, "bundle": tmp_path / "generated" / "aug.json", "tmp": tmp_path}


def _config(tmp: Path, **overrides: str) -> Path:
    text = CONFIG_PATH.read_text()
    for line, replacement in overrides.items():
        assert line in text
        text = text.replace(line, replacement)
    path = tmp / "cohort.toml"
    path.write_text(text)
    return path


def _query(db: Path, sql: str) -> list[tuple]:
    """Query with cohort tables shown by fixture name (record number), not study ID, for readable
    assertions. test_cohort_tables_hold_only_study_ids checks the real tables."""
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        for table, view in (("cohort.postpartum_cohort", "pc_by_mrn"), ("cohort.exclusion", "ex_by_mrn")):
            con.execute(f"""CREATE TEMP VIEW {view} AS SELECT t.* REPLACE (k.patient_id AS patient_id)
                            FROM {table} t JOIN phi.patient_key k ON k.study_id = t.patient_id""")
        sql = sql.replace("cohort.postpartum_cohort", "pc_by_mrn").replace("cohort.exclusion", "ex_by_mrn")
        return con.execute(sql).fetchall()


def test_every_delivery_patient_is_in_the_cohort_or_excluded_with_a_reason(store) -> None:
    build(store["db"], CONFIG_PATH, store["bundle"])

    cohort = dict(_query(store["db"], "SELECT patient_id, hdp_source FROM cohort.postpartum_cohort"))
    excluded = dict(_query(store["db"], "SELECT patient_id, reason FROM cohort.exclusion"))

    assert cohort["native"] == cohort["native_and_prior"] == "native"
    assert excluded["died_before_discharge"] == "died_before_discharge"
    assert excluded["no_encounter"] == "delivery_encounter_missing"
    assert "never_pregnant" not in cohort | excluded
    # Pool: old_hdp, died_after_discharge, two_deliveries, pool0-3 = 7 patients; 50% rounded half up = 4.
    augmented = [p for p, s in cohort.items() if s == "augmented"]
    assert len(augmented) == 4
    assert len([r for r in excluded.values() if r == "no_hdp_not_augmented"]) == 3
    assert set(cohort) | set(excluded) == {
        "native", "native_and_prior", "old_hdp", "died_before_discharge", "died_after_discharge",
        "no_encounter", "two_deliveries", "pool0", "pool1", "pool2", "pool3"}
    assert not set(cohort) & set(excluded)


def test_anchor_is_most_recent_delivery(store) -> None:
    build(store["db"], _config(store["tmp"], **{"fraction = 0.5": "fraction = 1.0"}), store["bundle"])
    rows = dict((r[0], r[1:]) for r in _query(store["db"], """
        SELECT patient_id, delivery_procedure_id, hdp_source FROM cohort.postpartum_cohort"""))
    assert rows["two_deliveries"] == ("proc-two_deliveries-2020", "augmented")


def test_death_after_discharge_stays_in_and_death_before_discharge_is_excluded(store) -> None:
    build(store["db"], _config(store["tmp"], **{"fraction = 0.5": "fraction = 1.0"}), store["bundle"])

    in_cohort = _query(store["db"], """
        SELECT deceased_at::VARCHAR, deceased_at > discharge_at FROM cohort.postpartum_cohort
        WHERE patient_id = 'died_after_discharge'""")
    assert in_cohort == [("2020-03-06 15:00:00+00", True)]  # 10:00-05:00 is 15:00 UTC

    excluded = _query(store["db"], """
        SELECT reason, detail FROM cohort.exclusion WHERE patient_id = 'died_before_discharge'""")
    assert excluded == [("died_before_discharge", "died 2020-03-01 15:30, discharged 2020-03-01 16:00")]
    assert _query(store["db"], "SELECT count(*) FROM cohort.postpartum_cohort WHERE deceased_at IS NULL")[0][0] > 0


def test_prior_hdp_is_recorded_but_does_not_affect_membership(store) -> None:
    # fraction 0: nobody is augmented, so only native HDP gets in, whatever prior_hdp says.
    build(store["db"], _config(store["tmp"], **{"fraction = 0.5": "fraction = 0.0"}), store["bundle"])
    cohort = dict(_query(store["db"], "SELECT patient_id, prior_hdp FROM cohort.postpartum_cohort"))
    assert cohort == {"native": False, "native_and_prior": True}
    excluded = dict(_query(store["db"], "SELECT patient_id, reason FROM cohort.exclusion"))
    assert excluded["old_hdp"] == excluded["two_deliveries"] == "no_hdp_not_augmented"

    build(store["db"], _config(store["tmp"], **{"fraction = 0.5": "fraction = 1.0"}), store["bundle"])
    prior = dict(_query(store["db"], "SELECT patient_id, prior_hdp FROM cohort.postpartum_cohort"))
    # old_hdp: onset 700 days before delivery. two_deliveries: HDP before the 2018 delivery.
    assert {p for p, v in prior.items() if v} == {"native_and_prior", "old_hdp", "two_deliveries"}


def test_augmented_conditions_are_tagged_and_flow_through_ingest(store) -> None:
    build(store["db"], CONFIG_PATH, store["bundle"])

    bundle = json.loads(store["bundle"].read_text())
    conditions = [e["resource"] for e in bundle["entry"]]
    assert len(conditions) == 4
    for c in conditions:
        assert c["meta"]["tag"] == [AUGMENTATION_TAG]
        assert c["code"]["coding"][0]["code"] == "48194001"
        onset = datetime.fromisoformat(c["onsetDateTime"]).replace(tzinfo=None)
        assert timedelta(days=14) <= DELIVERY + timedelta(hours=5) - onset <= timedelta(days=84)

    loaded = _query(store["db"], """
        SELECT count(*) FROM raw.fhir_resource_current
        WHERE source_file LIKE '%aug.json' AND resource_type = 'Condition'""")
    assert loaded == [(4,)]
    cohort_ids = {r[0] for r in _query(store["db"], """
        SELECT hdp_condition_id FROM cohort.postpartum_cohort WHERE hdp_source = 'augmented'""")}
    assert cohort_ids == {c["id"] for c in conditions}


def test_rebuild_is_identical(store) -> None:
    build(store["db"], CONFIG_PATH, store["bundle"])
    first_bundle = store["bundle"].read_bytes()
    first = _query(store["db"], ROWS_AS_TEXT)

    build(store["db"], CONFIG_PATH, store["bundle"])

    assert store["bundle"].read_bytes() == first_bundle
    assert _query(store["db"], ROWS_AS_TEXT) == first
    last_load = _query(store["db"], "SELECT new, changed, unchanged FROM ops.load_run ORDER BY load_id DESC LIMIT 1")
    assert last_load == [(0, 0, 4)]


def test_augmented_conditions_never_count_as_native(store) -> None:
    # Even when augmentation uses a native HDP code, the tag keeps it out of "native".
    cfg = _config(store["tmp"], **{'code = "48194001"': f'code = "{PREECLAMPSIA}"'})
    build(store["db"], cfg, store["bundle"])
    _, plan = build(store["db"], cfg, store["bundle"])
    assert [d.patient_id for d in plan.native] == ["native", "native_and_prior"]
    assert len(plan.augmented) == 4


def test_fraction_zero_augments_no_one(store) -> None:
    _, plan = build(store["db"], _config(store["tmp"], **{"fraction = 0.5": "fraction = 0.0"}), store["bundle"])
    assert plan.augmented == []
    assert json.loads(store["bundle"].read_text())["entry"] == []


def test_fraction_outside_zero_to_one_is_rejected(store) -> None:
    with pytest.raises(ValueError, match="fraction"):
        build(store["db"], _config(store["tmp"], **{"fraction = 0.5": "fraction = 1.5"}), store["bundle"])


def test_cohort_tables_hold_only_study_ids(store, pseudonym_key) -> None:
    from ppbp.privacy.pseudonym import study_id

    build(store["db"], CONFIG_PATH, store["bundle"])
    with duckdb.connect(str(store["db"]), read_only=True) as con:
        ids = [r[0] for r in con.execute("SELECT patient_id FROM cohort.postpartum_cohort "
                                         "UNION ALL SELECT patient_id FROM cohort.exclusion").fetchall()]
        mapping = dict(con.execute("SELECT patient_id, study_id FROM phi.patient_key").fetchall())
    assert ids and all(i.startswith("SID-") for i in ids)
    assert "native" not in ids
    assert mapping["native"] == study_id("native", pseudonym_key) in ids
