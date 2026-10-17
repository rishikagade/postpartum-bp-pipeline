import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ppbp.alerts.evaluate import evaluate
from ppbp.cohort.build import CONFIG_PATH as COHORT_CONFIG, build
from ppbp.fhir.ingest import run_load
from ppbp.home.load import run as run_home
from ppbp.home.simulate import CONFIG_PATH as HOME_CONFIG, load_config, read_cohort, simulate, write_outputs
from ppbp.privacy.phi import build_contacts

DELIVERY = datetime(2020, 3, 1, 10, 0)

# A fixed pseudonym key, so study IDs are the same on every test run and no key file is
# written under data/secrets by tests (D30).
TEST_KEY = "00" * 31 + "01"


@pytest.fixture(autouse=True)
def pseudonym_key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setenv("PPBP_PSEUDONYM_KEY", TEST_KEY)
    return bytes.fromhex(TEST_KEY)


# --- A full pipeline on made-up patients with identifying data (shared by Phase 5b and 6 tests) ---


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
