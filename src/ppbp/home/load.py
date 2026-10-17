"""Phase 4: load home BP exports, clean them, and build one BP timeline per patient.

    python -m ppbp.home.load

1. Raw: every line of enrollment.jsonl and readings.jsonl goes into the raw store unchanged.
   A load of a file is a full snapshot of it: new / changed / unchanged / removed lines are
   counted, and re-running on the same files changes nothing (idempotent).
2. Clean: rebuild home.enrollment, home.reading and home.reading_rejection from the current
   raw lines. Timestamps become UTC, kPa is converted, wrong time zones are corrected and
   flagged, late arrivals are flagged, duplicates are removed, and anything unusable is
   rejected with a reason (D22). Nothing is dropped silently.
3. Timeline: timeline.bp_reading = clean home readings + clinical BP panels from the FHIR store,
   aligned to postpartum day (local calendar days since discharge).

Each run is one transaction: it either fully applies or leaves the store untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from ppbp import DB_PATH
from ppbp.db import connect
from ppbp.fhir.ingest import display_path
from ppbp.home.simulate import CONFIG_PATH, OUT_DIR, load_config

ENROLLMENT_FILE = "enrollment.jsonl"
READINGS_FILE = "readings.jsonl"
MMHG_UNITS = {"mmhg", "mm hg", "mm[hg]"}

LOINC_BP_PANEL, LOINC_SYSTOLIC, LOINC_DIASTOLIC = "85354-9", "8480-6", "8462-4"


# --- 1. Raw load -----------------------------------------------------------


@dataclass
class RawStats:
    load_id: int
    files_in: int = 0
    records_in: int = 0
    changes: Counter = field(default_factory=Counter)  # new / changed / unchanged / removed


def load_raw(con: duckdb.DuckDBPyConnection, files: list[Path]) -> RawStats:
    load_id = con.execute("SELECT nextval('ops.load_id_seq')").fetchone()[0]
    stats = RawStats(load_id=load_id, files_in=len(files))
    for path in files:
        source = display_path(path)
        data = path.read_bytes()
        lines = data.decode().splitlines()
        stats.records_in += len(lines)
        con.execute("INSERT INTO raw.load_file VALUES (?, ?, ?, 'home_export', ?, 'loaded', NULL)",
                    [load_id, source, hashlib.sha256(data).hexdigest(), len(lines)])

        old = dict(con.execute("SELECT line_no, line_sha256 FROM raw.home_line_current WHERE source_file = ?",
                               [source]).fetchall())
        new_rows, changes = [], []
        for line_no, line in enumerate(lines, start=1):
            h = hashlib.sha256(line.encode()).hexdigest()
            kind = "new" if line_no not in old else "unchanged" if old[line_no] == h else "changed"
            stats.changes[kind] += 1
            if kind != "unchanged":
                new_rows.append((source, line_no, h, line, load_id))
                changes.append((load_id, source, line_no, kind, old.get(line_no), h))
        removed = [n for n in old if n > len(lines)]
        stats.changes["removed"] += len(removed)
        changes += [(load_id, source, n, "removed", old[n], None) for n in removed]

        if new_rows:
            con.executemany("INSERT INTO raw.home_line_version VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", new_rows)
        if changes:
            con.executemany("INSERT INTO ops.home_line_change VALUES (?, ?, ?, ?, ?, ?)", changes)
        con.execute("DELETE FROM raw.home_line_current WHERE source_file = ?", [source])
        if lines:
            con.executemany("INSERT INTO raw.home_line_current VALUES (?, ?, ?, ?)",
                            [(source, n, hashlib.sha256(l.encode()).hexdigest(), load_id)
                             for n, l in enumerate(lines, start=1)])
    return stats


def current_lines(con: duckdb.DuckDBPyConnection, source: str) -> list[tuple[int, str, str]]:
    return con.execute("""
        SELECT c.line_no, c.line_sha256, v.raw_line
        FROM raw.home_line_current c
        JOIN raw.home_line_version v USING (source_file, line_no, line_sha256)
        WHERE c.source_file = ? ORDER BY c.line_no""", [source]).fetchall()


# --- 2. Cleaning -----------------------------------------------------------


@dataclass(frozen=True)
class Enrollment:
    patient_id: str               # study ID (D30)
    timezone: str
    consent: bool
    discharge_at: datetime        # aware UTC
    monitoring_end: datetime      # aware UTC
    deceased_at: datetime | None  # aware UTC
    source_file: str
    line_no: int


class Rejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason, self.detail = reason, detail


@dataclass
class Candidate:
    reading_id: str
    submission_id: str
    patient_id: str
    measured_at: datetime         # aware UTC, after any time-zone correction
    measured_at_source: str       # as submitted, so a correction is visible on the record
    received_at: datetime         # aware UTC
    systolic: float
    diastolic: float
    flags: list[str]
    source_file: str
    line_no: int
    line_sha256: str


def as_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise Rejected("missing_field" if value is None else "non_numeric", name)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        raise Rejected("non_numeric", f"{name}={value!r}") from None


def parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise Rejected("missing_field" if value is None else "invalid_timestamp", name)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise Rejected("invalid_timestamp", f"{name}={value!r}") from None
    if dt.tzinfo is None:
        raise Rejected("invalid_timestamp", f"{name} has no UTC offset: {value!r}")
    return dt


def clean_reading(record: Any, enr: dict[str, Enrollment], cfg: dict[str, Any],
                  source: str, line_no: int, line_hash: str) -> Candidate:
    """Validate and normalize one submission. Raises Rejected with a reason."""
    c = cfg["cleaning"]
    if not isinstance(record, dict):
        raise Rejected("invalid_json", "not a JSON object")
    for name in ("submission_id", "patient_ref", "measured_at", "received_at", "systolic", "diastolic", "unit"):
        if record.get(name) in (None, ""):
            raise Rejected("missing_field", name)
    patient = enr.get(record["patient_ref"])
    if patient is None:
        raise Rejected("unknown_patient", "not in the enrollment roster or the cohort")

    flags: list[str] = []
    systolic, diastolic = as_number(record["systolic"], "systolic"), as_number(record["diastolic"], "diastolic")
    unit = str(record["unit"]).strip().lower()
    if unit == "kpa":
        systolic, diastolic = round(systolic * c["kpa_to_mmhg"]), round(diastolic * c["kpa_to_mmhg"])
        flags.append("unit_converted_kpa")
    elif unit not in MMHG_UNITS:
        raise Rejected("unknown_unit", f"unit={record['unit']!r}")

    lo_s, hi_s = c["systolic_range"]
    lo_d, hi_d = c["diastolic_range"]
    if not (lo_s <= systolic <= hi_s and lo_d <= diastolic <= hi_d and diastolic < systolic):
        raise Rejected("implausible_value", f"{systolic:g}/{diastolic:g} mm Hg")

    measured = parse_time(record["measured_at"], "measured_at")
    received = parse_time(record["received_at"], "received_at").astimezone(UTC)
    # A device that labels local wall-clock time with the wrong offset: keep the wall-clock
    # time and apply the patient's zone. Assumes patients stay in their enrolled zone (D22).
    tz = ZoneInfo(patient.timezone)
    wall = measured.replace(tzinfo=None)
    expected_offset = wall.replace(tzinfo=tz).utcoffset()
    if measured.utcoffset() != expected_offset:
        flags.append("tz_corrected")
        measured = wall.replace(tzinfo=tz)
    measured = measured.astimezone(UTC)

    if patient.deceased_at is not None and measured >= patient.deceased_at:
        raise Rejected("after_death", f"measured {measured:%Y-%m-%d %H:%M}Z")
    if not patient.discharge_at <= measured <= patient.monitoring_end:
        raise Rejected("outside_monitoring_window", f"measured {measured:%Y-%m-%d %H:%M}Z")
    if received - measured > timedelta(hours=c["late_after_hours"]):
        flags.append("late")

    return Candidate(f"{source}#{line_no}", record["submission_id"], patient.patient_id, measured,
                     record["measured_at"], received, systolic, diastolic, flags, source, line_no, line_hash)


def deduplicate(cands: list[Candidate], cfg: dict[str, Any]) -> tuple[list[Candidate], list[tuple[Candidate, str, str, str]]]:
    """Keep the earliest-received of each exact resend (same submission_id) or near-duplicate."""
    c = cfg["cleaning"]
    window, tol = timedelta(minutes=c["near_duplicate_minutes"]), c["near_duplicate_mmhg"]
    kept: list[Candidate] = []
    by_submission: dict[str, Candidate] = {}
    by_patient: dict[str, list[Candidate]] = {}
    dupes = []
    for cand in sorted(cands, key=lambda x: (x.received_at, x.line_no)):
        if cand.submission_id in by_submission:
            first = by_submission[cand.submission_id]
            dupes.append((cand, "duplicate_exact", f"same submission_id as {first.reading_id}", first.reading_id))
            continue
        near = next((k for k in by_patient.get(cand.patient_id, [])
                     if abs(k.measured_at - cand.measured_at) <= window
                     and abs(k.systolic - cand.systolic) <= tol and abs(k.diastolic - cand.diastolic) <= tol), None)
        if near is not None:
            dupes.append((cand, "duplicate_near",
                          f"within {c['near_duplicate_minutes']} min and {tol} mm Hg of {near.reading_id}",
                          near.reading_id))
            continue
        kept.append(cand)
        by_submission[cand.submission_id] = cand
        by_patient.setdefault(cand.patient_id, []).append(cand)
    return kept, dupes


def load_enrollment(con: duckdb.DuckDBPyConnection, source: str, monitoring_days_max: int
                    ) -> tuple[dict[str, Enrollment], list[tuple]]:
    """Roster lines joined to the cohort (the source of truth for discharge and death).

    The vendor's roster is keyed by record number. The result is keyed the same way, for looking
    up incoming readings, but every Enrollment carries the study ID, which is all that's written
    out (D30). Problems are reported by line number, never by record number."""
    cohort = {r[0]: r[1:] for r in con.execute("""
        SELECT k.patient_id, c.patient_id, c.discharge_at::TIMESTAMP, c.deceased_at::TIMESTAMP
        FROM cohort.postpartum_cohort c JOIN phi.patient_key k ON k.study_id = c.patient_id""").fetchall()}
    out, problems = {}, []
    for line_no, _, line in current_lines(con, source):
        rec = json.loads(line)
        mrn = rec["patient_ref"]
        if mrn not in cohort:
            problems.append((line_no, "enrollment_not_in_cohort"))
            continue
        if not 1 <= rec["monitoring_days"] <= monitoring_days_max:
            problems.append((line_no, "enrollment_bad_monitoring_days"))
            continue
        study, *dates = cohort[mrn]
        discharge, deceased = (d.replace(tzinfo=UTC) if d else None for d in dates)
        # Through the end of the last postpartum day, in the patient's local time.
        tz = ZoneInfo(rec["timezone"])
        last_day = discharge.astimezone(tz).date() + timedelta(days=rec["monitoring_days"] + 1)
        end = datetime.combine(last_day, time(0, 0), tz).astimezone(UTC)
        if deceased is not None:
            end = min(end, deceased)
        out[mrn] = Enrollment(study, rec["timezone"], bool(rec["consent_home_monitoring"]),
                              discharge, end, deceased, source, line_no)
    return out, problems


def postpartum_day(measured: datetime, discharge: datetime, tz: ZoneInfo) -> int:
    return (measured.astimezone(tz).date() - discharge.astimezone(tz).date()).days


CLINICAL_SQL = """
WITH obs AS (
    SELECT v.resource_id AS record_id,
           regexp_extract(v.resource_json->>'$.subject.reference', '([^/:]+)$', 1) AS record_number,
           (v.resource_json->>'$.effectiveDateTime')::TIMESTAMPTZ AS measured_at,
           v.resource_json->'$.component' AS components
    FROM raw.fhir_resource_current c
    JOIN raw.fhir_resource_version v USING (resource_type, resource_id, content_hash)
    WHERE c.resource_type = 'Observation'
      AND list_contains(json_extract_string(v.resource_json, '$.code.coding[*].code'), $panel)
)
SELECT k.study_id, o.record_id, o.measured_at::TIMESTAMP,
       list_filter(json_transform(o.components, '[{"code":{"coding":[{"code":"VARCHAR"}]},"valueQuantity":{"value":"DOUBLE"}}]'),
                   x -> x.code.coding[1].code = $systolic)[1].valueQuantity.value AS systolic,
       list_filter(json_transform(o.components, '[{"code":{"coding":[{"code":"VARCHAR"}]},"valueQuantity":{"value":"DOUBLE"}}]'),
                   x -> x.code.coding[1].code = $diastolic)[1].valueQuantity.value AS diastolic
