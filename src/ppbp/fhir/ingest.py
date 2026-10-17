"""Phase 2: load FHIR R4 bundles into the raw store, detecting new / changed / unchanged.

    python -m ppbp.fhir.ingest                # load every bundle in the Synthea sample
    python -m ppbp.fhir.ingest <file|dir>...  # load specific bundles (e.g. Phase 3 augmentation)

A resource is identified by (resourceType, id). Its content hash is the SHA-256 of its
JSON with sorted keys, so key order and whitespace never count as a change. Re-running a
load on the same inputs adds a load_run row and changes nothing else (idempotent).

Each load runs in one transaction: it either fully applies or leaves the store untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from ppbp import DB_PATH, FHIR_DIR, PROJECT_ROOT
from ppbp.db import connect

Resource = dict[str, Any]


def canonical_hash(resource: Resource) -> str:
    text = json.dumps(resource, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def display_path(path: Path) -> str:
    # abspath, not resolve(): `data` may be a symlink to `data.nosync` (DECISIONS D11), and the
    # recorded path should be the same on every machine.
    absolute = Path(os.path.abspath(path))
    try:
        return str(absolute.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(absolute)


def collect_files(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in inputs:
        files.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    return files


@dataclass
class LoadStats:
    load_id: int
    files_in: int = 0
    records_in: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    rejections: Counter = field(default_factory=Counter)

    @property
    def rejected(self) -> int:
        return sum(self.rejections.values())


# --- Parsing ---------------------------------------------------------------


@dataclass
class Staged:
    rows: list[dict[str, Any]] = field(default_factory=list)
    files: list[tuple] = field(default_factory=list)       # raw.load_file rows minus load_id
    rejections: list[tuple] = field(default_factory=list)  # raw.load_rejection rows minus load_id
    seen: dict[tuple[str, str], tuple[str, str, int]] = field(default_factory=dict)

    def reject(self, source: str, index: int | None, reason: str, detail: str = "") -> None:
        self.rejections.append((source, index, reason, detail))


def stage_file(path: Path, staged: Staged) -> None:
    """Parse one bundle and add its entries to `staged`, rejecting what can't be loaded."""
    source = display_path(path)
    data = path.read_bytes()
    file_hash = hashlib.sha256(data).hexdigest()

    try:
        bundle = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        staged.reject(source, None, "invalid_json", str(exc))
        staged.files.append((source, file_hash, None, 0, "rejected", "invalid_json"))
        return
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        staged.reject(source, None, "not_a_bundle")
        staged.files.append((source, file_hash, None, 0, "rejected", "not_a_bundle"))
        return

    entries = bundle.get("entry", [])
    for index, entry in enumerate(entries):
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict) or not resource.get("resourceType"):
            staged.reject(source, index, "missing_resource_type")
            continue
        if not resource.get("id"):
            staged.reject(source, index, "missing_id", resource["resourceType"])
            continue

        key = (resource["resourceType"], resource["id"])
        content_hash = canonical_hash(resource)
        if key in staged.seen:
            first_hash, first_source, first_index = staged.seen[key]
            if first_hash == content_hash:
                staged.reject(source, index, "duplicate_in_load",
                              f"identical to {first_source}#{first_index}")
            else:
                staged.reject(source, index, "conflicting_duplicate_in_load",
                              f"differs from {first_source}#{first_index}, which was kept")
            continue
        staged.seen[key] = (content_hash, source, index)
        staged.rows.append({
            "resource_type": key[0],
            "resource_id": key[1],
            "content_hash": content_hash,
            "resource_json": json.dumps(resource, ensure_ascii=False),
            "source_file": source,
            "entry_index": index,
        })
    staged.files.append((source, file_hash, bundle.get("type"), len(entries), "loaded", None))


# --- Applying to the store -------------------------------------------------


