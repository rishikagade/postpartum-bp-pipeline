PYTHON ?= python3.11
VENV := .venv
PY := $(VENV)/bin/python
# Editable-install .pth files can get the macOS "hidden" flag, which Python skips.
export PYTHONPATH := src

.PHONY: setup data explore walkthrough ingest cohort home test

# One-time setup: virtualenv, dependencies, and the Synthea sample download.
setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e ".[dev]"
	$(PY) -m ppbp.data.fetch_synthea

data:
	$(PY) -m ppbp.data.fetch_synthea

# Phase 1: summarize the FHIR resources and the blood pressure observations.
explore:
	$(PY) -m ppbp.fhir.explore

# Phase 1: plain-language timeline of the walkthrough patient.
walkthrough:
	$(PY) -m ppbp.fhir.explore --patient REMOVED-RECORD-NUMBER

# Phase 2: load the FHIR bundles into DuckDB (data/ppbp.duckdb). Safe to re-run.
ingest:
	$(PY) -m ppbp.fhir.ingest

# Phase 3: postpartum cohort (config/cohort.toml). Loads its augmentation bundle via ingest.
cohort:
	$(PY) -m ppbp.cohort.build

# Phase 4: simulate home BP readings, load and clean them, build timelines, and reconcile
# what was injected against what cleaning did (config/home.toml).
home:
	$(PY) -m ppbp.home.simulate
	$(PY) -m ppbp.home.load
	$(PY) -m ppbp.home.reconcile

test:
	$(PY) -m pytest -q
