"""Phase 6: render the data quality report as a PDF (D38).

Counts only: no patient, alert or record identifiers appear in the PDF. `make report` runs the
leak check over it, and the PDF check also forbids study IDs, alert IDs and record references.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FOOTER = "Synthetic data. Not for clinical use. Data quality measure only; not a clinical or safety measure."

REASONS = {
    "duplicate_exact": "Sent twice, unchanged",
    "duplicate_near": "Repeated within minutes",
    "implausible_value": "Impossible blood pressure value",
    "unknown_unit": "Unknown unit",
    "missing_field": "Missing a required field",
    "non_numeric": "Value not a number",
    "invalid_json": "Unreadable line",
    "invalid_timestamp": "Unreadable time",
    "outside_monitoring_window": "Outside the monitoring window",
    "after_death": "Dated after the patient's death",
    "unknown_patient": "Patient not enrolled",
}
COMPONENTS = {
    "missing_value": "Missing values",
    "duplicate_rows": "Duplicates",
    "invalid_type": "Wrong type or format",
    "outliers": "Impossible values",
    "critical_failures": "Critical failures",
    "business_rule_violations": "Business rules (timeliness, gaps, integrity)",
}

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_styles["Title"], fontSize=17, alignment=TA_LEFT, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=_styles["Heading2"], fontSize=12.5, spaceBefore=12, spaceAfter=3)
BODY = ParagraphStyle("body", parent=_styles["BodyText"], fontSize=9.5, leading=12.5)
MEANS = ParagraphStyle("means", parent=BODY, textColor=colors.HexColor("#1f3a5f"), spaceAfter=5)
SCORE = ParagraphStyle("score", parent=BODY, fontSize=26, leading=32, spaceAfter=2)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8.5, leading=10.5)
LABEL_BOX = ParagraphStyle("label", parent=BODY, fontSize=9, leading=12, backColor=colors.HexColor("#fff4d6"),
                           borderColor=colors.HexColor("#c9a227"), borderWidth=0.75, borderPadding=6, spaceAfter=10)
FLAG_BOX = ParagraphStyle("flag", parent=LABEL_BOX, backColor=colors.HexColor("#fde2e1"),
                          borderColor=colors.HexColor("#b42318"))


def _table(header: list[str], rows: list[list[Any]], widths: list[float]) -> Table:
    data = [[Paragraph(f"<b>{h}</b>", CELL) for h in header]] + \
           [[Paragraph(str(c), CELL) for c in row] for row in rows]
    t = Table(data, colWidths=[w * inch for w in widths], repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#8a9bb0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fb")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _section(title: str, means: str, *body: Any) -> KeepTogether:
    return KeepTogether([Paragraph(title, H2), Paragraph(f"<b>What this means:</b> {means}", MEANS), *body])


def _score_part(report: dict[str, Any]) -> list[Any]:
    s = report["score"]
    rows = []
    for comp, c in s["components"].items():
        counted = [r for r in c["rule_results"] if r["counted"]]
        behind = "; ".join(f"{r['rule_name']}: {r['affected']} of {r['evaluated']:,} ({r['affected_pct']:.2f}%, "
                           f"{r['severity']} x{r['multiplier']:g})" for r in counted) or "nothing found"
        rows.append([COMPONENTS.get(comp, comp), f"{c['weight']:g}", f"{c['penalty']:.2f}", f"{c['deduction']:.2f}", behind])
    return [
        Paragraph(f"<b>{s['overall_score']}</b><font size=12> / 100</font>", SCORE),
        Paragraph(f"<b>What this means:</b> {s['what_this_means']}", MEANS),
        _table(["Component", "Weight", "Penalty (0-100)", "Points deducted", "Checks behind the deduction"], rows,
               [1.4, 0.62, 0.78, 0.75, 3.45]),
        Paragraph("Score = 100 minus the sum of weight x penalty / 100 (DataTrust AI's method). A penalty is the "
                  "share of records a check flagged, times its severity (Critical 3, High 2, Medium 1, Low 0.5).", SMALL),
    ]


def _sections(report: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    for sec in report["sections"]:
        d = sec["data"]
        if sec["id"] == "records_by_stage":
            body = [_table(["Step", "Count", "Made up of", "Adds up"],
                           [[c["step"], f"{c['value']:,}", " + ".join(f"{v:,} {k}" for k, v in c["parts"].items()),
                             "yes" if c["adds_up"] else "NO"] for c in d["checks"]], [1.6, 0.7, 4.0, 0.7]),
                    Spacer(1, 4),
                    _table(["Home readings", "Count"],
                           [["Asked for by the protocol (2 a day for 10 days)", f"{d['home']['readings_expected_by_protocol']:,}"],
                            ["Usable readings received", f"{d['home']['accepted_unique_readings']:,}"],
                            ["Completeness", f"{d['home']['completeness_pct']}%"]], [5.0, 1.0])]
        elif sec["id"] == "rejections_by_reason":
            rows = [[REASONS.get(r["reason"], r["reason"]), r["lines"], f"{r['share_pct']}%"] for r in d["home"]]
            rows += [[f"EHR record: {r['reason']}", r["records"], ""] for r in d["fhir"]]
            body = [_table(["Reason", "Lines", f"Share of {d['home_lines_checked']} lines"], rows, [3.6, 0.8, 1.6])]
        elif sec["id"] == "validation_pass_rates":
            body = [_table(["Check", "Severity", "Checked", "Flagged", "Pass rate"],
                           [[r["name"], r["severity"], f"{r['evaluated']:,}", r["affected"], f"{r['pass_rate_pct']:.2f}%"]
                            for r in d["rules"]], [3.6, 0.75, 0.85, 0.7, 0.8])]
        elif sec["id"] == "alerts_by_severity":
            body = [_table(["Severity", "Alert", "Open", "Closed", "Held back", "Total", "Patients"],
                           [[r["severity"], r["label"], r["open"], r["closed"], r["held_back"], r["total"], r["patients"]]
                            for r in d["by_severity"]], [0.8, 2.0, 0.6, 0.65, 0.8, 0.6, 0.75])]
        elif sec["id"] == "stale_patients":
            body = [_table(["Patients", "Count"],
                           [["Monitored", d["patients_evaluated"]],
                            [f"With at least one gap of {d['silence_threshold_hours']}+ hours", d["patients_with_a_silence"]],
                            ["Stopped sending before monitoring ended", d["patients_who_stopped_sending"]],
                            ["Gap alerts raised", d["silence_alerts"]]], [5.0, 1.0])]
        elif sec["id"] == "held_alerts":
            body = [_table(["Alert", "Severity", "Held back"],
                           [[r["label"], r["severity"], r["alerts"]] for r in d["by_rule"]], [3.0, 1.2, 1.0])]
            for p in sec["policy_review"]:
                body.append(Spacer(1, 6))
                why = p["why"][0].upper() + p["why"][1:]
                body.append(Paragraph(f"<b>POLICY REVIEW:</b> {p['alerts']} {p['issue']}. {why}.",
                                      FLAG_BOX))
        elif sec["id"] == "cohort":
            body = [_table(["Cohort", "Patients"],
                           [["Delivery patients in the source data", d["delivery_patients"]],
                            ["Monitored (in the cohort)", d["included"]],
                            ["&nbsp;&nbsp;&nbsp;&nbsp;with a hypertensive disorder in the source data (native)", d["native_hdp"]],
                            ["&nbsp;&nbsp;&nbsp;&nbsp;given one synthetically (augmented)", d["augmented_hdp"]],
                            ["&nbsp;&nbsp;&nbsp;&nbsp;with one in an earlier pregnancy (recorded, not used to select)",
                             d["with_prior_hdp"]],
                            ["Not monitored", d["excluded"]]], [5.0, 1.0])]
        else:
            body = []
        out.append(_section(sec["title"], sec["what_this_means"], *body))
    return out


def render_pdf(report: dict[str, Any], path: Path) -> None:
    run = report["quality_run"]

    def footer(canvas, doc) -> None:  # noqa: ANN001
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(0.6 * inch, 0.45 * inch, FOOTER)
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.45 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.75 * inch, title=report["title"],
                            author="Postpartum BP pipeline (synthetic data)", subject="Data quality report")
    story = [
        Paragraph(report["title"], H1),
        Paragraph(f"Quality run {run['run_id']} on {run['run_at'][:16]} UTC, quality config v{run['config_version']}. "
                  "Every number comes from the pipeline's own tables.", SMALL),
        Spacer(1, 8),
        Paragraph(f"<b>Read this first.</b> {report['label']}", LABEL_BOX),
        *_score_part(report),
        *_sections(report),
    ]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
