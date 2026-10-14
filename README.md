# Postpartum Blood Pressure Data Pipeline with Lineage

> **Synthetic data. Not for clinical use.** This is a data engineering demonstration built on [Synthea](https://synthetichealth.github.io/synthea/) synthetic patients and simulated home readings. It makes no claim of clinical validity, accuracy, or outcomes.

Work in progress, built in phases. Full README in Phase 8.

## Quick start (so far)

```bash
make setup        # virtualenv + download Synthea FHIR R4 sample (checksum-verified)
make explore      # Phase 1: resource summary, examples, BP structure
make walkthrough  # Phase 1: one patient's record, in plain language
```

- Requirements: [REQUIREMENTS.md](REQUIREMENTS.md)
- Decisions and assumptions: [DECISIONS.md](DECISIONS.md)
- FHIR walkthrough: [docs/phase1_fhir_walkthrough.md](docs/phase1_fhir_walkthrough.md)
