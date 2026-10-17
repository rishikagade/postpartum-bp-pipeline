PYTHON ?= python3.11
SHELL := /bin/bash
# Each pipeline step's console output is also kept in data/out/logs/, which the leak check scans.
# `set -o pipefail` in the recipe, not .SHELLFLAGS: macOS ships make 3.81, which ignores .SHELLFLAGS,
# and without it a failing step piped through tee would look like success.
LOGS := data/out/logs
LOG = set -o pipefail; mkdir -p $(LOGS) && $(PY)
VENV := .venv
PY := $(VENV)/bin/python
# Editable-install .pth files can get the macOS "hidden" flag, which Python skips.
export PYTHONPATH := src

.PHONY: setup data explore walkthrough ingest cohort home alerts explain privacy leak-check leak-check-history report serve test

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
	$(LOG) -m ppbp.fhir.explore | tee data/out/explore_summary.txt

# Phase 1: plain-language timeline of one enrolled patient (needs `make home` first).
# PATIENT is the enrollment number: #1 is the earliest discharge, the same on any machine (D30).
PATIENT ?= 47
walkthrough:
	$(PY) -m ppbp.fhir.explore --patient $(PATIENT)

# Phase 2: load the FHIR bundles into DuckDB (data/ppbp.duckdb). Safe to re-run.
ingest:
	$(LOG) -m ppbp.fhir.ingest | tee $(LOGS)/ingest.log

# Phase 3: postpartum cohort (config/cohort.toml). Loads its augmentation bundle via ingest.
cohort:
	$(LOG) -m ppbp.cohort.build | tee $(LOGS)/cohort.log

# Phase 4: simulate home BP readings, load and clean them, build timelines, and reconcile
# what was injected against what cleaning did (config/home.toml).
home:
	$(LOG) -m ppbp.home.simulate | tee $(LOGS)/home_simulate.log
	$(LOG) -m ppbp.home.load | tee $(LOGS)/home_load.log
	$(LOG) -m ppbp.home.reconcile | tee $(LOGS)/home_reconcile.log

# Phase 5: run the alert rules (config/rules.toml) and list open alerts, most urgent first.
alerts:
	$(LOG) -m ppbp.alerts.evaluate | tee $(LOGS)/alerts.log

# Phase 5: trace one alert to its rule version and raw records: make explain ID=ALR-...
explain:
	$(PY) -m ppbp.alerts.explain $(ID)

# Phase 5b: rebuild the restricted contact table, then check nothing identifying leaked.
privacy:
	$(LOG) -m ppbp.privacy.phi | tee $(LOGS)/privacy.log
	$(MAKE) -s leak-check

# Phase 5b: scan tables, logs, reports, access-layer responses and tracked files for identifiers.
leak-check:
	$(LOG) -m ppbp.privacy.leak_check | tee $(LOGS)/leak_check.log

# Phase 5b: the same check over every file and message in every commit. Run before any push.
leak-check-history:
	$(LOG) -m ppbp.privacy.leak_check --history | tee $(LOGS)/leak_check_history.log

# Phase 6: score data quality and write the JSON and PDF report, then leak-check everything (incl. the PDF).
report:
	$(LOG) -m ppbp.quality.report | tee $(LOGS)/report.log
	$(MAKE) -s leak-check

# Phase 7: read-only API and viewer on http://127.0.0.1:$(PORT)/ (roles simulated; every read logged).
PORT ?= 8000
serve:
	$(PY) -m ppbp.api.server --port $(PORT)

test:
	$(PY) -m pytest -q
