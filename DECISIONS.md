# Decisions and assumptions

Each entry: what was decided, why, and what it costs. Newest phase last.

## Phase 1: Data source and exploration

### D1. Synthea's published sample data instead of running Synthea
Java is not installed on the development machine, so Synthea (a Java program) can't be run. Following the plan's fallback, the project uses Synthea's published **FHIR R4 sample from November 2021** (`synthea_sample_data_fhir_r4_nov2021.zip`). `make setup` downloads it and checks its SHA-256 before extracting. No SHA-256 checksum for this archive was found in Synthea's download listing or sample-data repository. The pinned value's provenance is undocumented in this project; the archive in this checkout matches it, so the check detects later changes but does not independently authenticate the original download.
- Why this sample: the exploration summary reports 555 Patient resources, slightly above the 200–500 target. No verified patient count for the newer "latest" sample is recorded here, so it is not used as a comparison.
- Cost: the patient count and random seed can't be tuned. The dataset is fixed, which makes it fully reproducible.
- Patient count: 555 is slightly above the 500 target, but a full summary pass takes about 7 seconds, so all patients are used rather than an arbitrary subset. **Confirmed in review:** all 555 are used, and the 200–500 range is a soft guide.

### D2. Raw data isn't committed to git
The download is 95 MB zipped and 1.1 GB unzipped. It's reproducible from a pinned URL and checksum, so `data/raw/` is git-ignored.

### D3. The postpartum cohort is anchored on real Synthea deliveries (confirmed, built in Phase 3)
The data already contains pregnancies. 84 patients have a delivery Procedure (Childbirth, Cesarean section, Instrumental delivery, or Spontaneous breech delivery). 18 of them have a Preeclampsia or Antepartum eclampsia Condition at some point. So the cohort will **not** be built by tagging arbitrary women of childbearing age. Proposed instead:
1. Anchor: the patient's most recent delivery Procedure. Its Encounter's `period.end` is the discharge time.
2. "Native" hypertensive disorder of pregnancy (HDP): a Preeclampsia or Antepartum eclampsia onset within 280 days before that delivery. By a manual count from the exploration output, about 11 living patients qualify. The cohort step will recompute this in code.
3. "Augmented" HDP: a seeded, configurable fraction of the remaining delivery patients gets a **new** Condition resource. Its code is SNOMED 48194001 "Pregnancy-induced hypertension" *(code to be checked in Phase 3)*, and it carries a `meta.tag` marking it as synthetic cohort augmentation. These resources go in a separate FHIR bundle file, so they flow through the same ingestion and lineage as everything else. The original Synthea files are never edited.
4. Exclusions: patients who died before discharge + 10 days.

**Confirmed in review:** the default augmentation fraction is ~50% of non-native delivery patients (configurable, seeded), which gives roughly 45 HDP patients.

Cost: a small cohort (roughly 40–50 patients). That's enough to exercise every pipeline path, and it avoids inventing deliveries.

### D4. Discharge = end of the delivery encounter, as Synthea records it
Every Synthea delivery happens in an "Obstetric emergency hospital admission" (class EMER) that lasts about one hour. Real postpartum stays are longer. The proposal is to keep the source value rather than invent a length of stay, and list it as a limitation. **Confirmed in review:** keep the source value; no synthetic length of stay.

### D5. Clinical BP readings are sparse, and that's expected
Synthea records BP panels mainly at annual wellness exams and the first prenatal visit. The walkthrough patient has no BP reading at delivery or at the postnatal visit. The timeline will therefore be dominated by simulated home readings, with a few clinical points for context.

### D6. Identifying data appears outside the Patient resource
Besides `Patient.name/address/telecom`, the data has:
- the patient's name in `Encounter.subject.display`
- clinician names in `participant.individual.display`
- SSN, driver's license, and passport numbers in `Patient.identifier`
- a generated HTML narrative in `Patient.text`

