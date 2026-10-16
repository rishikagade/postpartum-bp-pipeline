"""Phase 5: evaluate the alert rules against clean home readings.

    python -m ppbp.alerts.evaluate          # run the rules, then print the nurse's alert list

Rules come from config/rules.toml (D24). Every reading gets one result per reading rule, and
every enrolled patient gets one result per silence rule, so each alert traces back to rule
results, and each result to a record. Alert ids are stable: the same data and rules give the
same alerts. Each run rebuilds the alerts tables in one transaction.

Only home readings are evaluated. Clinical BP panels are context on the timeline (D27).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from ppbp import DB_PATH, PROJECT_ROOT
from ppbp.db import connect

RULES_PATH = PROJECT_ROOT / "config" / "rules.toml"
RULE_KINDS = {"reading_threshold", "persistent_threshold", "patient_silence"}


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    data = path.read_bytes()
    cfg = tomllib.loads(data.decode())
    cfg["sha256"] = hashlib.sha256(data).hexdigest()
    ids = [r["id"] for r in cfg["rule"]]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate rule ids in {path}")
    for r in cfg["rule"]:
        if r["kind"] not in RULE_KINDS:
            raise ValueError(f"Rule {r['id']}: unknown kind {r['kind']!r}")
        if r["severity"] not in cfg["severity_order"]:
            raise ValueError(f"Rule {r['id']}: unknown severity {r['severity']!r}")
        if r.get("unless_rule") and r["unless_rule"] not in ids:
            raise ValueError(f"Rule {r['id']}: unless_rule {r['unless_rule']!r} is not a rule")
    return cfg


PARAM_KEYS = ("systolic_at_least", "diastolic_at_least", "unless_rule", "min_readings", "min_hours_apart",
              "close_after_hours", "hours")


def rule_params(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: rule[k] for k in PARAM_KEYS if k in rule}


def threshold_text(rule: dict[str, Any]) -> str:
    if rule["kind"] in ("reading_threshold", "persistent_threshold"):
        return f"systolic >= {rule['systolic_at_least']} and/or diastolic >= {rule['diastolic_at_least']} mm Hg"
    return f"no accepted reading for {rule['hours']} hours"


def alert_id(rule_id: str, patient_id: str, anchor: str) -> str:
    return "ALR-" + hashlib.sha256(f"{rule_id}|{patient_id}|{anchor}".encode()).hexdigest()[:12]


def fmt_bp(s: float, d: float) -> str:
    return f"{s:g}/{d:g} mm Hg"


# --- Inputs ----------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    reading_id: str
    patient_id: str
    measured_at: datetime        # aware UTC
    postpartum_day: int
    hours_since_discharge: float
    systolic: float
    diastolic: float


@dataclass(frozen=True)
class Window:
    patient_id: str
    timezone: str
    discharge_at: datetime       # aware UTC
    monitoring_end: datetime     # aware UTC; already capped at death (Phase 4)
    deceased_at: datetime | None


def read_inputs(con: duckdb.DuckDBPyConnection) -> tuple[int, list[Reading], list[Window]]:
    clean_id = con.execute("SELECT max(clean_id) FROM home.clean_run").fetchone()[0]
    if clean_id is None:
        raise RuntimeError("No cleaned home readings yet: run `make home` first")
    utc = lambda d: d.replace(tzinfo=UTC) if d else None  # noqa: E731
    readings = [Reading(r[0], r[1], utc(r[2]), *r[3:]) for r in con.execute("""
        SELECT reading_id, patient_id, measured_at::TIMESTAMP, postpartum_day, hours_since_discharge,
               systolic, diastolic
        FROM home.reading ORDER BY patient_id, measured_at, reading_id""").fetchall()]
    windows = [Window(r[0], r[1], utc(r[2]), utc(r[3]), utc(r[4])) for r in con.execute("""
        SELECT patient_id, timezone, discharge_at::TIMESTAMP, monitoring_end::TIMESTAMP, deceased_at::TIMESTAMP
        FROM home.enrollment ORDER BY patient_id""").fetchall()]
    return clean_id, readings, windows


# --- Evaluation ------------------------------------------------------------


@dataclass
class Result:
    rule: dict[str, Any]
    record_type: str
    record_id: str
    patient_id: str
    outcome: str                 # 'fired' | 'passed' | 'superseded' | 'unconfirmed'
    observed: str
    detail: str = ""


@dataclass
class Alert:
    alert_id: str
    rule: dict[str, Any]
    patient_id: str
    triggered_at: datetime
    updated_at: datetime
    postpartum_day: int
    days_since_discharge: float
    summary: str
    evidence: list[tuple[str, str, str]] = field(default_factory=list)  # (role, record_type, record_id)
    status: str = "open"
    closed_at: datetime | None = None


def meets(rule: dict[str, Any], r: Reading) -> bool:
    return r.systolic >= rule["systolic_at_least"] or r.diastolic >= rule["diastolic_at_least"]


def classify(rule: dict[str, Any], readings: list[Reading], rules_by_id: dict[str, dict]
             ) -> tuple[dict[str, Result], list[Reading]]:
    """One result per reading, plus the readings that meet this rule and no stronger one."""
    stronger = rules_by_id.get(rule.get("unless_rule", ""))
    results, qualifying = {}, []
    for r in readings:
        observed = fmt_bp(r.systolic, r.diastolic)
        if not meets(rule, r):
            results[r.reading_id] = Result(rule, "home_reading", r.reading_id, r.patient_id, "passed", observed)
        elif stronger is not None and meets(stronger, r):
            results[r.reading_id] = Result(rule, "home_reading", r.reading_id, r.patient_id, "superseded", observed,
                                           f"meets {stronger['id']}, which fires instead")
        else:
            results[r.reading_id] = Result(rule, "home_reading", r.reading_id, r.patient_id, "fired", observed,
                                           f"crossed {threshold_text(rule)}")
            qualifying.append(r)
    return results, qualifying


def evaluate_thresholds(rule: dict[str, Any], readings: list[Reading], rules_by_id: dict[str, dict]
                        ) -> tuple[list[Result], list[Alert]]:
    """Every qualifying reading raises its own alert (BP_SEVERE)."""
    results, qualifying = classify(rule, readings, rules_by_id)
    alerts = [Alert(alert_id(rule["id"], r.patient_id, r.reading_id), rule, r.patient_id, r.measured_at,
                    r.measured_at, r.postpartum_day, r.hours_since_discharge / 24,
                    f"{rule['label']}: {fmt_bp(r.systolic, r.diastolic)} on postpartum day {r.postpartum_day} "
                    f"(threshold: {threshold_text(rule)})",
                    [("trigger", "home_reading", r.reading_id)])
              for r in qualifying]
    return list(results.values()), alerts


def episodes(qualifying: list[Reading], close_after: timedelta) -> list[list[Reading]]:
    """Split one patient's qualifying readings wherever `close_after` passes without one."""
    out: list[list[Reading]] = []
    for r in qualifying:
        if out and r.measured_at - out[-1][-1].measured_at <= close_after:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def evaluate_persistent(rule: dict[str, Any], readings: list[Reading], rules_by_id: dict[str, dict],
                        windows: list[Window]) -> tuple[list[Result], list[Alert]]:
    """At most one open alert per patient, raised once an episode has `min_readings` readings with the
    first and latest at least `min_hours_apart` apart. Every reading in the episode, before and
    after that point, is evidence. The alert closes `close_after_hours` after the episode's last
    reading, if that falls before the end of monitoring (BP_ELEVATED, D26)."""
    results, qualifying = classify(rule, readings, rules_by_id)
    close_after = timedelta(hours=rule["close_after_hours"])
    apart = timedelta(hours=rule["min_hours_apart"])
    as_of = {w.patient_id: w for w in windows}
    by_patient: dict[str, list[Reading]] = {}
    for r in qualifying:
        by_patient.setdefault(r.patient_id, []).append(r)

    alerts: list[Alert] = []
    for pid, rs in by_patient.items():
        w = as_of[pid]
        for ep in episodes(rs, close_after):
            closes = ep[-1].measured_at + close_after
            closed = closes <= w.monitoring_end
            confirm = next((i for i, r in enumerate(ep)
                            if i + 1 >= rule["min_readings"] and r.measured_at - ep[0].measured_at >= apart), None)
            if confirm is None:
                why = (f"elevated, but no {rule['min_readings']}+ elevated readings {rule['min_hours_apart']:g}+ h "
                       f"apart before {'the episode closed' if closed else 'monitoring ended'}")
                for r in ep:
                    results[r.reading_id].outcome, results[r.reading_id].detail = "unconfirmed", why
                continue
            trig, top = ep[confirm], max(ep, key=lambda x: (x.systolic, x.diastolic))
            a = Alert(alert_id(rule["id"], pid, ep[0].reading_id), rule, pid, trig.measured_at, ep[-1].measured_at,
                      trig.postpartum_day, trig.hours_since_discharge / 24, "",
                      [("trigger", "home_reading", r.reading_id) for r in ep],
                      status="closed" if closed else "open", closed_at=closes if closed else None)
            days = (f"postpartum day {ep[0].postpartum_day}" if ep[0].postpartum_day == ep[-1].postpartum_day
                    else f"postpartum days {ep[0].postpartum_day}-{ep[-1].postpartum_day}")
            a.summary = (f"{rule['label']}: {len(ep)} readings on {days}, highest systolic "
                         f"{fmt_bp(top.systolic, top.diastolic)}; confirmed on day {trig.postpartum_day} "
                         f"(threshold: {threshold_text(rule)}, {rule['min_readings']}+ readings "
                         f"{rule['min_hours_apart']:g}+ h apart)"
                         + (f"; closed after {rule['close_after_hours']:g} h without an elevated reading"
                            if closed else ""))
            for r in ep:
                results[r.reading_id].detail = f"part of {a.alert_id} ({len(ep)} readings)"
            alerts.append(a)
    return list(results.values()), alerts


