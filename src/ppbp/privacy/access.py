"""Phase 5b: the access layer. Every read of alerts, timelines or contact details goes through
here, is checked against config/access.toml, and is logged to audit.access_log (D32).

    from ppbp.privacy.access import AccessLayer, Actor
    nurse = AccessLayer(Actor("nurse.1", "nurse"))
    nurse.list_alerts()

The Phase 7 API calls these methods. The render_* functions build each response without logging,
so the leak check can inspect exactly what each role would receive (D33).

Responses identify patients by study ID only. Contact details come only from read_contact, which
needs an alert for that patient that the nurse can see; the alert ID becomes the logged reason,
so no free text that could hold identifying details reaches the log.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from ppbp import DATA_DIR, DB_PATH, PROJECT_ROOT
from ppbp.alerts.explain import AlertNotFound, explain_alert
from ppbp.db import connect

ACCESS_PATH = PROJECT_ROOT / "config" / "access.toml"
QUALITY_REPORT = DATA_DIR / "out" / "reports" / "quality_report.json"
LEAK_REPORT = DATA_DIR / "out" / "privacy" / "leak_check.json"


class AccessDenied(PermissionError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class Actor:
    name: str
    role: str


def load_policy(path: Path = ACCESS_PATH) -> dict[str, set[str]]:
    cfg = tomllib.loads(path.read_text())
    return {role: set(spec["actions"]) for role, spec in cfg["roles"].items()}


def _rows(db_path: Path, sql: str, params: list | None = None) -> list[dict[str, Any]]:
    with duckdb.connect(str(db_path), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --- Responses (no logging) ------------------------------------------------


def render_alert_list(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Open and closed alerts, most urgent first. Held-back alerts never appear (D31)."""
    return _rows(db_path, """
        SELECT alert_id, severity, label, status, patient_id, postpartum_day,
               round(days_since_discharge, 1) AS days_since_discharge, triggered_at::VARCHAR AS triggered_at,
               updated_at::VARCHAR AS updated_at, summary
        FROM alerts.alert WHERE status <> 'held_back'
        ORDER BY severity_rank, status DESC, triggered_at, alert_id""")


