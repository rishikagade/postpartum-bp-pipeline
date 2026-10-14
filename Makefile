PYTHON ?= python3.11
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: setup data explore walkthrough test

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

test:
	$(PY) -m pytest -q
