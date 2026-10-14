# Decisions and assumptions

Each entry: what was decided, why, and what it costs. Newest phase last.

## Phase 1: Data source and exploration

### D1. Synthea's published sample data instead of running Synthea
Java is not installed on the development machine, so Synthea (a Java program) can't be run. Following the plan's fallback, the project uses Synthea's published **FHIR R4 sample from November 2021** (`synthea_sample_data_fhir_r4_nov2021.zip`). `make setup` downloads it and checks its SHA-256 before extracting.
- Why this sample: it has 555 patients. The newer "latest" sample has only about 108, below the 200–500 target.
- Cost: the patient count and random seed can't be tuned. The dataset is fixed, which makes it fully reproducible.
- Patient count: 555 is slightly above the 500 target, but a full summary pass takes about 7 seconds, so all patients are used rather than an arbitrary subset. *(Open question for review.)*

### D2. Raw data isn't committed to git
The download is 95 MB zipped and 1.1 GB unzipped. It's reproducible from a pinned URL and checksum, so `data/raw/` is git-ignored.

### D3. The postpartum cohort is anchored on real Synthea deliveries (proposal, built in Phase 3)
The data already contains pregnancies. 84 patients have a delivery Procedure (Childbirth, Cesarean section, Instrumental delivery, or Spontaneous breech delivery). 18 of them have a Preeclampsia or Antepartum eclampsia Condition at some point. So the cohort will **not** be built by tagging arbitrary women of childbearing age. Proposed instead:
1. Anchor: the patient's most recent delivery Procedure. Its Encounter's `period.end` is the discharge time.
2. "Native" hypertensive disorder of pregnancy (HDP): a Preeclampsia or Antepartum eclampsia onset within 280 days before that delivery. By a manual count from the exploration output, about 11 living patients qualify. The cohort step will recompute this in code.
3. "Augmented" HDP: a seeded, configurable fraction of the remaining delivery patients gets a **new** Condition resource. Its code is SNOMED 48194001 "Pregnancy-induced hypertension" *(code to be checked in Phase 3)*, and it carries a `meta.tag` marking it as synthetic cohort augmentation. These resources go in a separate FHIR bundle file, so they flow through the same ingestion and lineage as everything else. The original Synthea files are never edited.
4. Exclusions: patients who died before discharge + 10 days.

Cost: a small cohort (roughly 40–50 patients). That's enough to exercise every pipeline path, and it avoids inventing deliveries.

### D4. Discharge = end of the delivery encounter, as Synthea records it
Every Synthea delivery happens in an "Obstetric emergency hospital admission" (class EMER) that lasts about one hour. Real postpartum stays are longer. The proposal is to keep the source value rather than invent a length of stay, and list it as a limitation. *(Open question for review.)*

### D5. Clinical BP readings are sparse, and that's expected
Synthea records BP panels mainly at annual wellness exams and the first prenatal visit. The walkthrough patient has no BP reading at delivery or at the postnatal visit. The timeline will therefore be dominated by simulated home readings, with a few clinical points for context.

### D6. Identifying data appears outside the Patient resource
Besides `Patient.name/address/telecom`, the data has:
- the patient's name in `Encounter.subject.display`
- clinician names in `participant.individual.display`
- SSN, driver's license, and passport numbers in `Patient.identifier`
- a generated HTML narrative in `Patient.text`

Implication for Phase 5b: moving Patient fields into their own table isn't enough. The raw FHIR store must be treated as a restricted zone, and mapped clinical tables must only copy coded and ID fields, never `display` strings that could hold names.

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
