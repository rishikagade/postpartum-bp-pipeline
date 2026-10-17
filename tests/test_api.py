import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import duckdb
import pytest

from ppbp.api.server import BANNER, STATIC, make_server
from ppbp.db import connect
from ppbp.privacy.access import load_policy

# Endpoint -> the access-layer action it needs ({a}: an alert, {p}: that alert's patient).
ENDPOINTS = {
    "/api/alerts": "list_alerts",
    "/api/alerts/{a}": "read_alert",
    "/api/alerts/{a}/explain": "explain_alert",
    "/api/patients": "list_patients",
    "/api/patients/{p}/timeline": "read_timeline",
    "/api/patients/{p}/contact?alert_id={a}": "read_contact",
    "/api/loads": "read_loads",
    "/api/quality": "read_quality_report",
    "/api/access-log": "read_access_log",
    "/api/holds": "read_holds",
    "/api/leak-check": "read_leak_check",
}


@pytest.fixture
def api(world, tmp_path: Path):
    # The quality and leak-check reports the API serves, built for this test's world.
    from ppbp.privacy import leak_check
    from ppbp.quality.report import build_report, write_report

    write_report(build_report(world["db"]), tmp_path / "reports")
    leak_check.run(world["db"], tmp_path / "out", tmp_path / "leak_check.json", scan_repo=False)
    server = make_server(world["db"], port=0, quality_report=tmp_path / "reports" / "quality_report.json",
                         leak_report=tmp_path / "leak_check.json")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path: str, role: str | None = "nurse", method: str = "GET") -> tuple[int, dict, dict]:
        req = urllib.request.Request(base + path, method=method)
        if role:
            req.add_header("X-PPBP-Role", role)
            req.add_header("X-PPBP-Actor", f"{role}.test")
        try:
            with urllib.request.urlopen(req) as res:
                body = res.read()
                ctype = res.headers.get("Content-Type") or ""
                return res.status, dict(res.headers), json.loads(body) if "json" in ctype else {"raw": body.decode()}
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read())

    yield {"call": call, "db": world["db"], "base": base}
    server.shutdown()
    server.server_close()


def _q(db: Path, sql: str, params: list | None = None) -> list[tuple]:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def _visible_alert(db: Path) -> tuple[str, str]:
    return _q(db, "SELECT alert_id, patient_id FROM alerts.alert WHERE status = 'open' ORDER BY severity_rank, alert_id LIMIT 1")[0]


def test_viewer_and_every_response_carry_the_banner(api) -> None:
    status, headers, body = api["call"]("/", role=None)
    assert status == 200 and body["raw"].count(BANNER) >= 2          # top banner and footer
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["X-PPBP-Banner"] == BANNER
    for path in ("/api/alerts", "/api/meta", "/api/nope"):
        assert api["call"](path)[2]["banner"] == BANNER
    assert api["call"]("/favicon.ico", role=None)[0] == 204


def test_the_api_is_read_only_and_needs_a_simulated_role(api) -> None:
    for method in ("POST", "PUT", "DELETE"):
        assert api["call"]("/api/alerts", method=method)[0] == 405
    assert api["call"]("/api/alerts", role=None)[0] == 401
    assert api["call"]("/api/alerts", role="admin")[0] == 401


def test_each_role_reaches_exactly_what_the_policy_allows_and_every_call_is_logged(api) -> None:
    alert_id, patient_id = _visible_alert(api["db"])
    policy = load_policy()
    before = _q(api["db"], "SELECT coalesce(max(access_id), 0) FROM audit.access_log")[0][0]
    expected = []
    for role, allowed in policy.items():
        for path, action in ENDPOINTS.items():
            status, _, body = api["call"](path.format(a=alert_id, p=patient_id), role=role)
            assert status == (200 if action in allowed else 403), (role, action, body)
            expected.append((f"{role}.test", action, "allowed" if action in allowed else "denied"))
    logged = _q(api["db"], "SELECT actor, action, outcome FROM audit.access_log WHERE access_id > ? ORDER BY access_id",
                [before])
    assert logged == expected


def test_held_alerts_stay_off_the_nurse_view(api) -> None:
    held = [r[0] for r in _q(api["db"], "SELECT alert_id FROM alerts.alert WHERE status = 'held_back'")]
    assert held
    listed = {a["alert_id"] for a in api["call"]("/api/alerts")[2]["data"]}
    assert not listed & set(held)
    assert api["call"](f"/api/alerts/{held[0]}")[0] == 403