def render_nurse_alert(alert_id: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    """What a nurse needs to act: the alert, the threshold, the readings behind it, recent readings."""
    e = explain_alert(alert_id, db_path)
    readings = [{"reading_id": i["record_id"], "role": i["role"],
                 **{k: i["reading"][k] for k in ("measured_at", "postpartum_day", "systolic", "diastolic", "flags")},
                 "rule_outcome": (i["rule_result"] or {}).get("outcome")}
                for i in e["evidence"] if i["record_type"] == "home_reading"]
    rule = {k: e["rule"][k] for k in ("rule_id", "rule_version", "label", "severity", "params", "description", "source")}
    return {"alert": e["alert"], "rule": rule, "readings": readings, "patient": e["patient"],
            "recent_readings": e["context"], "hold": e["hold"]}


def render_timeline(patient_id: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    enrol = _rows(db_path, """SELECT patient_id, consent_home_monitoring, discharge_at::VARCHAR AS discharge_at,
                                     monitoring_end::VARCHAR AS monitoring_end, deceased_at::VARCHAR AS deceased_at
                              FROM home.enrollment WHERE patient_id = ?""", [patient_id])
    if not enrol:
        raise NotFound(patient_id)
    consent = enrol[0].pop("consent_home_monitoring")
    # Without home-monitoring consent, home readings are withheld from view as well as from alerting (D31).
    sources = ["home", "clinical"] if consent else ["clinical"]
    # Each reading says what the alert rules made of it, so readings that didn't raise an alert stay
    # visible: the alert reduction is prioritization, not hidden data (Phase 7 carry-forward, D42).
    points = _rows(db_path, """
        SELECT t.source, t.record_id, t.measured_at::VARCHAR AS measured_at, t.postpartum_day,
               t.systolic, t.diastolic, t.flags,
               CASE WHEN t.source = 'clinical' THEN 'clinical'
                    WHEN sev.outcome = 'fired' THEN 'severe'
                    WHEN elv.outcome = 'fired' THEN 'elevated_in_alert'
                    WHEN elv.outcome = 'unconfirmed' THEN 'elevated_unconfirmed'
                    ELSE 'normal' END AS mark,
               coalesce((SELECT list(e.alert_id ORDER BY e.alert_id) FROM alerts.alert_evidence e
                         JOIN alerts.alert a USING (alert_id)
                         WHERE e.record_id = t.record_id AND e.role = 'trigger' AND a.status <> 'held_back'),
                        []) AS alert_ids
        FROM timeline.bp_reading t
        LEFT JOIN alerts.validation_result sev ON sev.rule_id = 'BP_SEVERE' AND sev.record_id = t.record_id
        LEFT JOIN alerts.validation_result elv ON elv.rule_id = 'BP_ELEVATED' AND elv.record_id = t.record_id
        WHERE t.patient_id = ? AND list_contains(?, t.source)
        ORDER BY t.measured_at, t.record_id""", [patient_id, sources])
    for p in points:
        p["mark_label"] = MARK_LABELS[p["mark"]]
    return {"patient": enrol[0], "home_readings_withheld": None if consent else "no_home_monitoring_consent",
            "readings": points}


MARK_LABELS = {
    "severe": "Severe range: raised its own alert",
    "elevated_in_alert": "Elevated: part of an elevated-readings alert",
    "elevated_unconfirmed": "Elevated, unconfirmed: no second elevated reading 4+ hours apart, so no alert",
    "normal": "Below the alert thresholds",
    "clinical": "Clinic reading (context only, not evaluated by the alert rules)",
}


def render_patient_list(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Every enrolled patient, by study ID and enrollment number, with counts only. Lets a nurse reach a
    timeline whose readings raised no alert, e.g. an unconfirmed elevated reading (D42)."""
    return _rows(db_path, """
        SELECT e.patient_id, e.enrollment_no, e.discharge_at::VARCHAR AS discharge_at,
               e.consent_home_monitoring AS home_monitoring_consent,
               (SELECT count(*) FROM home.reading r WHERE r.patient_id = e.patient_id) AS home_readings,
               (SELECT count(*) FROM alerts.alert a WHERE a.patient_id = e.patient_id AND a.status = 'open') AS open_alerts,
               (SELECT count(*) FROM alerts.validation_result v WHERE v.patient_id = e.patient_id
                  AND v.rule_id = 'BP_ELEVATED' AND v.outcome = 'unconfirmed') AS unconfirmed_elevated
        FROM home.enrollment e ORDER BY e.enrollment_no""")


def render_load_summary(db_path: Path = DB_PATH, recent: int = 10) -> dict[str, Any]:
    """What came in, what changed, what was rejected and why: per load, counts only (R2).
    File names are left out: Synthea's carry patient names (D33)."""
    loads = _rows(db_path, """
        SELECT load_id, kind, started_at::VARCHAR AS started_at, status, files_in, records_in, new, changed,
               unchanged, rejected, error
        FROM ops.load_run ORDER BY load_id DESC LIMIT ?""", [recent])
    fhir_reasons = {r["load_id"]: r["reasons"] for r in _rows(db_path, """
        SELECT load_id, map_from_entries(list(struct_pack(k := reason, v := n))) AS reasons
        FROM (SELECT load_id, reason, count(*) n FROM raw.load_rejection GROUP BY ALL) GROUP BY 1""")}
    latest_clean = _rows(db_path, """SELECT clean_id, raw_load_id, lines_in, accepted, rejected, duplicates
                                     FROM home.clean_run ORDER BY clean_id DESC LIMIT 1""")
    home_reasons = {r["reason"]: r["n"] for r in _rows(db_path, """
        SELECT reason, count(*) n FROM home.reading_rejection GROUP BY 1 ORDER BY 2 DESC""")}
    for load in loads:
        load["rejected_by_reason"] = fhir_reasons.get(load["load_id"], {}) if load["kind"] == "fhir" else (
            home_reasons if latest_clean and load["load_id"] == latest_clean[0]["raw_load_id"] else None)
    return {"loads": loads, "latest_home_cleaning": latest_clean[0] if latest_clean else None,
            "note": "Home loads are cleaned after loading: rejections and duplicates removed are given for the "
                    "load the latest cleaning run used. Source file names are not shown (they can identify "
                    "patients)."}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise NotFound(str(path.name))
    return json.loads(path.read_text())


def render_quality_report(path: Path = QUALITY_REPORT) -> dict[str, Any]:
    return _read_json(path)


def render_leak_check(path: Path = LEAK_REPORT) -> dict[str, Any]:
    return _read_json(path)


def hold_summary(db_path: Path = DB_PATH) -> dict[str, Any]:
    """Counts of held-back alerts, with no patient details. Held severe-range alerts are flagged
    for policy review: a real service needs a safety escalation path for them (D31)."""
    rows = _rows(db_path, """
        SELECT a.rule_id, a.label, a.severity, h.reason, count(*) AS alerts
        FROM alerts.alert_hold h JOIN alerts.alert a USING (alert_id) GROUP BY ALL ORDER BY 1""")
    held_severe = sum(r["alerts"] for r in rows if r["severity"] == "Critical")
    review = ([{"flag": "POLICY_REVIEW", "issue": "severe-range alerts held back for lack of home-monitoring consent",
                "alerts": held_severe,
                "why": "held as R7 requires, but a real service needs a safety escalation path (DECISIONS D31)"}]
              if held_severe else [])
    return {"held_back_total": sum(r["alerts"] for r in rows), "by_rule": rows,
            "held_severe": held_severe, "policy_review": review}


def render_holds(db_path: Path = DB_PATH) -> dict[str, Any]:
    alerts = _rows(db_path, """
        SELECT h.alert_id, a.rule_id, a.severity, a.patient_id, h.reason, h.status_if_held, h.detail
        FROM alerts.alert_hold h JOIN alerts.alert a USING (alert_id) ORDER BY a.severity_rank, h.alert_id""")
    return {"summary": hold_summary(db_path), "alerts": alerts}


def render_access_log(db_path: Path = DB_PATH, limit: int = 500) -> list[dict[str, Any]]:
    return _rows(db_path, """
        SELECT access_id, accessed_at::VARCHAR AS accessed_at, actor, role, action, resource_type, resource_id,
               patient_id, outcome, reason
        FROM audit.access_log ORDER BY access_id DESC LIMIT ?""", [limit])


# --- The layer -------------------------------------------------------------


class AccessLayer:
    def __init__(self, actor: Actor, db_path: Path = DB_PATH, policy_path: Path = ACCESS_PATH,
                 quality_report: Path = QUALITY_REPORT, leak_report: Path = LEAK_REPORT) -> None:
        policy = load_policy(policy_path)
        if actor.role not in policy:
            raise ValueError(f"Unknown role {actor.role!r}")
        self.actor, self.db_path, self.actions = actor, db_path, policy[actor.role]
        self.quality_report, self.leak_report = quality_report, leak_report

    def _log(self, action: str, resource_type: str, resource_id: str | None, patient_id: str | None,
             outcome: str, reason: str | None = None) -> None:
        with connect(self.db_path) as con:
            con.execute("INSERT INTO audit.access_log VALUES (nextval('audit.access_id_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [datetime.now(UTC), self.actor.name, self.actor.role, action, resource_type, resource_id,
                         patient_id, outcome, reason])

    def _guard(self, action: str, resource_type: str, resource_id: str | None = None,
               patient_id: str | None = None) -> None:
        if action not in self.actions:
            self._log(action, resource_type, resource_id, patient_id, "denied", f"role {self.actor.role} may not {action}")
            raise AccessDenied(f"role {self.actor.role} may not {action}")

    def _alert_owner(self, alert_id: str) -> tuple[str, str] | None:
        rows = _rows(self.db_path, "SELECT patient_id, status FROM alerts.alert WHERE alert_id = ?", [alert_id])
        return (rows[0]["patient_id"], rows[0]["status"]) if rows else None

    def list_alerts(self) -> list[dict[str, Any]]:
        self._guard("list_alerts", "alert_list")
        out = render_alert_list(self.db_path)
        self._log("list_alerts", "alert_list", None, None, "allowed", f"{len(out)} alerts returned")
        return out

    def list_patients(self) -> list[dict[str, Any]]:
        self._guard("list_patients", "patient_list")
        out = render_patient_list(self.db_path)
        for p in out:
            if not p["home_monitoring_consent"]:   # counts of withheld readings would reveal them (D31)
                p["home_readings"] = p["unconfirmed_elevated"] = None
        self._log("list_patients", "patient_list", None, None, "allowed", f"{len(out)} patients")
        return out

    def read_alert(self, alert_id: str) -> dict[str, Any]:
        owner = self._alert_owner(alert_id)
        self._guard("read_alert", "alert", alert_id, owner[0] if owner else None)
        if owner is None:
            self._log("read_alert", "alert", alert_id, None, "not_found")
            raise NotFound(alert_id)
        if owner[1] == "held_back":
            self._log("read_alert", "alert", alert_id, owner[0], "denied", "held_back: no_home_monitoring_consent")
            raise AccessDenied("alert held back: no home-monitoring consent")
        out = render_nurse_alert(alert_id, self.db_path)
        self._log("read_alert", "alert", alert_id, owner[0], "allowed")
        return out

    def explain_alert(self, alert_id: str) -> dict[str, Any]:
        owner = self._alert_owner(alert_id)
        self._guard("explain_alert", "alert", alert_id, owner[0] if owner else None)
        try:
            out = explain_alert(alert_id, self.db_path)
        except AlertNotFound:
            self._log("explain_alert", "alert", alert_id, None, "not_found")
            raise NotFound(alert_id) from None
        self._log("explain_alert", "alert", alert_id, owner[0], "allowed")
        return out

    def read_timeline(self, patient_id: str) -> dict[str, Any]:
        self._guard("read_timeline", "timeline", patient_id, patient_id)
        try:
            out = render_timeline(patient_id, self.db_path)
        except NotFound:
            self._log("read_timeline", "timeline", patient_id, None, "not_found")
            raise
        self._log("read_timeline", "timeline", patient_id, patient_id, "allowed",
                  "home readings withheld: no consent" if out["home_readings_withheld"] else None)
        return out

    def read_contact(self, patient_id: str, alert_id: str) -> dict[str, Any]:
        """Display name and phone, only while acting on a visible alert for this patient."""
        self._guard("read_contact", "contact", patient_id, patient_id)
        owner = self._alert_owner(alert_id)
        if owner is None or owner[0] != patient_id or owner[1] == "held_back":
            why = ("no such alert" if owner is None else "alert is for another patient" if owner[0] != patient_id
                   else "alert held back: no home-monitoring consent")
            self._log("read_contact", "contact", patient_id, patient_id, "denied", f"{why} ({alert_id})")
            raise AccessDenied(why)
        rows = _rows(self.db_path, "SELECT display_name, phone FROM phi.patient_contact WHERE study_id = ?", [patient_id])
        if not rows:
            self._log("read_contact", "contact", patient_id, patient_id, "not_found", f"acting on {alert_id}")
            raise NotFound(patient_id)
        self._log("read_contact", "contact", patient_id, patient_id, "allowed", f"acting on {alert_id}")
        return {"patient_id": patient_id, **rows[0]}

    def read_access_log(self, limit: int = 500) -> list[dict[str, Any]]:
        self._guard("read_access_log", "access_log")
        out = render_access_log(self.db_path, limit)   # read before this call is logged
        self._log("read_access_log", "access_log", None, None, "allowed", f"{len(out)} entries returned")
        return out

    def read_loads(self) -> dict[str, Any]:
        self._guard("read_loads", "load_summary")
        out = render_load_summary(self.db_path)
        self._log("read_loads", "load_summary", None, None, "allowed", f"{len(out['loads'])} loads")
        return out

    def read_quality_report(self) -> dict[str, Any]:
        self._guard("read_quality_report", "quality_report")
        try:
            out = render_quality_report(self.quality_report)
        except NotFound:
            self._log("read_quality_report", "quality_report", None, None, "not_found")
            raise
        self._log("read_quality_report", "quality_report", str(out["quality_run"]["run_id"]), None, "allowed")
        return out

    def read_leak_check(self) -> dict[str, Any]:
        self._guard("read_leak_check", "leak_check")
        try:
            out = render_leak_check(self.leak_report)
        except NotFound:
            self._log("read_leak_check", "leak_check", None, None, "not_found")
            raise
        self._log("read_leak_check", "leak_check", None, None, "allowed", "passed" if out["passed"] else "FAILED")
        return out

    def read_holds(self) -> list[dict[str, Any]]:
        self._guard("read_holds", "alert_hold")
        out = render_holds(self.db_path)
        self._log("read_holds", "alert_hold", None, None, "allowed", f"{len(out['alerts'])} held-back alerts")
        return out
