"""DuckDB connection and schema.

Schemas:
    raw     restricted zone: unmodified FHIR JSON, which holds identifying data (DECISIONS D6).
            Downstream tables copy coded and ID fields out of it; nothing else reads it directly.
    ops     load bookkeeping: what came in, what changed, what was rejected and why.
    cohort  the postpartum cohort (Phase 3): who is in, who is out and why.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from ppbp import DB_PATH

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS cohort;

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

-- One row per cohort build. The cohort tables below hold the latest build only.
CREATE SEQUENCE IF NOT EXISTS cohort.build_id_seq START 1;

CREATE TABLE IF NOT EXISTS cohort.build_run (
    build_id             INTEGER PRIMARY KEY,
    built_at             TIMESTAMPTZ NOT NULL,
    config_version       VARCHAR NOT NULL,
    config_sha256        VARCHAR NOT NULL,
    augmentation_load_id INTEGER NOT NULL,   -- the FHIR load that carried the augmentation bundle
    delivery_patients    INTEGER NOT NULL,
    included             INTEGER NOT NULL,
    native_hdp           INTEGER NOT NULL,
    augmented_hdp        INTEGER NOT NULL,
    excluded             INTEGER NOT NULL
);

-- IDs and coded fields only (DECISIONS D6). Every *_id points at a resource in the raw store.
CREATE TABLE IF NOT EXISTS cohort.postpartum_cohort (
    patient_id            VARCHAR PRIMARY KEY,
    build_id              INTEGER NOT NULL,
    delivery_procedure_id VARCHAR NOT NULL,
    delivery_code         VARCHAR NOT NULL,
    delivery_encounter_id VARCHAR NOT NULL,
    delivery_start        TIMESTAMPTZ NOT NULL,
    discharge_at          TIMESTAMPTZ NOT NULL,   -- delivery Encounter.period.end (D4)
    hdp_source            VARCHAR NOT NULL,       -- 'native' | 'augmented'
    hdp_condition_id      VARCHAR NOT NULL,
    hdp_code              VARCHAR NOT NULL,
    hdp_onset             TIMESTAMPTZ NOT NULL,
    deceased_at           TIMESTAMPTZ,            -- NULL if alive in the source. Later phases stop data here (D15).
    prior_hdp             BOOLEAN NOT NULL        -- HDP in an earlier pregnancy; recorded, not used for selection (D14)
);

-- Every delivery patient not in the cohort, with the reason.
-- Reasons: died_before_discharge, delivery_encounter_missing, no_hdp_not_augmented.
CREATE TABLE IF NOT EXISTS cohort.exclusion (
    patient_id            VARCHAR PRIMARY KEY,
    build_id              INTEGER NOT NULL,
    delivery_procedure_id VARCHAR NOT NULL,
    reason                VARCHAR NOT NULL,
    detail                VARCHAR
);
"""


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    # UTC everywhere. TIMESTAMPTZ values are cast to TIMESTAMP (UTC) before reaching Python,
    # which avoids a pytz dependency.
    con.execute("SET TimeZone = 'UTC'")
    con.execute(SCHEMA)
    return con
