"""Postpartum blood pressure pipeline. Synthetic data only; not a clinical tool."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
FHIR_DIR = DATA_DIR / "raw" / "synthea_r4_nov2021" / "fhir"
# Override when the project sits in a synced folder (e.g. iCloud Documents), which slows DuckDB writes.
DB_PATH = Path(os.environ.get("PPBP_DB_PATH", DATA_DIR / "ppbp.duckdb"))