def test_patient_list_reaches_every_enrolled_patient_with_counts_only(api) -> None:
    patients = api["call"]("/api/patients")[2]["data"]
    assert [p["enrollment_no"] for p in patients] == list(range(1, len(patients) + 1))
    assert len(patients) == _q(api["db"], "SELECT count(*) FROM home.enrollment")[0][0]
    assert set(patients[0]) == {"patient_id", "enrollment_no", "discharge_at", "home_monitoring_consent",
                                "home_readings", "open_alerts", "unconfirmed_elevated"}
    no_consent = [p for p in patients if not p["home_monitoring_consent"]]
    assert no_consent and all(p["home_readings"] is None for p in no_consent)


def test_unconfirmed_elevated_readings_show_on_the_timeline(api) -> None:
    # Make one elevated reading "unconfirmed", as the rule would for a lone reading (Phase 7 carry-forward).
    rid, pid = _q(api["db"], """SELECT v.record_id, v.patient_id FROM alerts.validation_result v
        JOIN home.enrollment e USING (patient_id)
        WHERE v.rule_id = 'BP_ELEVATED' AND v.outcome = 'fired' AND e.consent_home_monitoring LIMIT 1""")[0]
    with connect(api["db"]) as con:
        con.execute("UPDATE alerts.validation_result SET outcome = 'unconfirmed' WHERE rule_id = 'BP_ELEVATED' "
                    "AND record_id = ?", [rid])
    points = {r["record_id"]: r for r in api["call"](f"/api/patients/{pid}/timeline")[2]["data"]["readings"]}
    assert points[rid]["mark"] == "elevated_unconfirmed"
    assert "unconfirmed" in points[rid]["mark_label"]
    unconfirmed = sum(r["mark"] == "elevated_unconfirmed" for r in points.values())
    assert unconfirmed == _q(api["db"], "SELECT count(*) FROM alerts.validation_result WHERE patient_id = ? "
                                        "AND rule_id = 'BP_ELEVATED' AND outcome = 'unconfirmed'", [pid])[0][0]
    patients = {p["patient_id"]: p for p in api["call"]("/api/patients")[2]["data"]}
    assert patients[pid]["unconfirmed_elevated"] == unconfirmed


def test_every_timeline_reading_is_marked(api) -> None:
    for (pid,) in _q(api["db"], "SELECT patient_id FROM home.enrollment WHERE consent_home_monitoring"):
        for r in api["call"](f"/api/patients/{pid}/timeline")[2]["data"]["readings"]:
            assert r["mark"] in {"severe", "elevated_in_alert", "elevated_unconfirmed", "normal", "clinical"}
            if r["mark"] in ("severe", "elevated_in_alert"):
                assert r["alert_ids"], r


def test_contact_lookup_through_the_api_is_logged_with_its_alert(api) -> None:
    alert_id, patient_id = _visible_alert(api["db"])
    status, _, body = api["call"](f"/api/patients/{patient_id}/contact?alert_id={alert_id}")
    assert status == 200 and set(body["data"]) == {"patient_id", "display_name", "phone"}
    assert api["call"](f"/api/patients/{patient_id}/contact")[0] == 403          # no alert, no contact
    reasons = [r[0] for r in _q(api["db"], "SELECT reason FROM audit.access_log WHERE action = 'read_contact' "
                                           "ORDER BY access_id")]
    assert reasons[-2:] == [f"acting on {alert_id}", "no such alert ()"]


def test_viewer_inserts_data_as_text_only() -> None:
    js = (STATIC / "app.js").read_text()
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert risky not in js
    html = (STATIC / "index.html").read_text()
    assert "<script>" not in html and "style=" not in html          # CSP allows only same-origin files


def test_leak_check_reads_screenshots(world, tmp_path: Path) -> None:
    """Screenshots are for people: no IDs at all. Without OCR the check still reads PNG metadata."""
    import struct
    import zlib

    from ppbp.privacy import leak_check

    def png_with_text(path: Path, text: str) -> None:
        def chunk(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        raw = zlib.compress(b"\0\0\0\0")                       # one 1x1 RGB pixel row
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                         + chunk(b"tEXt", b"Comment\0" + text.encode()) + chunk(b"IDAT", raw) + chunk(b"IEND", b""))

    sid = _q(world["db"], "SELECT patient_id FROM home.enrollment LIMIT 1")[0][0]
    out = tmp_path / "out"
    out.mkdir()
    png_with_text(out / "clean.png", "Synthetic data. Not for clinical use. SID-•••")
    clean = leak_check.run(world["db"], out, tmp_path / "r.json", scan_repo=False)
    assert clean["passed"] and clean["scanned"]["images"] == 1
    png_with_text(out / "leaky.png", f"patient {sid}")
    found = leak_check.run(world["db"], out, tmp_path / "r.json", scan_repo=False)
    assert any(f["location"].endswith("leaky.png") and "study_id" in f["kinds"] for f in found["findings"])
