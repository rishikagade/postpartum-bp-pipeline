"""Phase 5: explain_alert(alert_id), from the alert down to the raw records (R5).

    python -m ppbp.alerts.explain ALR-xxxxxxxxxxxx

Returns the alert, the rule and its version (with the rules file hash for that run), each
evidence record with its rule result, and for every home reading the chain:
clean reading -> exact raw line (text and SHA-256) -> the load and file hash that brought it in
-> the cleaning run and its config hash. Also returns the patient's readings in the
`context_hours` before the alert. IDs and values only; no identifying data (R7).

Raw lines are shown redacted: the vendor's record number and device ID are replaced, because
they identify the patient (D30). `line_sha256` is the hash of the original line, so anyone with
raw-zone access can confirm the redacted line is the same record.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import duckdb

from ppbp import DB_PATH
from ppbp.alerts.evaluate import RULES_PATH, load_rules


class AlertNotFound(KeyError):
    pass


# Fields in a raw home export line that identify the patient or their device (D30).
REDACT = {"patient_ref": "[record number withheld: see study ID]", "device_id": "[device ID withheld]"}


def redact_line(raw_line: str) -> tuple[str, list[str]]:
    """The raw line with identifying fields replaced, and the names of the fields replaced."""
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return "[unparseable line withheld]", ["*"]
    if not isinstance(record, dict):
        return "[unparseable line withheld]", ["*"]
    hidden = [k for k in REDACT if k in record]
    return json.dumps({k: REDACT.get(k, v) for k, v in record.items()}), hidden


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict[str, Any]]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> dict[str, Any] | None:
    rows = _rows(con, sql, params)
    return rows[0] if rows else None


def explain_alert(alert_id: str, db_path: Path = DB_PATH, rules_path: Path = RULES_PATH) -> dict[str, Any]:
    with duckdb.connect(str(db_path), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        # Timestamps as ISO text: readable, and no pytz needed.
        alert = _one(con, """
            SELECT alert_id, label, severity, rule_id, rule_version, patient_id, triggered_at::VARCHAR AS triggered_at,
                   updated_at::VARCHAR AS updated_at, postpartum_day, round(days_since_discharge, 2) AS days_since_discharge,
                   threshold, summary, status, closed_at::VARCHAR AS closed_at, run_id
            FROM alerts.alert WHERE alert_id = ?""", [alert_id])
        if alert is None:
            raise AlertNotFound(alert_id)

        run = _one(con, """SELECT run_id, run_at::VARCHAR AS run_at, rules_version, rules_sha256, clean_id
                           FROM alerts.rule_run WHERE run_id = ?""", [alert["run_id"]])
        rule = _one(con, """SELECT rule_id, rule_version, name, label, severity, kind, params, description, source
                            FROM alerts.rule WHERE rule_id = ?""", [alert["rule_id"]])
        rule["params"] = json.loads(rule["params"])
        rule["rules_file_version"], rule["rules_file_sha256"] = run["rules_version"], run["rules_sha256"]

        cleaning = _one(con, """SELECT clean_id, cleaned_at::VARCHAR AS cleaned_at, config_version, config_sha256,
                                       raw_load_id FROM home.clean_run WHERE clean_id = ?""", [run["clean_id"]])
        patient = _one(con, """
            SELECT patient_id, discharge_at::VARCHAR AS discharge_at, monitoring_end::VARCHAR AS monitoring_end,
                   deceased_at::VARCHAR AS deceased_at, timezone
            FROM home.enrollment WHERE patient_id = ?""", [alert["patient_id"]])

        evidence = []
        for ev in _rows(con, """SELECT role, record_type, record_id FROM alerts.alert_evidence
                                WHERE alert_id = ? ORDER BY role DESC, record_id""", [alert_id]):
            item: dict[str, Any] = dict(ev)
            item["rule_result"] = _one(con, """
                SELECT rule_id, rule_version, outcome, observed, detail FROM alerts.validation_result
                WHERE rule_id = ? AND record_type = ? AND record_id = ?""",
                [alert["rule_id"], ev["record_type"], ev["record_id"]])
            if ev["record_type"] == "home_reading":
                item["reading"] = _one(con, """
                    SELECT reading_id, submission_id, measured_at::VARCHAR AS measured_at, measured_at_source,
                           received_at::VARCHAR AS received_at, postpartum_day,
                           round(hours_since_discharge, 2) AS hours_since_discharge, systolic, diastolic, flags
                    FROM home.reading WHERE reading_id = ?""", [ev["record_id"]])
                item["raw"] = _one(con, """
                    SELECT r.source_file, r.line_no, r.line_sha256, v.raw_line,
                           v.first_load_id, cur.last_load_id
                    FROM home.reading r
                    JOIN raw.home_line_version v USING (source_file, line_no, line_sha256)
                    LEFT JOIN raw.home_line_current cur USING (source_file, line_no, line_sha256)
                    WHERE r.reading_id = ?""", [ev["record_id"]])
                item["raw"]["raw_line_redacted"], item["raw"]["redacted_fields"] = \
                    redact_line(item["raw"].pop("raw_line"))
                item["raw"]["loaded_by"] = _one(con, """
                    SELECT l.load_id, l.kind, l.started_at::VARCHAR AS started_at, f.file_sha256
                    FROM ops.load_run l JOIN raw.load_file f USING (load_id)
                    WHERE l.load_id = ? AND f.source_file = ?""",
                    [item["raw"]["first_load_id"], item["raw"]["source_file"]])
            evidence.append(item)

        hold = _one(con, """SELECT reason, status_if_held, detail FROM alerts.alert_hold WHERE alert_id = ?""",
                    [alert_id])
        context_hours = load_rules(rules_path)["context_hours"]
        context = _rows(con, """
            SELECT source, record_id, measured_at::VARCHAR AS measured_at, postpartum_day, systolic, diastolic, flags
            FROM timeline.bp_reading
            WHERE patient_id = ? AND measured_at <= ?::TIMESTAMPTZ
              AND measured_at > ?::TIMESTAMPTZ - to_hours(?)
            ORDER BY measured_at""", [alert["patient_id"], alert["triggered_at"], alert["triggered_at"], context_hours])

    alert.pop("run_id")
    return {
        "alert": alert,
        "rule": rule,
        "rule_run": run,
        "cleaning_run": cleaning,
        "patient": patient,
        "hold": hold,                      # set when the alert is held back for consent (D31)
        "evidence": evidence,
        "context": {"hours_before_alert": context_hours, "readings": context},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("alert_id")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    try:
        print(json.dumps(explain_alert(args.alert_id, args.db), indent=2, default=str))
    except AlertNotFound:
        raise SystemExit(f"No alert {args.alert_id!r} in the latest rule run") from None


if __name__ == "__main__":
    main()
