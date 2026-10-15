import json
from pathlib import Path

import duckdb
import pytest

from ppbp.fhir.ingest import canonical_hash, run_load


def _patient(pid: str, **extra) -> dict:
    return {"resourceType": "Patient", "id": pid, "gender": "female", **extra}


def _write_bundle(path: Path, *resources: dict) -> Path:
    bundle = {"resourceType": "Bundle", "type": "transaction",
              "entry": [{"fullUrl": f"urn:uuid:{r.get('id')}", "resource": r} for r in resources]}
    path.write_text(json.dumps(bundle))
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


def _query(db: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql).fetchall()


def test_hash_ignores_key_order() -> None:
    assert canonical_hash({"a": 1, "b": {"c": 2, "d": 3}}) == canonical_hash({"b": {"d": 3, "c": 2}, "a": 1})


def test_first_load_is_all_new_and_reload_is_all_unchanged(tmp_path: Path, db: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    _write_bundle(src / "a.json", _patient("p1"), {"resourceType": "Observation", "id": "o1", "status": "final"})
    _write_bundle(src / "b.json", _patient("p2"))

    first = run_load([src], db)
    assert (first.files_in, first.records_in, first.new, first.changed, first.unchanged) == (2, 3, 3, 0, 0)

    second = run_load([src], db)
    assert (second.new, second.changed, second.unchanged, second.rejected) == (0, 0, 3, 0)
    assert _query(db, "SELECT count(*) FROM raw.fhir_resource_version") == [(3,)]
    assert _query(db, "SELECT count(*) FROM ops.resource_change") == [(3,)]
    assert _query(db, "SELECT DISTINCT last_seen_load_id FROM raw.fhir_resource_current") == [(second.load_id,)]


def test_changed_resource_keeps_both_versions(tmp_path: Path, db: Path) -> None:
    bundle = tmp_path / "a.json"
    _write_bundle(bundle, _patient("p1"), _patient("p2"))
    run_load([bundle], db)

    _write_bundle(bundle, _patient("p1", birthDate="1990-01-01"), _patient("p2"))
    stats = run_load([bundle], db)

    assert (stats.new, stats.changed, stats.unchanged) == (0, 1, 1)
    assert _query(db, "SELECT count(*) FROM raw.fhir_resource_version WHERE resource_id = 'p1'") == [(2,)]
    current = _query(db, "SELECT content_hash FROM raw.fhir_resource_current WHERE resource_id = 'p1'")
    assert current == [(canonical_hash(_patient("p1", birthDate="1990-01-01")),)]
    change = _query(db, f"SELECT change, old_hash IS NOT NULL FROM ops.resource_change "
                        f"WHERE load_id = {stats.load_id}")
    assert change == [("changed", True)]


def test_reordered_keys_are_unchanged(tmp_path: Path, db: Path) -> None:
    bundle = tmp_path / "a.json"
    _write_bundle(bundle, {"resourceType": "Patient", "id": "p1", "gender": "female"})
    run_load([bundle], db)
    _write_bundle(bundle, {"gender": "female", "id": "p1", "resourceType": "Patient"})
    assert run_load([bundle], db).unchanged == 1


def test_bad_input_is_rejected_with_a_reason_and_the_rest_loads(tmp_path: Path, db: Path) -> None:
    src = tmp_path / "in"
    src.mkdir()
    (src / "broken.json").write_text("{not json")
    (src / "not_bundle.json").write_text(json.dumps(_patient("x")))
    _write_bundle(src / "good.json", _patient("p1"), {"resourceType": "Patient"}, {"id": "no-type"})
    _write_bundle(src / "later.json", _patient("p1"), _patient("p1", gender="male"))

    stats = run_load([src], db)

    assert stats.new == 1
    assert dict(stats.rejections) == {
        "invalid_json": 1, "not_a_bundle": 1, "missing_id": 1, "missing_resource_type": 1,
        "duplicate_in_load": 1, "conflicting_duplicate_in_load": 1,
    }
    statuses = _query(db, "SELECT source_file LIKE '%broken.json', status FROM ops.load_file ORDER BY source_file")
    assert (True, "rejected") in statuses
    assert _query(db, "SELECT count(*) FROM ops.load_rejection") == [(6,)]
    # The first copy of p1 (from good.json, which sorts before later.json) is the one kept.
    assert _query(db, "SELECT source_file LIKE '%good.json' FROM raw.fhir_resource_current") == [(True,)]


def test_failed_load_leaves_store_untouched(tmp_path: Path, db: Path, monkeypatch) -> None:
    bundle = _write_bundle(tmp_path / "a.json", _patient("p1"))
    run_load([bundle], db)
    _write_bundle(bundle, _patient("p1", gender="male"), _patient("p2"))

    import ppbp.fhir.ingest as ingest

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ingest.LoadStats, "rejected", property(boom))
    with pytest.raises(RuntimeError):
        run_load([bundle], db)

    assert _query(db, "SELECT count(*) FROM raw.fhir_resource_current") == [(1,)]
    assert _query(db, "SELECT count(*) FROM raw.fhir_resource_version") == [(1,)]
    assert _query(db, "SELECT status FROM ops.load_run ORDER BY load_id") == [("succeeded",), ("failed",)]
