"""DuckDB connection and schema.

Schemas:
    raw       RESTRICTED: unmodified FHIR JSON (which holds identifying data, D6) and home export
              lines (keyed by medical record number). Only boundary code reads it, and swaps the
              record number for a study ID on the way out (D30).
    phi       RESTRICTED: record number <-> study ID, and contact details for the logged lookup.
    ops       load bookkeeping that identifies no one: per-load counts and errors, home export
              line changes. Per-file and per-resource FHIR lineage lives in raw (D33).
    cohort    the postpartum cohort (Phase 3): who is in, who is out and why.
    home      cleaned home BP readings and enrollment (Phase 4), plus every rejected line.
    timeline  one BP timeline per cohort patient: home and clinical readings (Phase 4).
    alerts    rule results per record, alerts, and their evidence (Phase 5).
    audit     who read what, when, and whether it was allowed (Phase 5b).
    quality   data quality rule results per record, and the score built from them (Phase 6).

Everything outside raw and phi identifies patients only by study ID (SID-...), never by
record number, name or contact detail. `make leak-check` verifies this (D33).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from ppbp import DB_PATH

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS cohort;
CREATE SCHEMA IF NOT EXISTS home;
CREATE SCHEMA IF NOT EXISTS timeline;
CREATE SCHEMA IF NOT EXISTS alerts;
CREATE SCHEMA IF NOT EXISTS phi;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS quality;

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

-- One row per input file per load. The file hash pins exactly what was read. RESTRICTED (D33):
-- Synthea names each bundle file after the patient, so file paths are identifying.
CREATE TABLE IF NOT EXISTS raw.load_file (
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
-- RESTRICTED (D33): it names source files.
CREATE TABLE IF NOT EXISTS raw.load_rejection (
    load_id      INTEGER NOT NULL,
    source_file  VARCHAR NOT NULL,
    entry_index  INTEGER,                   -- NULL when the whole file was rejected
    reason       VARCHAR NOT NULL,
    detail       VARCHAR
);

-- Every new or changed resource, per load. Unchanged resources are counted, not listed.
-- RESTRICTED (D33): a Patient's resource_id is the record number.
CREATE TABLE IF NOT EXISTS raw.resource_change (
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
    patient_id            VARCHAR PRIMARY KEY,    -- study ID (SID-...), not the record number (D30)
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

-- Home reading exports, line by line, exactly as received (Phase 4). Each load of a file is a
-- full snapshot of it: lines that disappear are recorded as 'removed' in ops.home_line_change.
CREATE TABLE IF NOT EXISTS raw.home_line_version (
    source_file   VARCHAR NOT NULL,
    line_no       INTEGER NOT NULL,         -- 1-based
    line_sha256   VARCHAR NOT NULL,
    raw_line      VARCHAR NOT NULL,
    first_load_id INTEGER NOT NULL,
    PRIMARY KEY (source_file, line_no, line_sha256)
);

CREATE TABLE IF NOT EXISTS raw.home_line_current (
    source_file       VARCHAR NOT NULL,
    line_no           INTEGER NOT NULL,
    line_sha256       VARCHAR NOT NULL,
    last_load_id      INTEGER NOT NULL,
    PRIMARY KEY (source_file, line_no)
);

CREATE TABLE IF NOT EXISTS ops.home_line_change (
    load_id     INTEGER NOT NULL,
    source_file VARCHAR NOT NULL,
    line_no     INTEGER NOT NULL,
    change      VARCHAR NOT NULL,           -- 'new' | 'changed' | 'removed'
    old_hash    VARCHAR,
    new_hash    VARCHAR,
    PRIMARY KEY (load_id, source_file, line_no)
);

-- One row per cleaning run. The home and timeline tables hold the latest run only.
CREATE SEQUENCE IF NOT EXISTS home.clean_id_seq START 1;

CREATE TABLE IF NOT EXISTS home.clean_run (
    clean_id       INTEGER PRIMARY KEY,
    cleaned_at     TIMESTAMPTZ NOT NULL,
    config_version VARCHAR NOT NULL,
    config_sha256  VARCHAR NOT NULL,
    raw_load_id    INTEGER NOT NULL,        -- the home load this run cleaned
    lines_in       INTEGER NOT NULL,
    accepted       INTEGER NOT NULL,
    rejected       INTEGER NOT NULL,        -- includes duplicates
    duplicates     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS home.enrollment (
    patient_id              VARCHAR PRIMARY KEY,   -- study ID (D30); the device ID stays in raw
    enrollment_no           INTEGER NOT NULL UNIQUE, -- 1..N in discharge order: the same on any machine (D30)
    clean_id                INTEGER NOT NULL,
    timezone                VARCHAR NOT NULL,
    consent_home_monitoring BOOLEAN NOT NULL,  -- gates alerting (Phase 5b, D31)
    discharge_at            TIMESTAMPTZ NOT NULL,
    monitoring_end          TIMESTAMPTZ NOT NULL,  -- discharge + monitoring days, or death if earlier
    deceased_at             TIMESTAMPTZ,
    source_file             VARCHAR NOT NULL,
    line_no                 INTEGER NOT NULL
);

-- Accepted readings. (source_file, line_no, line_sha256) points at the exact raw line.
CREATE TABLE IF NOT EXISTS home.reading (
    reading_id            VARCHAR PRIMARY KEY,   -- '<source_file>#<line_no>'
    clean_id              INTEGER NOT NULL,
    submission_id         VARCHAR NOT NULL,
    patient_id            VARCHAR NOT NULL,
    measured_at           TIMESTAMPTZ NOT NULL,  -- UTC, after any time-zone correction
    measured_at_source    VARCHAR NOT NULL,      -- the timestamp exactly as submitted
    received_at           TIMESTAMPTZ NOT NULL,
    postpartum_day        INTEGER NOT NULL,      -- local calendar days since discharge
    hours_since_discharge DOUBLE NOT NULL,
    systolic              DOUBLE NOT NULL,       -- mm Hg
    diastolic             DOUBLE NOT NULL,       -- mm Hg
    flags                 VARCHAR[] NOT NULL,    -- 'tz_corrected', 'unit_converted_kpa', 'late'
                                                 -- (also carried onto timeline.bp_reading)
    source_file           VARCHAR NOT NULL,
    line_no               INTEGER NOT NULL,
    line_sha256           VARCHAR NOT NULL
);

-- Every line that didn't become a reading, with the reason. Nothing is dropped silently.
CREATE TABLE IF NOT EXISTS home.reading_rejection (
    reading_id    VARCHAR PRIMARY KEY,           -- '<source_file>#<line_no>'
    clean_id      INTEGER NOT NULL,
    submission_id VARCHAR,
    patient_id    VARCHAR,
    reason        VARCHAR NOT NULL,
    detail        VARCHAR,
    duplicate_of  VARCHAR,                       -- reading_id of the kept reading
    source_file   VARCHAR NOT NULL,
    line_no       INTEGER NOT NULL,
    line_sha256   VARCHAR NOT NULL
);

-- One longitudinal BP timeline per cohort patient: clean home readings plus clinical BP panels
-- from the FHIR store (IDs, codes and values only, per D6).
CREATE TABLE IF NOT EXISTS timeline.bp_reading (
    patient_id            VARCHAR NOT NULL,
    source                VARCHAR NOT NULL,      -- 'home' | 'clinical'
    record_id             VARCHAR NOT NULL,      -- home reading_id, or FHIR Observation id
    measured_at           TIMESTAMPTZ NOT NULL,
    postpartum_day        INTEGER NOT NULL,      -- negative before discharge
    hours_since_discharge DOUBLE NOT NULL,
    systolic              DOUBLE NOT NULL,
    diastolic             DOUBLE NOT NULL,
    flags                 VARCHAR[] NOT NULL,
    PRIMARY KEY (source, record_id)
);

-- Alerting (Phase 5). One row per rule run; the other alerts tables hold the latest run only.
CREATE SEQUENCE IF NOT EXISTS alerts.run_id_seq START 1;

CREATE TABLE IF NOT EXISTS alerts.rule_run (
    run_id             INTEGER PRIMARY KEY,
    run_at             TIMESTAMPTZ NOT NULL,
    rules_version      VARCHAR NOT NULL,
    rules_sha256       VARCHAR NOT NULL,
    clean_id           INTEGER NOT NULL,       -- the home cleaning run evaluated
    readings_evaluated INTEGER NOT NULL,
    patients_evaluated INTEGER NOT NULL,
    results            INTEGER NOT NULL,
    alerts             INTEGER NOT NULL
);

-- The rules as they were for this run, copied from config/rules.toml.
CREATE TABLE IF NOT EXISTS alerts.rule (
    rule_id      VARCHAR PRIMARY KEY,
    rule_version VARCHAR NOT NULL,
    run_id       INTEGER NOT NULL,
    name         VARCHAR NOT NULL,
    label        VARCHAR NOT NULL,             -- plain wording for the nurse
    severity     VARCHAR NOT NULL,
    kind         VARCHAR NOT NULL,             -- 'reading_threshold' | 'persistent_threshold' | 'patient_silence'
    params       JSON NOT NULL,                -- thresholds, exactly as configured
    description  VARCHAR NOT NULL,
    source       VARCHAR NOT NULL
);

-- One result per rule per record (DataTrust's rule-result shape, made per record).
CREATE TABLE IF NOT EXISTS alerts.validation_result (
    run_id       INTEGER NOT NULL,
    rule_id      VARCHAR NOT NULL,
    rule_version VARCHAR NOT NULL,
    record_type  VARCHAR NOT NULL,             -- 'home_reading' | 'patient'
    record_id    VARCHAR NOT NULL,             -- home.reading.reading_id, or patient_id
    patient_id   VARCHAR NOT NULL,
    outcome      VARCHAR NOT NULL,             -- 'fired' | 'passed' | 'superseded' | 'unconfirmed'
    observed     VARCHAR NOT NULL,             -- e.g. '172/112 mm Hg', or the longest silence
    detail       VARCHAR,
    PRIMARY KEY (rule_id, record_type, record_id)
);

CREATE TABLE IF NOT EXISTS alerts.alert (
    alert_id             VARCHAR PRIMARY KEY,  -- stable across re-runs
    run_id               INTEGER NOT NULL,
    rule_id              VARCHAR NOT NULL,
    rule_version         VARCHAR NOT NULL,
    label                VARCHAR NOT NULL,
    severity             VARCHAR NOT NULL,
    severity_rank        INTEGER NOT NULL,     -- 1 = most urgent
    patient_id           VARCHAR NOT NULL,     -- no other identifying data (R7)
    triggered_at         TIMESTAMPTZ NOT NULL,  -- when the rule's condition was first met
    updated_at           TIMESTAMPTZ NOT NULL,  -- latest contributing reading (or = triggered_at)
    postpartum_day       INTEGER NOT NULL,      -- at triggered_at
    days_since_discharge DOUBLE NOT NULL,       -- at triggered_at
    threshold            VARCHAR NOT NULL,      -- what was crossed, in words
    summary              VARCHAR NOT NULL,
    status               VARCHAR NOT NULL,      -- 'open' | 'closed' (end of monitoring) | 'held_back' (D31)
    closed_at            TIMESTAMPTZ            -- set when a rule closes the alert (BP_ELEVATED)
);

CREATE TABLE IF NOT EXISTS alerts.alert_evidence (
    alert_id    VARCHAR NOT NULL,
    role        VARCHAR NOT NULL,              -- 'trigger' (every contributing reading) |
                                               -- 'last_reading_before_silence' | 'silence'
    record_type VARCHAR NOT NULL,              -- 'home_reading' | 'patient'
    record_id   VARCHAR NOT NULL,
    PRIMARY KEY (alert_id, role, record_type, record_id)
);

-- Per-rule summary in DataTrust's shape: affected count and share of records evaluated.
CREATE OR REPLACE VIEW alerts.rule_summary AS
SELECT r.rule_id, r.rule_version, r.name AS rule_name, r.severity,
       count(*) FILTER (WHERE v.outcome = 'fired') AS affected_count,
       count(*) AS evaluated_count,
       round(100.0 * count(*) FILTER (WHERE v.outcome = 'fired') / nullif(count(*), 0), 2) AS affected_pct,
       r.description
FROM alerts.rule r
LEFT JOIN alerts.validation_result v USING (rule_id, rule_version)
GROUP BY ALL;

-- Alerts held back from the nurse's list, one row per alert, with the reason (D31).
CREATE TABLE IF NOT EXISTS alerts.alert_hold (
    alert_id       VARCHAR PRIMARY KEY,
    run_id         INTEGER NOT NULL,
    reason         VARCHAR NOT NULL,           -- 'no_home_monitoring_consent'
    status_if_held VARCHAR NOT NULL,           -- what the status would have been: 'open' | 'closed'
    detail         VARCHAR NOT NULL            -- where the consent decision came from
);

-- Phase 5b, RESTRICTED. Record number <-> study ID. Nothing outside raw and phi holds a record number.
CREATE TABLE IF NOT EXISTS phi.patient_key (
    study_id        VARCHAR PRIMARY KEY,
    patient_id      VARCHAR NOT NULL UNIQUE,   -- FHIR Patient.id = medical record number
    key_fingerprint VARCHAR NOT NULL           -- which pseudonym key made study_id
);

-- Phase 5b, RESTRICTED. Minimum contact details for the nurse's logged lookup (cohort only).
CREATE TABLE IF NOT EXISTS phi.patient_contact (
    study_id        VARCHAR PRIMARY KEY,
    display_name    VARCHAR NOT NULL,          -- prefix, given and family name from the official name
    phone           VARCHAR,
    source_hash     VARCHAR NOT NULL           -- content hash of the FHIR Patient version it came from
);

-- Phase 5b. Every read through the access layer, allowed or denied. IDs only, never PHI.
CREATE SEQUENCE IF NOT EXISTS audit.access_id_seq START 1;

CREATE TABLE IF NOT EXISTS audit.access_log (
    access_id     INTEGER PRIMARY KEY,
    accessed_at   TIMESTAMPTZ NOT NULL,
    actor         VARCHAR NOT NULL,            -- simulated user name, e.g. 'nurse.1'
    role          VARCHAR NOT NULL,            -- 'nurse' | 'engineer' | 'privacy_officer'
    action        VARCHAR NOT NULL,            -- e.g. 'list_alerts', 'read_timeline', 'read_contact'
    resource_type VARCHAR NOT NULL,
    resource_id   VARCHAR,
    patient_id    VARCHAR,                     -- study ID
    outcome       VARCHAR NOT NULL,            -- 'allowed' | 'denied' | 'not_found'
    reason        VARCHAR                      -- stated purpose (contact lookups) or why denied
);

-- Clinical BP panels left off the timeline, with the reason (a panel needs both components).
CREATE TABLE IF NOT EXISTS timeline.skipped_panel (
    record_id  VARCHAR PRIMARY KEY,                -- FHIR Observation id
    clean_id   INTEGER NOT NULL,
    patient_id VARCHAR NOT NULL,                   -- study ID
    reason     VARCHAR NOT NULL                    -- 'missing_component'
);

-- Phase 6. One row per quality run; the other quality tables hold the latest run only.
CREATE SEQUENCE IF NOT EXISTS quality.run_id_seq START 1;

CREATE TABLE IF NOT EXISTS quality.run (
    run_id         INTEGER PRIMARY KEY,
    run_at         TIMESTAMPTZ NOT NULL,
    config_version VARCHAR NOT NULL,
    config_sha256  VARCHAR NOT NULL,
    overall_score  DOUBLE NOT NULL,
    inputs         JSON NOT NULL                -- the load, build, cleaning and alert runs scored
);

-- One row per quality rule: DataTrust's rule-result shape, computed from the per-record rows below.
CREATE TABLE IF NOT EXISTS quality.rule_summary (
    rule_id      VARCHAR PRIMARY KEY,
    run_id       INTEGER NOT NULL,
    name         VARCHAR NOT NULL,
    scope        VARCHAR NOT NULL,             -- which records the rule checks
    component    VARCHAR NOT NULL,
    severity     VARCHAR NOT NULL,
    evaluated    INTEGER NOT NULL,
    affected     INTEGER NOT NULL,
    affected_pct DOUBLE NOT NULL
);

-- Every record a quality rule flagged. IDs only (study IDs for patients, D30).
CREATE TABLE IF NOT EXISTS quality.rule_result (
    run_id      INTEGER NOT NULL,
    rule_id     VARCHAR NOT NULL,
    record_type VARCHAR NOT NULL,
    record_id   VARCHAR NOT NULL,
    patient_id  VARCHAR,
    detail      VARCHAR,
    PRIMARY KEY (rule_id, record_type, record_id)
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


# Tables that moved schema. Each runs once on a database that still has the old table.
MIGRATIONS = [
    # Phase 5b (D33): FHIR file and resource lineage names patients, so it moved from ops to raw.
    ("ops", "load_file", "raw"),
    ("ops", "load_rejection", "raw"),
    ("ops", "resource_change", "raw"),
]


def migrate(con: duckdb.DuckDBPyConnection) -> None:
    for old_schema, table, new_schema in MIGRATIONS:
        exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ? "
                             "AND table_name = ?", [old_schema, table]).fetchone()[0]
        if exists:
            con.execute(f"INSERT INTO {new_schema}.{table} SELECT * FROM {old_schema}.{table}")
            con.execute(f"DROP TABLE {old_schema}.{table}")


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    # UTC everywhere. TIMESTAMPTZ values are cast to TIMESTAMP (UTC) before reaching Python,
    # which avoids a pytz dependency.
    con.execute("SET TimeZone = 'UTC'")
    con.execute(SCHEMA)
    migrate(con)
    return con
