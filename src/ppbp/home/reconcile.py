"""Phase 4: reconcile the simulator's output with what the pipeline loaded and cleaned.

    python -m ppbp.home.reconcile

Reads the simulator's answer key (injected_issues.json). The pipeline itself never reads it.

1. Counts: every number from scheduled readings to accepted and rejected, where each step
   must add up exactly. Any mismatch fails the run.
2. Issues: for each injected issue, the line was either handled as expected, handled for
   another reason (e.g. a resend of an impossible value is rejected as implausible, not as a
   duplicate), or missed. Outcomes with no injected cause are listed as unexpected.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ppbp import DB_PATH
from ppbp.fhir.ingest import display_path
from ppbp.home.load import ENROLLMENT_FILE, READINGS_FILE
from ppbp.home.simulate import OUT_DIR

# Injected issue -> (kind of expected outcome, value): a rejection reason or an accepted flag.
EXPECTED = {
    "impossible_value": ("rejected", "implausible_value"),
    "unit_or_field_error:kpa": ("flag", "unit_converted_kpa"),
    "unit_or_field_error:unknown_unit": ("rejected", "unknown_unit"),
    "unit_or_field_error:missing_field": ("rejected", "missing_field"),
    "unit_or_field_error:non_numeric": ("rejected", "non_numeric"),
    "wrong_timezone": ("flag", "tz_corrected"),
    "late": ("flag", "late"),
    "duplicate_exact": ("rejected", "duplicate_exact"),
    "duplicate_near": ("rejected", "duplicate_near"),
}


def expected_for(issue: str) -> tuple[str, str]:
    return EXPECTED.get(issue) or EXPECTED[issue.split(":")[0]]


def reconcile(db_path: Path = DB_PATH, in_dir: Path = OUT_DIR) -> dict[str, Counter]:
    key = json.loads((in_dir / "injected_issues.json").read_text())
    source = display_path(in_dir / READINGS_FILE)
    with duckdb.connect(str(db_path), read_only=True) as con:
        flags = {n: set(f) for n, f in con.execute(
            "SELECT line_no, flags FROM home.reading WHERE source_file = ?", [source]).fetchall()}
        reasons = dict(con.execute(
            "SELECT line_no, reason FROM home.reading_rejection WHERE source_file = ?", [source]).fetchall())

    out: dict[str, Counter] = {}
    explained: set[tuple[int, str]] = set()
    for line, issues in key["issues_by_line"].items():
        n = int(line)
        for issue in issues:
            kind, value = expected_for(issue)
            label = issue.split(":")[0]
            got_rejected = reasons.get(n)
            if (kind == "rejected" and got_rejected == value) or (kind == "flag" and value in flags.get(n, set())):
                result = "as expected"
                explained.add((n, value))
            elif got_rejected is not None:
                result = f"rejected as {got_rejected}"
                explained.add((n, got_rejected))
            else:
                result = "missed"
            out.setdefault(label, Counter())[result] += 1

    unexpected = Counter()
    for n, reason in reasons.items():
        if (n, reason) not in explained:
            unexpected[f"rejected as {reason}"] += 1
    for n, fs in flags.items():
        for f in fs:
            if (n, f) not in explained:
                unexpected[f"flagged {f}"] += 1
    if unexpected:
        out["(no injected cause)"] = unexpected
    return out


@dataclass(frozen=True)
class Step:
    label: str
    value: int
    equation: str = ""   # how `value` is made up, e.g. "= 850 + 110"
    ok: bool = True


def count_reconciliation(db_path: Path = DB_PATH, in_dir: Path = OUT_DIR) -> list[Step]:
    """Scheduled readings -> lines written -> raw lines loaded -> accepted + rejected."""
    c = json.loads((in_dir / "injected_issues.json").read_text())["counts"]
    readings, enrollment = display_path(in_dir / READINGS_FILE), display_path(in_dir / ENROLLMENT_FILE)
    with duckdb.connect(str(db_path), read_only=True) as con:
        raw = dict(con.execute("SELECT source_file, count(*) FROM raw.home_line_current "
                               "WHERE source_file IN (?, ?) GROUP BY 1", [readings, enrollment]).fetchall())
        accepted = con.execute("SELECT count(*) FROM home.reading WHERE source_file = ?", [readings]).fetchone()[0]
        by_reason = dict(con.execute("SELECT reason, count(*) FROM home.reading_rejection WHERE source_file = ? "
                                     "GROUP BY 1 ORDER BY 1", [readings]).fetchall())
        enrolled = con.execute("SELECT count(*) FROM home.enrollment").fetchone()[0]
        timeline_home = con.execute("SELECT count(*) FROM timeline.bp_reading WHERE source = 'home'").fetchone()[0]

    expected = c["missed"] + c["submitted"]
    rejected = sum(by_reason.values())
    dupes = by_reason.get("duplicate_exact", 0) + by_reason.get("duplicate_near", 0)
    raw_r, raw_e = raw.get(readings, 0), raw.get(enrollment, 0)

    def step(label: str, value: int, parts: list[int], names: str) -> Step:
        return Step(label, value, f"= {' + '.join(str(p) for p in parts)}  ({names})", value == sum(parts))

    return [
        step("scheduled readings", c["scheduled"], [expected, c["after_dropout"], c["after_death"]],
             "still expected + after dropout + after death"),
        step("still expected", expected, [c["submitted"], c["missed"]], "submitted + missed"),
        step("reading lines written", c["reading_lines"], [c["submitted"], c["duplicate_lines"]],
             "submitted + duplicate lines"),
        step("raw lines loaded", raw_r + raw_e, [c["reading_lines"], c["enrollment_lines"]],
             "reading lines + enrollment lines, one per patient"),
        Step("  of which reading lines", raw_r, f"= {c['reading_lines']} written", raw_r == c["reading_lines"]),
        Step("  of which enrollment lines", raw_e, f"= {enrolled} enrolled = {c['patients']} patients",
             raw_e == enrolled == c["patients"]),
        step("reading lines cleaned", raw_r, [accepted, rejected], "accepted + rejected"),
        step("rejected", rejected, [dupes, rejected - dupes], "duplicates + other reasons"),
        *[Step(f"  {reason}", n) for reason, n in by_reason.items()],
        Step("accepted", accepted, f"= {timeline_home} home points on the timeline", accepted == timeline_home),
    ]


def print_counts(steps: list[Step]) -> None:
    print("== Count reconciliation: scheduled readings to accepted and rejected")
    for s in steps:
        mark = "" if s.ok else "   <-- DOES NOT ADD UP"
        print(f"   {s.label:<28} {s.value:>6}  {s.equation}{mark}")
    print(f"   {'all steps add up' if all(s.ok for s in steps) else 'MISMATCH: see above'}")


def print_report(results: dict[str, Counter]) -> None:
    print("== Injected issues vs cleaning outcome (line by line)")
    for label, outcomes in results.items():
        total = sum(outcomes.values())
        detail = ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
        print(f"   {label:<22} {total:>4}   {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--in-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    steps = count_reconciliation(args.db, args.in_dir)
    print_counts(steps)
    print_report(reconcile(args.db, args.in_dir))
    if not all(s.ok for s in steps):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
