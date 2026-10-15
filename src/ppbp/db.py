"""DuckDB connection and schema.

Schemas:
    raw  restricted zone: unmodified FHIR JSON, which holds identifying data (DECISIONS D6).
         Downstream tables copy coded and ID fields out of it; nothing else reads it directly.
    ops  load bookkeeping: what came in, what changed, what was rejected and why.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from ppbp import DB_PATH

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE SEQUENCE IF NOT EXISTS ops.load_id_seq START 1;

CREATE TABLE IF NOT EXISTS ops.load_run (
    load_id      INTEGER PRIMARY KEY,
    kind         VARCHAR NOT NULL,          -- 'fhir'
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    status       VARCHAR NOT NULL,          -- 'succeeded' | 'failed'
    files_in     INTEGER,
    records_in   INTEGER,
    new          INTEGER,
    changed      INTEGER,
    unchanged    INTEGER,
    rejected     INTEGER,
    error        VARCHAR
);

-- One row per input file per load. The file hash pins exactly what was read.
CREATE TABLE IF NOT EXISTS ops.load_file (
    load_id      INTEGER NOT NULL,
    source_file  VARCHAR NOT NULL,          -- relative to the project root
    file_sha256  VARCHAR NOT NULL,
    bundle_type  VARCHAR,
    records_in   INTEGER NOT NULL,
    status       VARCHAR NOT NULL,          -- 'loaded' | 'rejected'
    error        VARCHAR,
    PRIMARY KEY (load_id, source_file)
);

-- Anything not loaded is recorded here with a reason; nothing is dropped silently.
CREATE TABLE IF NOT EXISTS ops.load_rejection (
    load_id      INTEGER NOT NULL,
    source_file  VARCHAR NOT NULL,
    entry_index  INTEGER,                   -- NULL when the whole file was rejected
    reason       VARCHAR NOT NULL,
    detail       VARCHAR
);

-- Every new or changed resource, per load. Unchanged resources are counted, not listed.
CREATE TABLE IF NOT EXISTS ops.resource_change (
    load_id       INTEGER NOT NULL,
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    change        VARCHAR NOT NULL,         -- 'new' | 'changed'
    old_hash      VARCHAR,
    new_hash      VARCHAR NOT NULL,
    PRIMARY KEY (load_id, resource_type, resource_id)
);

-- Every distinct version of every resource ever loaded. Never updated or deleted.
CREATE TABLE IF NOT EXISTS raw.fhir_resource_version (
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR NOT NULL,
    content_hash  VARCHAR NOT NULL,         -- SHA-256 of canonical JSON
    resource_json JSON NOT NULL,
    first_load_id INTEGER NOT NULL,
    source_file   VARCHAR NOT NULL,
    entry_index   INTEGER NOT NULL,
    PRIMARY KEY (resource_type, resource_id, content_hash)
);

-- Pointer to the current version of each resource.
CREATE TABLE IF NOT EXISTS raw.fhir_resource_current (
    resource_type     VARCHAR NOT NULL,
    resource_id       VARCHAR NOT NULL,
    content_hash      VARCHAR NOT NULL,
    last_seen_load_id INTEGER NOT NULL,
    source_file       VARCHAR NOT NULL,
    entry_index       INTEGER NOT NULL,
    PRIMARY KEY (resource_type, resource_id)
);
"""


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con
