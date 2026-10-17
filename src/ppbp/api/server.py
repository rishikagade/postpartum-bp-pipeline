"""Phase 7: read-only API and viewer (R8).

    python -m ppbp.api.server              # http://127.0.0.1:8000
    python -m ppbp.api.server --port 9000

GET only. Every /api call goes through the access layer (ppbp.privacy.access), which checks the
role against config/access.toml and writes audit.access_log, allowed or not (D32). Roles are
simulated: the caller names them in the X-PPBP-Role and X-PPBP-Actor headers, as the viewer at /
does. There is no real authentication, which is out of scope (REQUIREMENTS).

Every response carries the banner "Synthetic data. Not for clinical use.", and the viewer shows
it on every screen. The server listens on localhost only and handles one request at a time,
because DuckDB allows one writer per database file and every read writes to the access log.
"""

from __future__ import annotations

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ppbp import DB_PATH
from ppbp.privacy.access import ACCESS_PATH, LEAK_REPORT, QUALITY_REPORT, AccessDenied, AccessLayer, Actor, NotFound, load_policy

BANNER = "Synthetic data. Not for clinical use."
STATIC = Path(__file__).parent / "static"
ID = r"([A-Za-z0-9\-]+)"

# (pattern, handler(layer, match, query)). Every handler is a single access-layer call.
ROUTES: list[tuple[re.Pattern, Callable[[AccessLayer, re.Match, dict], Any]]] = [
    (re.compile(r"^/api/alerts$"), lambda a, m, q: a.list_alerts()),
    (re.compile(rf"^/api/alerts/{ID}$"), lambda a, m, q: a.read_alert(m[1])),
    (re.compile(rf"^/api/alerts/{ID}/explain$"), lambda a, m, q: a.explain_alert(m[1])),
    (re.compile(r"^/api/patients$"), lambda a, m, q: a.list_patients()),
    (re.compile(rf"^/api/patients/{ID}/timeline$"), lambda a, m, q: a.read_timeline(m[1])),
    (re.compile(rf"^/api/patients/{ID}/contact$"), lambda a, m, q: a.read_contact(m[1], q.get("alert_id", [""])[0])),
    (re.compile(r"^/api/loads$"), lambda a, m, q: a.read_loads()),
    (re.compile(r"^/api/quality$"), lambda a, m, q: a.read_quality_report()),
    (re.compile(r"^/api/access-log$"), lambda a, m, q: a.read_access_log()),
    (re.compile(r"^/api/holds$"), lambda a, m, q: a.read_holds()),
    (re.compile(r"^/api/leak-check$"), lambda a, m, q: a.read_leak_check()),
]


def make_handler(db_path: Path, policy_path: Path = ACCESS_PATH, quality_report: Path = QUALITY_REPORT,
                 leak_report: Path = LEAK_REPORT) -> type[BaseHTTPRequestHandler]:
    roles = sorted(load_policy(policy_path))

    class Handler(BaseHTTPRequestHandler):
        server_version = "ppbp"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # the access log is the record of reads
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.send_header("X-PPBP-Banner", BANNER)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps({"banner": BANNER, **payload}, default=str).encode()
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            if url.path == "/api/meta":
                return self._json(200, {"data": {"roles": roles, "banner": BANNER}})
            role, actor = self.headers.get("X-PPBP-Role", ""), self.headers.get("X-PPBP-Actor", "")
            if role not in roles or not re.fullmatch(r"[A-Za-z0-9._\-]{1,40}", actor):
                return self._json(401, {"error": "name a role (X-PPBP-Role) and an actor (X-PPBP-Actor); "
                                                 "roles are simulated"})
            layer = AccessLayer(Actor(actor, role), db_path, policy_path, quality_report, leak_report)
            for pattern, handler in ROUTES:
                if m := pattern.match(url.path):
                    try:
                        return self._json(200, {"data": handler(layer, m, parse_qs(url.query))})
                    except AccessDenied as e:
                        return self._json(403, {"error": "denied", "reason": e.reason})
                    except NotFound:
                        return self._json(404, {"error": "not found"})
            return self._json(404, {"error": "no such endpoint"})

        def _static(self, path: str) -> None:
            if path == "/favicon.ico":                     # no icon; answer so browsers don't log a 404
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/app.css": ("app.css", "text/css; charset=utf-8")}
            if path not in files:
                return self._json(404, {"error": "not found"})
            name, ctype = files[path]
            self._send(200, (STATIC / name).read_bytes(), ctype)

        def _read_only(self) -> None:
            self._json(405, {"error": "read-only API: GET only"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only  # noqa: N815

    return Handler


def make_server(db_path: Path = DB_PATH, host: str = "127.0.0.1", port: int = 8000, **paths: Path) -> HTTPServer:
    return HTTPServer((host, port), make_handler(db_path, **paths))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = make_server(args.db, port=args.port)
    print(f"== {BANNER}\n   Viewer and read-only API on http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