def silence_start(w: Window) -> datetime:
    """Local midnight at the start of postpartum day 1 (D25)."""
    tz = ZoneInfo(w.timezone)
    day1 = w.discharge_at.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(day1, time(0, 0), tz).astimezone(UTC)


def evaluate_silence(rule: dict[str, Any], readings: list[Reading], windows: list[Window]
                     ) -> tuple[list[Result], list[Alert]]:
    limit = timedelta(hours=rule["hours"])
    by_patient: dict[str, list[Reading]] = {}
    for r in readings:
        by_patient.setdefault(r.patient_id, []).append(r)

    results, alerts = [], []
    for w in windows:
        clock, last = silence_start(w), None
        gaps: list[tuple[datetime, datetime, Reading | None, bool]] = []  # (from, to, last reading, resumed)
        for r in by_patient.get(w.patient_id, []):
            if r.measured_at - clock >= limit:
                gaps.append((clock, r.measured_at, last, True))
            if r.measured_at > clock:
                clock = r.measured_at
            last = r
        # Silence up to the end of monitoring, which is already capped at death (D15).
        if w.monitoring_end - clock >= limit:
            gaps.append((clock, w.monitoring_end, last, False))

        longest = max((to - frm for frm, to, _, _ in gaps), default=timedelta(0))
        if not gaps:
            results.append(Result(rule, "patient", w.patient_id, w.patient_id, "passed",
                                  f"no silence of {rule['hours']}+ hours"))
            continue
        results.append(Result(rule, "patient", w.patient_id, w.patient_id, "fired",
                              f"{len(gaps)} silence(s), longest {longest.total_seconds() / 3600:.1f} hours",
                              "deceased during monitoring; silence counted only until death"
                              if w.deceased_at and w.deceased_at <= w.monitoring_end else ""))
        tz = ZoneInfo(w.timezone)
        for frm, to, last_r, resumed in gaps:
            triggered = frm + limit
            day = (triggered.astimezone(tz).date() - w.discharge_at.astimezone(tz).date()).days
            since = (f"last reading {last_r.measured_at.astimezone(tz):%Y-%m-%d %H:%M} local, postpartum day "
                     f"{last_r.postpartum_day}") if last_r else "no readings since discharge"
            until = "readings resumed after" if resumed else "no further readings before monitoring ended after"
            summary = (f"{rule['label']}: {since}; {until} {(to - frm).total_seconds() / 3600:.0f} hours")
            a = Alert(alert_id(rule["id"], w.patient_id, frm.isoformat()), rule, w.patient_id, triggered, triggered, day,
                      (triggered - w.discharge_at).total_seconds() / 86400, summary,
                      [("silence", "patient", w.patient_id)])
            if last_r:
                a.evidence.append(("last_reading_before_silence", "home_reading", last_r.reading_id))
            alerts.append(a)
    return results, alerts