APPLY_SQL = """
CREATE TEMP TABLE classified AS
SELECT s.*, c.content_hash AS old_hash,
       CASE WHEN c.content_hash IS NULL THEN 'new'
            WHEN c.content_hash <> s.content_hash THEN 'changed'
            ELSE 'unchanged' END AS change
FROM stage s
LEFT JOIN raw.fhir_resource_current c USING (resource_type, resource_id);

INSERT INTO raw.resource_change
SELECT $load_id, resource_type, resource_id, change, old_hash, content_hash
FROM classified WHERE change <> 'unchanged';

-- A resource can change back to an earlier version, which is then already stored.
INSERT INTO raw.fhir_resource_version
SELECT resource_type, resource_id, content_hash, resource_json::JSON, $load_id, source_file, entry_index
FROM classified WHERE change <> 'unchanged'
ON CONFLICT DO NOTHING;

INSERT OR REPLACE INTO raw.fhir_resource_current
SELECT resource_type, resource_id, content_hash, $load_id, source_file, entry_index
FROM classified;
"""


def load_staged(con: duckdb.DuckDBPyConnection, staged: Staged, stats: LoadStats, workdir: Path) -> None:
    # Bulk-load via newline-delimited JSON: far faster than row-by-row inserts.
    ndjson = workdir / "stage.ndjson"
    with ndjson.open("w") as fh:
        for row in staged.rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    con.execute(
        """
        CREATE TEMP TABLE stage AS
        SELECT * FROM read_json(?, format = 'newline_delimited', columns = {
            resource_type: 'VARCHAR', resource_id: 'VARCHAR', content_hash: 'VARCHAR',
            resource_json: 'VARCHAR', source_file: 'VARCHAR', entry_index: 'INTEGER'})
        """,
        [str(ndjson)],
    )
    for statement in filter(str.strip, APPLY_SQL.split(";")):
        con.execute(statement, {"load_id": stats.load_id} if "$load_id" in statement else None)

    counts = dict(con.execute("SELECT change, count(*) FROM classified GROUP BY change").fetchall())
    stats.new, stats.changed, stats.unchanged = (counts.get(k, 0) for k in ("new", "changed", "unchanged"))
    con.execute("DROP TABLE classified")
    con.execute("DROP TABLE stage")

    if staged.files:
        con.executemany("INSERT INTO raw.load_file VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(stats.load_id, *f) for f in staged.files])
    if staged.rejections:
        con.executemany("INSERT INTO raw.load_rejection VALUES (?, ?, ?, ?, ?)",
                        [(stats.load_id, *r) for r in staged.rejections])


def run_load(inputs: list[Path], db_path: Path = DB_PATH) -> LoadStats:
    con = connect(db_path)
    try:
        load_id = con.execute("SELECT nextval('ops.load_id_seq')").fetchone()[0]
        started = datetime.now(UTC)
        stats = LoadStats(load_id=load_id)

        staged = Staged()
        files = collect_files(inputs)
        for path in files:
            stage_file(path, staged)
        stats.files_in = len(files)
        stats.records_in = sum(f[3] for f in staged.files)
        stats.rejections = Counter(r[2] for r in staged.rejections)

        try:
            con.begin()
            with tempfile.TemporaryDirectory() as tmp:
                load_staged(con, staged, stats, Path(tmp))
            con.execute(
                "INSERT INTO ops.load_run VALUES (?, 'fhir', ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, NULL)",
                [load_id, started, datetime.now(UTC), stats.files_in, stats.records_in,
                 stats.new, stats.changed, stats.unchanged, stats.rejected],
            )
            con.commit()
        except Exception as exc:
            con.rollback()
            con.execute(
                "INSERT INTO ops.load_run (load_id, kind, started_at, finished_at, status, files_in, error) "
                "VALUES (?, 'fhir', ?, ?, 'failed', ?, ?)",
                [load_id, started, datetime.now(UTC), stats.files_in, repr(exc)],
            )
            raise
        return stats
    finally:
        con.close()


def print_report(stats: LoadStats) -> None:
    print(f"== FHIR load {stats.load_id}")
    print(f"   files in:      {stats.files_in:,}")
    print(f"   records in:    {stats.records_in:,}")
    print(f"   new:           {stats.new:,}")
    print(f"   changed:       {stats.changed:,}")
    print(f"   unchanged:     {stats.unchanged:,}")
    print(f"   rejected:      {stats.rejected:,}")
    for reason, n in stats.rejections.most_common():
        print(f"     {reason:<30} {n:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", type=Path, default=[FHIR_DIR],
                        help="bundle files or directories of bundles (default: the Synthea sample)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    print_report(run_load(args.inputs, args.db))


if __name__ == "__main__":
    main()
