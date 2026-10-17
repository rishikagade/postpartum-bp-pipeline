"""Phase 5b: check that nothing identifying has leaked out of the restricted zones (D33).

    python -m ppbp.privacy.leak_check             # exits 1 if anything is found
    python -m ppbp.privacy.leak_check --history   # every file and message in every commit

Builds a catalogue of identifying values from the restricted zones (FHIR Patients and
Practitioners in raw, contact details in phi). Then it scans everything outside those zones:
    - every table in ops, cohort, home, timeline, alerts and audit
    - every access-layer response, for every alert and every enrolled patient (built without logging)
    - logs and reports under data/out, including the text of PDFs, and screenshots (embedded text,
      plus their visible text when an OCR tool is set in PPBP_OCR). PDFs and images are for people,
      so they also may not hold study IDs, alert IDs or record references
    - every file git tracks or would commit (not ignored)
The report gives where each finding is and what kind it is, never the value itself.

Restricted by design, and not scanned: the raw and phi schemas, data/raw (Synthea bundles),
data/generated (raw inputs: the vendor's exports and the augmentation bundle, keyed by record
number), and data/secrets.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import duckdb

from ppbp import DATA_DIR, DB_PATH, PROJECT_ROOT
from ppbp.alerts.explain import explain_alert
from ppbp.privacy.access import (
    render_access_log, render_alert_list, render_holds, render_load_summary, render_nurse_alert, render_patient_list,
    render_timeline)

SCANNED_SCHEMAS = ("ops", "cohort", "home", "timeline", "alerts", "audit")
RESTRICTED = ["schema raw", "schema phi", "data/raw/", "data/generated/", "data/secrets/"]
REPORT_PATH = DATA_DIR / "out" / "privacy" / "leak_check.json"

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# An 8-hex record-number prefix on its own, or truncated in an example as "a1b2c3d4-...".
HEX8 = re.compile(r"(?<![0-9a-fA-F-])[0-9a-f]{8}(?=-\.\.\.|(?![0-9a-fA-F-]))")
WORD_WITH_DIGITS = re.compile(r"[A-Za-z][A-Za-z'\-]*\d+")     # Synthea names carry digits, e.g. "Given12"
CODE_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9\-]{5,}[A-Z0-9]")


@dataclass
class Catalogue:
    record_numbers: set[str] = field(default_factory=set)
    prefixes: set[str] = field(default_factory=set)
    name_tokens: set[str] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)          # phone, SSN, licence, passport
    phrases: dict[str, str] = field(default_factory=dict)  # multi-word value -> kind (addresses, names)
    birth_dates: dict[str, str] = field(default_factory=dict)  # study_id -> birth date


def build_catalogue(con: duckdb.DuckDBPyConnection) -> Catalogue:
    cat = Catalogue()
    rows = con.execute("""
        SELECT v.resource_type, v.resource_id, v.resource_json
        FROM raw.fhir_resource_current c JOIN raw.fhir_resource_version v USING (resource_type, resource_id, content_hash)
        WHERE c.resource_type IN ('Patient', 'Practitioner')""").fetchall()
    for rtype, rid, j in rows:
        r = json.loads(j)
        for n in r.get("name", []):
            for tok in [*n.get("given", []), n.get("family", "")]:
                cat.name_tokens.update(WORD_WITH_DIGITS.findall(tok))
            full = " ".join([*n.get("given", []), n.get("family", "")]).strip()
            if full:
                cat.phrases[full] = "name"
        if rtype != "Patient":
            continue
        cat.record_numbers.add(rid)
        cat.prefixes.add(rid[:8])
        for t in r.get("telecom", []):
            if t.get("value"):
                cat.codes.add(t["value"])
        for i in r.get("identifier", []):
            if i.get("value") and i["value"] != rid:
                cat.codes.add(i["value"])
        for a in r.get("address", []):
            for line in a.get("line", []):
                cat.phrases[line] = "address"
        for e in r.get("extension", []):
            if e["url"].endswith("patient-mothersMaidenName") and e.get("valueString"):
                cat.name_tokens.update(WORD_WITH_DIGITS.findall(e["valueString"]))
    for sid, bd in con.execute("""
            SELECT k.study_id, v.resource_json->>'$.birthDate'
            FROM phi.patient_key k
            JOIN raw.fhir_resource_current c ON c.resource_type = 'Patient' AND c.resource_id = k.patient_id
            JOIN raw.fhir_resource_version v USING (resource_type, resource_id, content_hash)""").fetchall():
        if bd:
            cat.birth_dates[sid] = bd
    for (name,) in con.execute("SELECT display_name FROM phi.patient_contact").fetchall():
        cat.phrases[name] = "name"
    cat.phrases = {p: k for p, k in cat.phrases.items() if len(p) >= 6}
    return cat


def find(text: str, cat: Catalogue, patient_id: str | None = None) -> Counter:
    """Kinds of identifying value found in `text` (never the values)."""
    hits: Counter = Counter()
    hits["record_number"] += sum(1 for m in UUID.findall(text) if m in cat.record_numbers)
    hits["record_number_prefix"] += sum(1 for m in HEX8.findall(text) if m in cat.prefixes)
    hits["name"] += sum(1 for m in WORD_WITH_DIGITS.findall(text) if m in cat.name_tokens)
    hits["identifier_or_phone"] += sum(1 for m in CODE_TOKEN.findall(text) if m in cat.codes)
    hits["identifier_or_phone"] += sum(1 for c in cat.codes if "-" in c and c in text and not CODE_TOKEN.fullmatch(c))
    for phrase, kind in cat.phrases.items():
        if phrase in text:
            hits[kind] += 1
    if patient_id and cat.birth_dates.get(patient_id, "\0") in text:
        hits["birth_date"] += 1
    return +hits


# --- Targets ---------------------------------------------------------------


@dataclass
class Finding:
    target: str
    location: str
    kinds: dict[str, int]


def scan_tables(con: duckdb.DuckDBPyConnection, cat: Catalogue) -> tuple[int, list[Finding]]:
    findings, scanned = [], 0
    tables = con.execute(f"""SELECT schema_name, table_name FROM duckdb_tables()
                             WHERE schema_name IN {SCANNED_SCHEMAS} ORDER BY 1, 2""").fetchall()
    for schema, table in tables:
        cols = [c[0] for c in con.execute(f"DESCRIBE {schema}.{table}").fetchall()]
        pcol = cols.index("patient_id") if "patient_id" in cols else None
        kinds: Counter = Counter()
        for row in con.execute(f"SELECT COLUMNS(*)::VARCHAR FROM {schema}.{table}").fetchall():
            scanned += 1
            kinds += find(" | ".join(v for v in row if v is not None), cat, row[pcol] if pcol is not None else None)
        if kinds:
            findings.append(Finding("table", f"{schema}.{table}", dict(kinds)))
    return scanned, findings


def scan_responses(db_path: Path, cat: Catalogue) -> tuple[int, list[Finding]]:
    findings, scanned = [], 0

    def check(where: str, obj: Any, patient_id: str | None = None) -> None:
        nonlocal scanned
        scanned += 1
        kinds = find(json.dumps(obj, default=str), cat, patient_id)
        if kinds:
            findings.append(Finding("access_response", where, dict(kinds)))

    alerts = render_alert_list(db_path)
    check("nurse: list_alerts", alerts)
    with duckdb.connect(str(db_path), read_only=True) as con:
        all_alerts = con.execute("SELECT alert_id, patient_id, status FROM alerts.alert ORDER BY 1").fetchall()
        patients = [r[0] for r in con.execute("SELECT patient_id FROM home.enrollment ORDER BY 1").fetchall()]
    for alert_id, pid, status in all_alerts:
        if status != "held_back":
            check(f"nurse: read_alert {alert_id}", render_nurse_alert(alert_id, db_path), pid)
        check(f"engineer: explain_alert {alert_id}", explain_alert(alert_id, db_path), pid)
    for pid in patients:
        check(f"nurse: read_timeline {pid}", render_timeline(pid, db_path), pid)
    check("nurse: list_patients", render_patient_list(db_path))
    check("engineer: read_loads", render_load_summary(db_path))
    check("privacy officer: read_holds", render_holds(db_path))
    check("privacy officer: read_access_log", render_access_log(db_path, limit=100_000))
    return scanned, findings


# Reports for people (PDFs) carry counts only, so they may not hold even pseudonymous IDs (D38).
PDF_FORBIDDEN = {
    "study_id": re.compile(r"SID-[0-9a-f]{12}"),
    "alert_id": re.compile(r"ALR-[0-9a-f]{12}"),
    "record_reference": re.compile(r"\.jsonl#\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
}


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta = " ".join(str(v) for v in (reader.metadata or {}).values())
    return meta + "\n" + "\n".join(page.extract_text() or "" for page in reader.pages)


def png_text(path: Path) -> str:
    """A PNG's embedded text chunks, plus the text visible in it when an OCR tool is configured
    (PPBP_OCR: a program that prints the text it reads in the images it's given, e.g. tools/ocr.swift)."""
    import os
    import struct
    import zlib

    data, chunks, i = path.read_bytes(), [], 8
    while i + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + length]
        if kind == b"tEXt":
            chunks.append(body.replace(b"\0", b" ").decode("latin-1"))
        elif kind == b"zTXt":
            key, _, rest = body.partition(b"\0")
            chunks.append(key.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("latin-1"))
        elif kind == b"iTXt":
            chunks.append(body.replace(b"\0", b" ").decode("utf-8", "replace"))
        i += 12 + length
    if ocr := os.environ.get("PPBP_OCR"):
        chunks.append(subprocess.run([ocr, str(path)], capture_output=True, text=True, check=True).stdout)
    return "\n".join(chunks)