# --- Run -------------------------------------------------------------------


@dataclass
class RunStats:
    run_id: int
    readings: int
    patients: int
    results: list[Result]
    alerts: list[Alert]


def evaluate(db_path: Path = DB_PATH, rules_path: Path = RULES_PATH) -> RunStats:
    cfg = load_rules(rules_path)
    rules_by_id = {r["id"]: r for r in cfg["rule"]}
    rank = {sev: i + 1 for i, sev in enumerate(cfg["severity_order"])}
    with connect(db_path) as con:
        clean_id, readings, windows = read_inputs(con)
        results: list[Result] = []
        alerts: list[Alert] = []
        for rule in cfg["rule"]:
            if rule["kind"] == "reading_threshold":
                res, al = evaluate_thresholds(rule, readings, rules_by_id)
            elif rule["kind"] == "persistent_threshold":
                res, al = evaluate_persistent(rule, readings, rules_by_id, windows)
            else:
                res, al = evaluate_silence(rule, readings, windows)
            results += res
            alerts += al

        con.begin()
        try:
            run_id = con.execute("SELECT nextval('alerts.run_id_seq')").fetchone()[0]
            for table in ("alerts.rule", "alerts.validation_result", "alerts.alert", "alerts.alert_evidence"):
                con.execute(f"DELETE FROM {table}")
            con.executemany("INSERT INTO alerts.rule VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (r["id"], r["version"], run_id, r["name"], r["label"], r["severity"], r["kind"],
                 json.dumps(rule_params(r)), r["description"], r["source"]) for r in cfg["rule"]])
            if results:
                con.executemany("INSERT INTO alerts.validation_result VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                    (run_id, x.rule["id"], x.rule["version"], x.record_type, x.record_id, x.patient_id,
                     x.outcome, x.observed, x.detail or None) for x in results])
            if alerts:
                con.executemany("INSERT INTO alerts.alert VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                    (a.alert_id, run_id, a.rule["id"], a.rule["version"], a.rule["label"], a.rule["severity"],
                     rank[a.rule["severity"]], a.patient_id, a.triggered_at, a.updated_at, a.postpartum_day,
                     a.days_since_discharge, threshold_text(a.rule), a.summary, a.status, a.closed_at)
                    for a in alerts])
                con.executemany("INSERT INTO alerts.alert_evidence VALUES (?, ?, ?, ?)",
                                [(a.alert_id, *e) for a in alerts for e in a.evidence])
            con.execute("INSERT INTO alerts.rule_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                run_id, datetime.now(UTC), cfg["version"], cfg["sha256"], clean_id, len(readings),
                len(windows), len(results), len(alerts)])
            con.commit()
        except Exception:
            con.rollback()
            raise
    return RunStats(run_id, len(readings), len(windows), results, alerts)


