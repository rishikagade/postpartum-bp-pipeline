"""Download and unpack Synthea's published FHIR R4 sample data.

Java (needed to run Synthea itself) is not assumed, so we use a fixed,
published sample. The checksum pins the exact file so every run of this
project starts from identical inputs.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

from ppbp import DATA_DIR, FHIR_DIR

SAMPLE_URL = (
    "https://synthetichealth.github.io/synthea-sample-data/downloads/"
    "synthea_sample_data_fhir_r4_nov2021.zip"
)
SAMPLE_SHA256 = "6d3c5433bcae4791bc5c30469d1445b430fb4894d5c13bda15fee0584bbd7778"
ZIP_PATH = DATA_DIR / "raw" / "synthea_download" / "r4_nov2021.zip"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} (~95 MB)...")
    urllib.request.urlretrieve(url, dest)


def safe_extract(zip_path: Path, target: Path) -> None:
    """Extract only .json files, refusing any path that escapes `target`."""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if member.is_dir() or not member.filename.endswith(".json"):
                continue
            out = (target / Path(member.filename).name).resolve()
            if root not in out.parents:
                raise ValueError(f"Refusing unsafe path in archive: {member.filename}")
            with zf.open(member) as src, out.open("wb") as dst:
                dst.write(src.read())


def main() -> int:
    if FHIR_DIR.exists() and any(FHIR_DIR.glob("*.json")):
        print(f"FHIR data already present at {FHIR_DIR}")
        return 0
    if not ZIP_PATH.exists():
        download(SAMPLE_URL, ZIP_PATH)
    actual = sha256_of(ZIP_PATH)
    if actual != SAMPLE_SHA256:
        print(f"Checksum mismatch: expected {SAMPLE_SHA256}, got {actual}", file=sys.stderr)
        return 1
    safe_extract(ZIP_PATH, FHIR_DIR)
    print(f"Extracted {len(list(FHIR_DIR.glob('*.json')))} files to {FHIR_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
