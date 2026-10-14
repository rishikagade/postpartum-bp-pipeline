"""Postpartum blood pressure pipeline. Synthetic data only; not a clinical tool."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
FHIR_DIR = DATA_DIR / "raw" / "synthea_r4_nov2021" / "fhir"
