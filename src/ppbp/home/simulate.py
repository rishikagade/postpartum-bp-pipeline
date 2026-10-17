"""Phase 4: simulate home blood pressure submissions for the postpartum cohort.

    python -m ppbp.home.simulate

Writes, under data/generated/home/:
    enrollment.jsonl       one line per cohort patient: device, time zone, consent, monitoring days
    readings.jsonl         what the readings app exports: one submission per line, messy on purpose
    injected_issues.json   the answer key: which lines carry which injected issue. The pipeline
                           never reads it. Only the report and tests compare it to cleaning results.

Everything is seeded (config/home.toml). Each patient and purpose gets its own random stream,
so the same config and cohort give byte-identical files. No reading is generated at or after a
patient's deceased_at (D15).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import tomllib
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ppbp import DATA_DIR, DB_PATH, PROJECT_ROOT
from ppbp.db import connect

CONFIG_PATH = PROJECT_ROOT / "config" / "home.toml"
OUT_DIR = DATA_DIR / "generated" / "home"
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:ppbp:home-readings")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    data = path.read_bytes()
    cfg = tomllib.loads(data.decode())
    cfg["sha256"] = hashlib.sha256(data).hexdigest()
    mon = cfg["monitoring"]
    if not 1 <= mon["days"] <= mon["max_days"]:
        raise ValueError(f"monitoring.days must be between 1 and {mon['max_days']}, got {mon['days']}")
    return cfg


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


@dataclass(frozen=True)
class CohortPatient:
    patient_id: str               # medical record number: the vendor knows its patients (D30)
    discharge_at: datetime        # naive UTC
    deceased_at: datetime | None  # naive UTC


def read_cohort(db_path: Path) -> list[CohortPatient]:
    """The cohort as the home-monitoring vendor sees it: by record number.

    The simulator stands in for an outside vendor that enrols real patients, so it reads the
    restricted phi.patient_key mapping. Its exports are restricted raw input (D30), and the
    pipeline swaps the record number for a study ID when it cleans them."""
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT k.patient_id, c.discharge_at::TIMESTAMP, c.deceased_at::TIMESTAMP "
            "FROM cohort.postpartum_cohort c JOIN phi.patient_key k ON k.study_id = c.patient_id "
            "ORDER BY k.patient_id"
        ).fetchall()
    return [CohortPatient(*r) for r in rows]


# --- Patient-level assignments ---------------------------------------------


@dataclass
class PatientPlan:
    patient: CohortPatient
    consent: bool
    profile: str                 # 'normal' | 'elevated' | 'severe'
    stop_day: int | None         # first postpartum day with no submissions (dropout), or None


def pick(patients: list[str], fraction: float, rng: random.Random) -> set[str]:
    return set(rng.sample(patients, round_half_up(fraction * len(patients))))


def plan_patients(cohort: list[CohortPatient], cfg: dict[str, Any]) -> list[PatientPlan]:
    seed, days = cfg["seed"], cfg["monitoring"]["days"]
    ids = [p.patient_id for p in cohort]  # sorted by read_cohort

    no_consent = pick(ids, cfg["consent"]["fraction_without"], random.Random(f"{seed}:consent"))
    bp = cfg["blood_pressure"]
    profile_rng = random.Random(f"{seed}:profile")
    severe = pick(ids, bp["severe_patient_fraction"], profile_rng)
    rest = [i for i in ids if i not in severe]
    elevated = set(profile_rng.sample(rest, min(len(rest), round_half_up(bp["elevated_patient_fraction"] * len(ids)))))
    dropouts = pick(ids, cfg["messiness"]["dropout_fraction"], random.Random(f"{seed}:dropout"))

    plans = []
    for p in cohort:
        stop_day = None
        if p.patient_id in dropouts and days >= 2:
            # Stops partway: submits on day 1 at least, and not on the last day.
            stop_day = random.Random(f"{seed}:{p.patient_id}:stop").randint(2, days)
        profile = "severe" if p.patient_id in severe else "elevated" if p.patient_id in elevated else "normal"
        plans.append(PatientPlan(p, p.patient_id not in no_consent, profile, stop_day))
    return plans


# --- Readings --------------------------------------------------------------


@dataclass
class Submission:
    payload: dict[str, Any]
    received_at: datetime              # aware UTC; also the export order
    issues: list[str] = field(default_factory=list)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def scheduled_times(plan: PatientPlan, cfg: dict[str, Any], tz: ZoneInfo) -> list[tuple[int, int, datetime]]:
    """(postpartum day, slot index, aware local time) for every reading the patient is asked for."""
    mon = cfg["monitoring"]
    rng = random.Random(f"{cfg['seed']}:{plan.patient.patient_id}:schedule")
    discharge_local: date = plan.patient.discharge_at.replace(tzinfo=UTC).astimezone(tz).date()
    out = []
    for day in range(1, mon["days"] + 1):
        for slot_index, slot in enumerate(mon["slots"]):
            jitter = rng.randint(-mon["slot_jitter_minutes"], mon["slot_jitter_minutes"])
            local = datetime.combine(discharge_local + timedelta(days=day), time.fromisoformat(slot), tz)
            out.append((day, slot_index, local + timedelta(minutes=jitter)))
    return out


def bp_values(plan: PatientPlan, cfg: dict[str, Any], n: int) -> list[tuple[int, int]]:
    bp = cfg["blood_pressure"]
    rng = random.Random(f"{cfg['seed']}:{plan.patient.patient_id}:bp")
    base_sys, base_dia = bp[f"{plan.profile}_baseline"]
    sd_sys, sd_dia = bp["noise_sd"]
    # Outside the severe spikes, values stay below the severe range.
    values = [(min(159, round(rng.gauss(base_sys, sd_sys))), min(109, round(rng.gauss(base_dia, sd_dia))))
              for _ in range(n)]
    if plan.profile == "severe" and n:
        for i in rng.sample(range(n), min(n, rng.randint(*bp["severe_spikes"]))):
            values[i] = (rng.randint(*bp["severe_systolic"]), rng.randint(*bp["severe_diastolic"]))
    return values


def corrupt_values(payload: dict[str, Any], rng: random.Random) -> str:
    kind = rng.choice(["zero_systolic", "huge_systolic", "diastolic_above_systolic", "tiny_systolic"])
    if kind == "zero_systolic":
        payload["systolic"] = 0
    elif kind == "huge_systolic":
        payload["systolic"] = 999
    elif kind == "diastolic_above_systolic":
        payload["diastolic"] = payload["systolic"] + 20
    else:
        payload["systolic"] = 25
    return f"impossible_value:{kind}"


def corrupt_fields(payload: dict[str, Any], cfg: dict[str, Any], rng: random.Random) -> str:
    kind = rng.choice(["kpa", "unknown_unit", "missing_field", "non_numeric"])
    if kind == "kpa":
        factor = cfg["cleaning"]["kpa_to_mmhg"]
        payload["systolic"] = round(payload["systolic"] / factor, 1)
        payload["diastolic"] = round(payload["diastolic"] / factor, 1)
        payload["unit"] = "kPa"
    elif kind == "unknown_unit":
        payload["systolic"] = round(payload["systolic"] / 10, 1)
        payload["diastolic"] = round(payload["diastolic"] / 10, 1)
        payload["unit"] = "cmHg"
    elif kind == "missing_field":
        del payload["diastolic"]
    else:
        payload["systolic"] = str(payload["systolic"]).replace("0", "O").replace("1", "l") + "?"
    return f"unit_or_field_error:{kind}"


def patient_submissions(plan: PatientPlan, cfg: dict[str, Any]) -> tuple[list[Submission], Counter]:
    """The patient's submission lines, plus what happened to each scheduled slot:
    exactly one of after_death, after_dropout, missed, submitted."""
    pid, seed, mess = plan.patient.patient_id, cfg["seed"], cfg["messiness"]
    tz = ZoneInfo(cfg["monitoring"]["timezone"])
    deceased = plan.patient.deceased_at.replace(tzinfo=UTC) if plan.patient.deceased_at else None
    slots = scheduled_times(plan, cfg, tz)
    values = bp_values(plan, cfg, len(slots))
    rng = random.Random(f"{seed}:{pid}:mess")
    device_id = str(uuid.uuid5(ID_NAMESPACE, f"device:{pid}"))

    out: list[Submission] = []
    slot_outcomes: Counter = Counter()
    for (day, slot_index, local), (sys_, dia) in zip(slots, values, strict=True):
        if deceased is not None and local >= deceased:
            slot_outcomes["after_death"] += 1       # nothing at or after death (D15)
            continue
        if plan.stop_day is not None and day >= plan.stop_day:
            slot_outcomes["after_dropout"] += 1
            continue
        if rng.random() < mess["missed_fraction"]:
            slot_outcomes["missed"] += 1
            continue
        slot_outcomes["submitted"] += 1

        issues: list[str] = []
        payload: dict[str, Any] = {
            "submission_id": str(uuid.uuid5(ID_NAMESPACE, f"{pid}:{day}:{slot_index}")),
            "patient_ref": pid,
            "device_id": device_id,
            "measured_at": local.isoformat(),
            "received_at": None,
            "systolic": sys_,
            "diastolic": dia,
            "unit": "mmHg",
            "channel": "sms",
        }
        # At most one value or field problem per reading, so each has one cause.
        roll = rng.random()
        if roll < mess["impossible_value_fraction"]:
            issues.append(corrupt_values(payload, rng))
        elif roll < mess["impossible_value_fraction"] + mess["unit_or_field_error_fraction"]:
            issues.append(corrupt_fields(payload, cfg, rng))
        if rng.random() < mess["wrong_timezone_fraction"]:
            # A common device bug: local wall-clock time labelled as UTC.
            payload["measured_at"] = local.replace(tzinfo=None).isoformat() + "Z"
            issues.append("wrong_timezone")

        if rng.random() < mess["late_fraction"]:
            received = local + timedelta(days=rng.randint(1, 4), minutes=rng.randint(0, 600))
            issues.append("late")
        else:
            received = local + timedelta(minutes=rng.randint(1, 30))
        payload["received_at"] = iso_utc(received)
        out.append(Submission(payload, received, issues))

        if rng.random() < mess["duplicate_fraction"]:
            resend = received + timedelta(minutes=rng.randint(1, 60))
            if rng.random() < 0.5:
                # Exact resend: the app retries the same payload.
                out.append(Submission(dict(payload), resend, ["duplicate_exact"]))
            else:
                near = dict(payload, submission_id=str(uuid.uuid5(ID_NAMESPACE, f"{pid}:{day}:{slot_index}:again")),
                            received_at=iso_utc(resend))
                if isinstance(near.get("systolic"), int) and isinstance(near.get("diastolic"), int):
                    near["systolic"] += rng.randint(-4, 4)
                    near["diastolic"] += rng.randint(-3, 3)
                if not near["measured_at"].endswith("Z"):
                    remeasured = datetime.fromisoformat(near["measured_at"]) + timedelta(minutes=rng.randint(1, 3))
                    near["measured_at"] = remeasured.isoformat()
                out.append(Submission(near, resend, ["duplicate_near"]))
    return out, slot_outcomes


# --- Output ----------------------------------------------------------------


@dataclass
class SimulationResult:
    plans: list[PatientPlan]
    submissions: list[Submission]
    slots: Counter               # after_death / after_dropout / missed / submitted

    @property
    def scheduled(self) -> int:
        return sum(self.slots.values())

    @property
    def before_dropout(self) -> int:
        """Slots the patient was still expected to submit: not after death or dropout."""
        return self.slots["missed"] + self.slots["submitted"]


def simulate(cohort: list[CohortPatient], cfg: dict[str, Any]) -> SimulationResult:
    plans = plan_patients(cohort, cfg)
    subs: list[Submission] = []
    slots: Counter = Counter()
    for plan in plans:
        patient_subs, outcomes = patient_submissions(plan, cfg)
        subs.extend(patient_subs)
        slots.update(outcomes)
    subs.sort(key=lambda s: (s.received_at, s.payload["submission_id"], len(s.issues)))
    return SimulationResult(plans, subs, slots)


def count_chain(result: SimulationResult) -> dict[str, int]:
    """Every count from scheduled slots to lines written. Each step adds up (checked in reconcile)."""
    duplicates = sum(1 for s in result.submissions if any(i.startswith("duplicate") for i in s.issues))
    return {
        "patients": len(result.plans),
        "scheduled": result.scheduled,
        "after_death": result.slots["after_death"],
        "after_dropout": result.slots["after_dropout"],
        "missed": result.slots["missed"],
        "submitted": result.slots["submitted"],
        "duplicate_lines": duplicates,
        "reading_lines": len(result.submissions),
        "enrollment_lines": len(result.plans),
    }


def write_outputs(result: SimulationResult, cfg: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "enrollment.jsonl").open("w") as fh:
        for plan in result.plans:
            fh.write(json.dumps({
                "patient_ref": plan.patient.patient_id,
                "device_id": str(uuid.uuid5(ID_NAMESPACE, f"device:{plan.patient.patient_id}")),
                "timezone": cfg["monitoring"]["timezone"],
                "consent_home_monitoring": plan.consent,
                "monitoring_days": cfg["monitoring"]["days"],
            }) + "\n")
    with (out_dir / "readings.jsonl").open("w") as fh:
        for s in result.submissions:
            fh.write(json.dumps(s.payload) + "\n")
    answer_key = {
        "note": "Answer key for tests and reports only. The pipeline never reads this file.",
        "config_sha256": cfg["sha256"],
        "patients": {p.patient.patient_id: {"profile": p.profile, "consent": p.consent, "stop_day": p.stop_day}
                     for p in result.plans},
        "counts": count_chain(result),
        # 1-based line numbers in readings.jsonl
        "issues_by_line": {str(i): s.issues for i, s in enumerate(result.submissions, start=1) if s.issues},
    }
    (out_dir / "injected_issues.json").write_text(json.dumps(answer_key, indent=2) + "\n")


def report(result: SimulationResult, cfg: dict[str, Any]) -> list[tuple[str, str, int, int]]:
    """(issue, configured rate, injected count, denominator) for every configured issue."""
    mess = cfg["messiness"]
    originals = [s for s in result.submissions if not any(i.startswith("duplicate") for i in s.issues)]
    kinds = Counter(i.split(":")[0] for s in result.submissions for i in s.issues)
    n_patients = len(result.plans)
    return [
        ("patients without consent", f"{cfg['consent']['fraction_without']:.0%}",
         sum(not p.consent for p in result.plans), n_patients),
        ("patients who stop early", f"{mess['dropout_fraction']:.0%}",
         sum(p.stop_day is not None for p in result.plans), n_patients),
        ("missed readings", f"{mess['missed_fraction']:.0%}", result.slots["missed"], result.before_dropout),
        ("duplicate submissions", f"{mess['duplicate_fraction']:.0%}",
         kinds["duplicate_exact"] + kinds["duplicate_near"], len(originals)),
        ("late submissions", f"{mess['late_fraction']:.0%}", kinds["late"], len(originals)),
        ("wrong time zone", f"{mess['wrong_timezone_fraction']:.0%}", kinds["wrong_timezone"], len(originals)),
        ("impossible values", f"{mess['impossible_value_fraction']:.0%}", kinds["impossible_value"], len(originals)),
        ("unit or field errors", f"{mess['unit_or_field_error_fraction']:.0%}", kinds["unit_or_field_error"],
         len(originals)),
    ]


def print_report(result: SimulationResult, cfg: dict[str, Any]) -> None:
    profiles = Counter(p.profile for p in result.plans)
    print(f"== Home reading simulation ({len(result.plans)} patients, {cfg['monitoring']['days']} days, "
          f"{len(cfg['monitoring']['slots'])} readings/day)")
    print(f"   scheduled readings:        {result.scheduled:,}")
    print(f"   before dropout or death:   {result.before_dropout:,}")
    print(f"   lines written:             {len(result.submissions):,}")
    print(f"   BP profiles:               {dict(sorted(profiles.items()))}")
    print(f"   {'issue':<26} {'configured':>10} {'produced':>9} {'of':>6} {'actual':>7}")
    for issue, configured, n, denom in report(result, cfg):
        print(f"   {issue:<26} {configured:>10} {n:>9} {denom:>6} {n / denom if denom else 0:>7.1%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    cfg = load_config(args.config)
    result = simulate(read_cohort(args.db), cfg)
    write_outputs(result, cfg, args.out)
    print_report(result, cfg)


if __name__ == "__main__":
    main()