Implication for Phase 5b: moving Patient fields into their own table isn't enough. The raw FHIR store must be treated as a restricted zone, and mapped clinical tables must only copy coded and ID fields, never `display` strings that could hold names. **Confirmed in review:** the raw store is kept unmodified and restricted, and is not scrubbed at ingest.

### D7. Clinical BP values can be fractional
Systolic values range from 42.473 to 200 mmHg. Some are non-integer (a Synthea artifact). The mapping will keep values as `DOUBLE`, and plausibility rules will work on ranges rather than assume whole numbers.

## Patterns from DataTrust AI (reviewed from github.com/rishikagade/DataTrust_AI)

What DataTrust AI does, so later phases reuse it accurately:
- **Scoring** (`backend/app/services/scoring.py`): weighted components (missing values 25, duplicates 20, invalid type 20, critical failures 15, outliers 10, business rules 10) and a severity multiplier (Critical 3, High 2, Medium 1, Low 0.5). The score is 100 minus the weighted penalty sum, and per-component deductions are returned in `calculation_detail`. **Carries over** in Phase 6.
- **Rule results** (`backend/app/rules/implementations.py`): every result has `rule_id`, `rule_name`, `severity`, `affected_count`, `affected_pct`, and `description`. **Carries over** as the shape of `validation_results`.
- **Differences, stated plainly:**
  - DataTrust's thresholds are in Python code, not a config file. Here, rules move into one versioned config file.
  - DataTrust's results are column-level counts. Here, they must be **per record**, because alerts need record-level lineage.
  - Both are extensions of DataTrust's approach, not reuse of existing behavior.

## Phase 2: FHIR ingestion

### D8. DuckDB as the store, one file, two schemas
Everything lives in one DuckDB file (`data/ppbp.duckdb`, git-ignored). The `raw` schema is the restricted zone from D6: unmodified FHIR JSON. The `ops` schema holds load bookkeeping. DuckDB has no roles, so "restricted" is a code convention for now: only mapping code reads `raw`. Phase 5b decides how to show this to the privacy officer.
- Cost: the raw store is about 1.2 GB, because it keeps the full JSON of every resource version. That's the price of tracing any record back to the exact source JSON.

### D9. Identity and change detection
A resource is identified by `(resourceType, id)`. Its version is the SHA-256 of its JSON with sorted keys and no whitespace, so key order and formatting never count as a change. Checked on the sample: every one of the 409,973 entries has an `id`, and no `(resourceType, id)` appears twice across the 557 files.
- `raw.fhir_resource_version` keeps every distinct version and is never updated. `raw.fhir_resource_current` points at the latest one. `ops.resource_change` lists every new or changed resource per load. Unchanged resources are counted, not listed, so a reload doesn't add 410k rows.
- Resources missing from a later load are **not** treated as deleted. The Phase 3 augmentation bundle is loaded on its own, so "absent from this load" doesn't mean "deleted". Deletion handling is listed as a limitation.

### D10. Loads are all-or-nothing, and nothing is dropped silently
Each load runs in one transaction. A failure rolls everything back and leaves a `failed` row in `ops.load_run` with the error. Bad input doesn't fail the load. It's written to `ops.load_rejection` with a reason: `invalid_json`, `not_a_bundle`, `missing_resource_type`, `missing_id`, `duplicate_in_load` (identical copy), or `conflicting_duplicate_in_load` (a different copy; the first one in file-name order is kept). Each input file's SHA-256 goes in `ops.load_file`, so it's always clear exactly which bytes were loaded.

### D11. The project folder is iCloud-synced, which slows the database ~5x
The repo sits under `~/Documents`, which iCloud Drive syncs on this machine. A full load takes about 50 s with the database outside iCloud, and about 4.5 min inside it (the CPU sits mostly idle). Setting `PPBP_DB_PATH` puts the database elsewhere. Sync also sets macOS's `hidden` flag on the venv's editable-install `.pth` file, and Python 3.11.13 skips hidden `.pth` files, so the Makefile and pytest put `src` on `PYTHONPATH` directly. *(Open question: move the repo out of `~/Documents`?)*