IMAGES = {".png"}
OCR_USED = {"images": 0, "ocr": None}


def text_files(paths: Iterable[Path]) -> Iterable[tuple[Path, str]]:
    import os

    for p in paths:
        try:
            if p.suffix.lower() in IMAGES:
                OCR_USED["images"] += 1
                OCR_USED["ocr"] = bool(os.environ.get("PPBP_OCR"))
                yield p, png_text(p)
            else:
                yield p, pdf_text(p) if p.suffix.lower() == ".pdf" else p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError, ValueError):
            continue


def scan_files(cat: Catalogue, out_dir: Path, report_path: Path, scan_repo: bool = True
               ) -> tuple[int, int, list[Finding]]:
    findings = []
    outputs = sorted(p for p in out_dir.rglob("*") if p.is_file() and p != report_path) if out_dir.exists() else []
    # Tracked files plus new files not yet committed (anything git doesn't ignore).
    tracked = [PROJECT_ROOT / f for f in subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=PROJECT_ROOT, capture_output=True,
        text=True, check=True).stdout.split()] if scan_repo else []
    n_out = n_tracked = 0
    for target, paths in (("output_file", outputs), ("tracked_file", tracked)):
        for path, text in text_files(paths):
            if target == "output_file":
                n_out += 1
            else:
                n_tracked += 1
            kinds = find(text, cat)
            if path.suffix.lower() in {".pdf", *IMAGES}:   # made for people: counts only, no IDs at all
                for kind, pattern in PDF_FORBIDDEN.items():
                    if n := len(pattern.findall(text)):
                        kinds[kind] += n
            if kinds:
                where = path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path
                findings.append(Finding(target, str(where), dict(kinds)))
    return n_out, n_tracked, findings


