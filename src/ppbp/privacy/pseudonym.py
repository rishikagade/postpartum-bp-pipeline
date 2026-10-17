"""Phase 5b: pseudonymous study IDs (D30).

Synthea's Patient.id is also the patient's medical record number (MR identifier), so it must
not leave the restricted zones (raw, phi). Every downstream table uses a study ID instead:

    study_id = "SID-" + first 12 hex chars of HMAC-SHA256(key, Patient.id)

The key is a secret kept outside git: the PPBP_PSEUDONYM_KEY environment variable (hex), or
data/secrets/pseudonym.key, created on first use with owner-only permissions. The mapping is
stored only in phi.patient_key, with the key's fingerprint, so a changed key is caught instead
of silently re-keying everyone.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

import duckdb

from ppbp import DATA_DIR

KEY_PATH = DATA_DIR / "secrets" / "pseudonym.key"
PREFIX = "SID-"


def load_key(path: Path = KEY_PATH) -> bytes:
    env = os.environ.get("PPBP_PSEUDONYM_KEY")
    if env:
        return bytes.fromhex(env)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32) + "\n")
    return bytes.fromhex(path.read_text().strip())


def fingerprint(key: bytes) -> str:
    """Identifies the key without revealing it."""
    return hashlib.sha256(b"ppbp-key-fingerprint:" + key).hexdigest()[:16]


def study_id(patient_id: str, key: bytes) -> str:
    return PREFIX + hmac.new(key, patient_id.encode(), hashlib.sha256).hexdigest()[:12]


def ensure_study_ids(con: duckdb.DuckDBPyConnection, patient_ids: list[str], key: bytes | None = None
                     ) -> dict[str, str]:
    """Return {Patient.id: study_id}, recording any new pairs in phi.patient_key."""
    key = key or load_key()
    fp = fingerprint(key)
    other = con.execute("SELECT DISTINCT key_fingerprint FROM phi.patient_key WHERE key_fingerprint <> ?",
                        [fp]).fetchall()
    if other:
        raise RuntimeError("phi.patient_key was built with a different pseudonym key. Restore that key, or "
                           "rebuild every downstream table from scratch with the new one.")
    mapping = {pid: study_id(pid, key) for pid in sorted(set(patient_ids))}
    existing = dict(con.execute("SELECT patient_id, study_id FROM phi.patient_key").fetchall())
    new = [(sid, pid, fp) for pid, sid in mapping.items() if pid not in existing]
    if new:
        con.executemany("INSERT INTO phi.patient_key VALUES (?, ?, ?)", new)
    return mapping