def print_report(stats: RunStats, db_path: Path = DB_PATH, limit: int = 15) -> None:
    print(f"== Rule run {stats.run_id}: {stats.readings} readings, {stats.patients} patients, "
          f"{len(stats.results)} rule results, {len(stats.alerts)} alerts")
    with duckdb.connect(str(db_path), read_only=True) as con:
        con.execute("SET TimeZone = 'UTC'")
        print(f"   {'rule':<12} {'ver':>3} {'severity':<9} {'affected':>8} {'of':>5} {'pct':>7}  alerts  patients")
        for row in con.execute("""
                SELECT s.rule_id, s.rule_version, s.severity, s.affected_count, s.evaluated_count, s.affected_pct,
                       count(DISTINCT a.alert_id), count(DISTINCT a.patient_id)
                FROM alerts.rule_summary s LEFT JOIN alerts.alert a USING (rule_id)
                GROUP BY ALL ORDER BY min(a.severity_rank), s.rule_id""").fetchall():
            print(f"   {row[0]:<12} {row[1]:>3} {row[2]:<9} {row[3]:>8} {row[4]:>5} {row[5]:>6}%  {row[6]:>6}  {row[7]:>8}")
        n_open = sum(a.status == "open" for a in stats.alerts)
        print(f"== Open alerts, most urgent first (showing {min(limit, n_open)} of {n_open} open; "
              f"{len(stats.alerts) - n_open} closed)")
        for a in con.execute("""
                SELECT alert_id, severity, label, patient_id, postpartum_day, triggered_at::TIMESTAMP, summary
                FROM alerts.alert WHERE status = 'open'
                ORDER BY severity_rank, triggered_at, alert_id LIMIT ?""", [limit]).fetchall():
            print(f"   {a[0]}  {a[1]:<8} day {a[4]:>2}  patient {a[3][:8]}  {a[6]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--rules", type=Path, default=RULES_PATH)
    args = parser.parse_args()
    print_report(evaluate(args.db, args.rules), args.db)


if __name__ == "__main__":
    main()