def run(db_path: Path = DB_PATH, out_dir: Path = DATA_DIR / "out", report_path: Path = REPORT_PATH,
        scan_repo: bool = True) -> dict[str, Any]:
    """scan_repo=False skips the repository's files: tests build their own made-up patients, and the
    test source naturally contains those values."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        cat = build_catalogue(con)
        n_rows, table_findings = scan_tables(con, cat)
    n_resp, response_findings = scan_responses(db_path, cat)
    OCR_USED.update(images=0, ocr=None)
    n_out, n_tracked, file_findings = scan_files(cat, out_dir, report_path, scan_repo)
    findings = table_findings + response_findings + file_findings
    report = {
        "catalogue": {"record_numbers": len(cat.record_numbers), "name_tokens": len(cat.name_tokens),
                      "identifiers_and_phones": len(cat.codes), "names_and_addresses": len(cat.phrases),
                      "birth_dates": len(cat.birth_dates)},
        "scanned": {"table_rows": n_rows, "access_responses": n_resp, "output_files": n_out, "tracked_files": n_tracked,
                    "images": OCR_USED["images"], "images_ocr": OCR_USED["ocr"]},
        "restricted_not_scanned": RESTRICTED,
        "findings": [f.__dict__ for f in findings],
        "passed": not findings,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def print_report(r: dict[str, Any]) -> None:
    c, s = r["catalogue"], r["scanned"]
    print(f"== Leak check: {'PASSED' if r['passed'] else 'FAILED'}")
    print(f"   looking for: {c['record_numbers']} record numbers (and prefixes), {c['name_tokens']} name tokens, "
          f"{c['identifiers_and_phones']} identifiers/phones, {c['names_and_addresses']} names/addresses, "
          f"{c['birth_dates']} birth dates")
    print(f"   scanned: {s['table_rows']:,} table rows, {s['access_responses']} access-layer responses, "
          f"{s['output_files']} output files, {s['tracked_files']} tracked files")
    if s["images"]:
        how = "visible text (OCR) and metadata" if s["images_ocr"] else "metadata only: set PPBP_OCR to read their text"
        print(f"   images: {s['images']} ({how})")
    print(f"   restricted, not scanned: {', '.join(r['restricted_not_scanned'])}")
    for f in r["findings"]:
        print(f"   FOUND in {f['target']} {f['location']}: {f['kinds']}")
    print(f"   report: {REPORT_PATH.relative_to(PROJECT_ROOT)} (findings name the place and kind, never the value)")


def scan_history(db_path: Path = DB_PATH, repo: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Every file and the message of every commit on every branch. Run before any push."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout

    with duckdb.connect(str(db_path), read_only=True) as con:
        cat = build_catalogue(con)
    commits, findings, blobs = git("rev-list", "--all", "--reverse").split(), [], 0
    for c in commits:
        short = git("log", "-1", "--format=%h", c).strip()
        kinds = find(git("log", "-1", "--format=%B", c), cat)
        if kinds:
            findings.append(Finding("commit_message", short, dict(kinds)))
        for f in git("ls-tree", "-r", "--name-only", c).split():
            try:
                text = git("show", f"{c}:{f}")
            except (subprocess.CalledProcessError, UnicodeDecodeError):
                continue
            blobs += 1
            kinds = find(text, cat)
            if kinds:
                findings.append(Finding("commit_file", f"{short}:{f}", dict(kinds)))
    return {"commits": len(commits), "files_scanned": blobs, "findings": [f.__dict__ for f in findings],
            "passed": not findings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--history", action="store_true", help="scan every commit instead of the current state")
    args = parser.parse_args()
    if args.history:
        report = scan_history(args.db)
        print(f"== History leak check: {'PASSED' if report['passed'] else 'FAILED'}")
        print(f"   scanned: {report['commits']} commits, {report['files_scanned']} files")
        for f in report["findings"]:
            print(f"   FOUND in {f['target']} {f['location']}: {f['kinds']}")
    else:
        report = run(args.db)
        print_report(report)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