FROM obs o
JOIN phi.patient_key k ON k.patient_id = o.record_number      -- record number -> study ID (D30)
JOIN home.enrollment e ON e.patient_id = k.study_id
WHERE o.measured_at BETWEEN e.discharge_at - to_days($lookback) AND e.monitoring_end
"""


@dataclass
class CleanStats:
    clean_id: int = 0
    lines_in: int = 0
    accepted: int = 0
    rejections: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    enrollment_problems: int = 0
    clinical_points: int = 0
    clinical_skipped: int = 0


def clean(con: duckdb.DuckDBPyConnection, cfg: dict[str, Any], enrollment_source: str, readings_source: str,
          raw_load_id: int, lookback_days: int) -> CleanStats:
    stats = CleanStats()
    stats.clean_id = con.execute("SELECT nextval('home.clean_id_seq')").fetchone()[0]
    for table in ("home.enrollment", "home.reading", "home.reading_rejection", "timeline.bp_reading",
                  "timeline.skipped_panel"):
        con.execute(f"DELETE FROM {table}")

    enr, problems = load_enrollment(con, enrollment_source, cfg["monitoring"]["max_days"])
    stats.enrollment_problems = len(problems)
    if problems:
        raise RuntimeError(f"Enrollment roster has {len(problems)} unusable lines, e.g. line {problems[0][0]}: "
                           f"{problems[0][1]}")
    # Enrollment number: discharge order, which doesn't depend on the pseudonym key, so documents
    # can name a patient the same way on any machine (D30). Discharge times are unique in this data;
    # the roster line breaks any tie.
    ordered = sorted(enr.values(), key=lambda e: (e.discharge_at, e.line_no))
    con.executemany("INSERT INTO home.enrollment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (e.patient_id, n, stats.clean_id, e.timezone, e.consent, e.discharge_at, e.monitoring_end,
         e.deceased_at, e.source_file, e.line_no) for n, e in enumerate(ordered, start=1)])
    by_study = {e.patient_id: e for e in enr.values()}

    cands, rejected = [], []
    for line_no, line_hash, line in current_lines(con, readings_source):
        stats.lines_in += 1
        rid = f"{readings_source}#{line_no}"
        record: Any = None
        try:
            record = json.loads(line)
            cands.append(clean_reading(record, enr, cfg, readings_source, line_no, line_hash))
        except json.JSONDecodeError as exc:
            rejected.append((rid, None, None, "invalid_json", str(exc), None, line_no, line_hash))
        except Rejected as rej:
            sub = record.get("submission_id") if isinstance(record, dict) else None
            known = enr.get(record.get("patient_ref")) if isinstance(record, dict) else None
            pid = known.patient_id if known else None   # study ID, never the record number
            rejected.append((rid, sub, pid, rej.reason, rej.detail, None, line_no, line_hash))

    kept, dupes = deduplicate(cands, cfg)
    for cand, reason, detail, dup_of in dupes:
        rejected.append((cand.reading_id, cand.submission_id, cand.patient_id, reason, detail, dup_of,
                         cand.line_no, cand.line_sha256))

    rows = []
    for k in kept:
        e = by_study[k.patient_id]
        tz = ZoneInfo(e.timezone)
        rows.append((k.reading_id, stats.clean_id, k.submission_id, k.patient_id, k.measured_at,
                     k.measured_at_source, k.received_at,
                     postpartum_day(k.measured_at, e.discharge_at, tz),
                     (k.measured_at - e.discharge_at).total_seconds() / 3600,
                     k.systolic, k.diastolic, k.flags, k.source_file, k.line_no, k.line_sha256))
        stats.flags.update(k.flags)
    if rows:
        con.executemany("INSERT INTO home.reading VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    if rejected:
        con.executemany("INSERT INTO home.reading_rejection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(r[0], stats.clean_id, *r[1:6], readings_source, *r[6:]) for r in rejected])
    stats.accepted = len(rows)
    stats.rejections = Counter(r[3] for r in rejected)

    # Timeline: home readings as cleaned, plus clinical BP panels (IDs and values only, D6).
    timeline = [(pid, "home", rid, m, day, hrs, s, d, f) for rid, _, _, pid, m, _, _, day, hrs, s, d, f, *_ in rows]
    skipped = []
    for pid, record_id, measured, systolic, diastolic in con.execute(CLINICAL_SQL, {
            "panel": LOINC_BP_PANEL, "systolic": LOINC_SYSTOLIC, "diastolic": LOINC_DIASTOLIC,
            "lookback": lookback_days}).fetchall():
        if systolic is None or diastolic is None:
            stats.clinical_skipped += 1   # a panel without both components can't be plotted
            skipped.append((record_id, stats.clean_id, pid, "missing_component"))
            continue
        e = by_study[pid]
        m = measured.replace(tzinfo=UTC)
        timeline.append((pid, "clinical", record_id, m, postpartum_day(m, e.discharge_at, ZoneInfo(e.timezone)),
                         (m - e.discharge_at).total_seconds() / 3600, systolic, diastolic, []))
        stats.clinical_points += 1
    if timeline:
        con.executemany("INSERT INTO timeline.bp_reading VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", timeline)
    if skipped:
        con.executemany("INSERT INTO timeline.skipped_panel VALUES (?, ?, ?, ?)", skipped)

    con.execute("INSERT INTO home.clean_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        stats.clean_id, datetime.now(UTC), cfg["version"], cfg["sha256"], raw_load_id, stats.lines_in,
        stats.accepted, sum(stats.rejections.values()),
        stats.rejections["duplicate_exact"] + stats.rejections["duplicate_near"]])
    return stats


# --- Run -------------------------------------------------------------------


def run(db_path: Path = DB_PATH, config_path: Path = CONFIG_PATH, in_dir: Path = OUT_DIR,
        lookback_days: int = 280) -> tuple[RawStats, CleanStats]:
    cfg = load_config(config_path)
    files = [in_dir / ENROLLMENT_FILE, in_dir / READINGS_FILE]
    with connect(db_path) as con:
        started = datetime.now(UTC)
        con.begin()
        try:
            raw = load_raw(con, files)
            stats = clean(con, cfg, display_path(files[0]), display_path(files[1]), raw.load_id, lookback_days)
            ch = raw.changes
            con.execute(
                "INSERT INTO ops.load_run VALUES (?, 'home', ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, NULL)",
                [raw.load_id, started, datetime.now(UTC), raw.files_in, raw.records_in,
                 ch["new"], ch["changed"], ch["unchanged"], sum(stats.rejections.values())])
            con.commit()
        except Exception as exc:
            con.rollback()
            con.execute("INSERT INTO ops.load_run (load_id, kind, started_at, finished_at, status, files_in, error) "
                        "VALUES (nextval('ops.load_id_seq'), 'home', ?, ?, 'failed', ?, ?)",
                        [started, datetime.now(UTC), len(files), repr(exc)])
            raise
    return raw, stats


def print_report(raw: RawStats, stats: CleanStats, db_path: Path = DB_PATH) -> None:
    ch = raw.changes
    print(f"== Home load {raw.load_id}")
    print(f"   files in: {raw.files_in}   lines in: {raw.records_in:,}   "
          f"new {ch['new']}, changed {ch['changed']}, unchanged {ch['unchanged']}, removed {ch['removed']}")
    print(f"== Cleaning run {stats.clean_id}")
    print(f"   reading lines:   {stats.lines_in:,}")
    print(f"   accepted:        {stats.accepted:,}")
    print(f"   rejected:        {sum(stats.rejections.values()):,}")
    for reason, n in sorted(stats.rejections.items()):
        print(f"     {reason:<28} {n}")
    print("   accepted with flags:")
    for flag in ("tz_corrected", "unit_converted_kpa", "late"):
        print(f"     {flag:<28} {stats.flags[flag]}")
    print(f"   timeline: {stats.accepted:,} home + {stats.clinical_points} clinical points"
          f"{f' ({stats.clinical_skipped} clinical panels skipped: missing a component)' if stats.clinical_skipped else ''}")
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute("""
            SELECT count(DISTINCT patient_id) FILTER (WHERE systolic >= 160 OR diastolic >= 110),
                   count(*) FILTER (WHERE systolic >= 160 OR diastolic >= 110),
                   count(DISTINCT patient_id) FILTER (WHERE (systolic >= 140 OR diastolic >= 90)
                                                     AND NOT (systolic >= 160 OR diastolic >= 110)),
                   count(DISTINCT patient_id)
            FROM home.reading""").fetchone()
        consent = con.execute("SELECT count(*) FILTER (WHERE NOT consent_home_monitoring), count(*) "
                              "FROM home.enrollment").fetchone()
    print(f"   severe-range home readings (>=160 and/or >=110): {rows[1]} from {rows[0]} patients")
    print(f"   patients with an elevated (>=140/90), non-severe reading: {rows[2]}")
    print(f"   patients with accepted readings: {rows[3]}   without consent: {consent[0]} of {consent[1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--in-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    raw, stats = run(args.db, args.config, args.in_dir)
    print_report(raw, stats, args.db)


if __name__ == "__main__":
    main()
