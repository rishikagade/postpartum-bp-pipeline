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
3. "Augmented" HDP: a seeded, configurable fraction of the remaining delivery patients gets a **new** Condition resource. Its code is SNOMED 48194001 "Pregnancy-induced hypertension" (checked in Phase 3: an active concept, and part of HL7's maternal-health value set for hypertensive disorders of pregnancy), and it carries a `meta.tag` marking it as synthetic cohort augmentation. These resources go in a separate FHIR bundle file, so they flow through the same ingestion and lineage as everything else. The original Synthea files are never edited.
4. Exclusions: patients who died before discharge + 10 days. *(Changed in Phase 3 review: only deaths before discharge are excluded. See D15.)*

**Confirmed in review:** the default augmentation fraction is ~50% of non-native delivery patients (configurable, seeded), which gives roughly 45 HDP patients.

Cost: a small cohort (roughly 40–50 patients). That's enough to exercise every pipeline path, and it avoids inventing deliveries.

**Built in Phase 3:** 84 delivery patients, 11 with native HDP (matching the manual count), 37 augmented, so **48 in the cohort** and 36 excluded. See D12–D15.

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

### D11. The project folder is iCloud-synced, so data lives in `data.nosync/`
The repo sits under `~/Documents`, which iCloud Drive syncs on this machine. A full load takes about 50 s with the database outside iCloud, and about 4.5 min inside it (the CPU sits mostly idle). Setting `PPBP_DB_PATH` puts the database elsewhere. Sync also sets macOS's `hidden` flag on the venv's editable-install `.pth` file, and Python 3.11.13 skips hidden `.pth` files, so the Makefile and pytest put `src` on `PYTHONPATH` directly.

**Decided in review:** the repo stays in `~/Documents`, and the data stays off iCloud. Locally, `data/` is a symlink to `data.nosync/`, and iCloud doesn't sync folders whose names end in `.nosync`. A full reload now takes about 43 s. Code doesn't need to know about this: ingest records paths without following symlinks, so source files are still recorded as `data/raw/...`. On a fresh clone `data/` is a normal folder, and `/data` and `/data.nosync/` are both git-ignored. `PPBP_DB_PATH` is still available for putting the database elsewhere.

## Phase 3: Postpartum cohort

### D12. The cohort definition is a versioned config file
`config/cohort.toml` holds the delivery and HDP codes, the 280-day lookback, the 10-day survival window, and the augmentation seed and fraction. Each build writes a row to `cohort.build_run` with the config's SHA-256 and the FHIR load that carried the augmentation bundle. This follows the same principle as the Phase 4+ rules config: definitions live in config, not in code.
- `make cohort` is the whole step: find deliveries, decide who's in, write the augmentation bundle (`data/generated/cohort_augmentation.json`), load it with the normal FHIR ingest, check that it arrived intact, then write `cohort.postpartum_cohort` and `cohort.exclusion`.
- Every delivery patient ends up in exactly one of those two tables. Exclusion reasons are `died_before_discharge`, `delivery_encounter_missing`, and `no_hdp_not_augmented`. On this data, all 36 exclusions are `no_hdp_not_augmented`.
- The cohort tables hold only IDs, codes and timestamps (D6). Each `*_id` points to a resource in the raw store.

### D13. Augmentation is reproducible to the byte
- **Who:** eligible patients without native HDP, sorted by ID, then a seeded `random.sample` of `fraction × pool` (rounded half up: 73 × 0.5 → 37).
- **IDs:** each augmented Condition's id is a UUIDv5 of patient + delivery procedure, so it's the same on every run. A rebuild writes an identical bundle, the reload reports all 37 unchanged, and the cohort comes out identical.
- **Onset:** a per-patient seeded draw of 14–84 days before delivery. **Confirmed in review.** This falls in late pregnancy, within the period after 20 weeks' gestation when pregnancy-induced (gestational) hypertension is diagnosed ([ACOG FAQ](https://www.acog.org/womens-health/faqs/preeclampsia-and-high-blood-pressure-during-pregnancy)). For a term delivery at 37–40 weeks, it works out to roughly 25–38 weeks' gestation. Synthea doesn't record gestational age, so a preterm delivery could put the onset slightly earlier.
- **Resource shape:** `clinicalStatus` active, `verificationStatus` confirmed, category `problem-list-item`, no encounter link, and `meta.tag` `urn:ppbp:provenance|synthetic-cohort-augmentation` on both the Condition and the bundle.
- **Never "native":** the native-HDP query skips anything with that tag, whatever its code. Without this, a rebuild could count the augmentation as native.
- Cost: changing the seed or fraction leaves the earlier augmented Conditions in the raw store, because ingest doesn't delete (D9). The cohort only uses the ones the current config selects, so they have no effect, but they remain visible in `raw`. Later phases must start from the cohort table, not from "all Conditions".

### D14. Native HDP window and earlier pregnancies
Native HDP means an onset from 280 days before delivery up to discharge. If there are several, the latest one counts. 18 delivery patients have Preeclampsia or Antepartum eclampsia at some point. 11 are within the window of their most recent delivery. The other 7 had it in an earlier pregnancy: 4 of them were then drawn for augmentation, and 3 weren't.

**Changed in review:** this risk factor is now recorded but not used for selection. `cohort.postpartum_cohort.prior_hdp` is true when the patient has an untagged Preeclampsia or Antepartum eclampsia onset before the lookback window of the most recent delivery. Membership is decided exactly as before, and a test checks this. On this data, `prior_hdp` is true for 4 cohort members, all augmented. The 3 excluded patients with prior HDP aren't in the cohort table, so the flag isn't recorded for them.

### D15. Only deaths before discharge are excluded. Later deaths stay in, with the date recorded
**Decided in review** (replaces the "discharge + 10 days" rule in D3): a patient who dies before discharge is excluded as `died_before_discharge`. A patient who dies during or after the monitoring period stays in the cohort as a deliberate edge case, and `cohort.postpartum_cohort.deceased_at` records the date.

On this data the change removes no one. Nobody died between discharge and day 10, so the cohort is still 48, with the same patients. Two cohort members die later: one 12 days after discharge, one about a year after.

**What later phases must do with `deceased_at`:**
- **Phase 4:** the home reading simulator generates no readings after `deceased_at`.
- **Phase 5:** the stale-data rule must not raise alerts for a patient after `deceased_at`. Silence after death isn't "gone quiet".

### D16. All timestamps in UTC
The DuckDB session runs in UTC. Source timestamps keep their offsets when parsed and are stored as `TIMESTAMPTZ`. Values cross into Python as UTC `TIMESTAMP`, which avoids a `pytz` dependency.

## Phase 4: Home blood pressure readings

`make home` runs three steps:
1. Simulate: `ppbp.home.simulate` writes the export files.
2. Load and clean: `ppbp.home.load` loads them, cleans them, and builds the timeline.
3. Reconcile: `ppbp.home.reconcile` compares what was injected with what cleaning did.

Every rate below is in `config/home.toml`, seeded and configurable. Each entry is marked **sourced** or **assumption**. Counts come from the pipeline's own output on the 48-patient cohort.

### D17. Monitoring: 10 days, twice daily, configurable up to 42 days (sourced)
- **10 days, twice daily:** the Heart Safe Motherhood program (the plan attributes it to Penn) texted patients "to check their blood pressures twice daily for 10 days after discharge". Source: Janssen et al., *Am J Obstet Gynecol MFM* 2021;3(6):100446, https://doi.org/10.1016/j.ajogmf.2021.100446 (abstract read via Europe PMC).
- **Up to 42 days:** a hospital-level program ran remote monitoring "after hospital discharge until 6 weeks postpartum". Source: Hauspurg et al., *Obstet Gynecol* 2019;134(4):685–691, https://doi.org/10.1097/AOG.0000000000003479 (abstract read via Europe PMC).
- **Fits ACOG's window:** ACOG Committee Opinion 736 recommends BP evaluation for HDP no later than 7–10 days postpartum, and within 72 hours for severe hypertension (README source 4).
- **Not cited:** the plan attributes the 10→42 day extension to ChristianaCare. I couldn't find any source for it (web search, Europe PMC, a 2025 *Delaware Journal of Public Health* issue, and a 2026 rapid review of 52 home-BP studies), so it isn't cited. Hauspurg 2019 supports the 42-day maximum instead.
- **Schedule:** readings at 08:00 and 18:00 local time, each ±60 min. Day 1 is the first local calendar day after discharge. *(The times are an assumption.)*

### D18. One time zone for everyone: America/New_York (assumption from pipeline output)
All 48 cohort patients have Massachusetts addresses. That was checked as an aggregate count; addresses aren't copied anywhere. Postpartum days are local calendar days in that zone.

### D19. Consent: 10% of cohort patients without home-monitoring consent (assumption)
This exists only to exercise consent gating in Phase 5b. 10% of 48, rounded half up, gives **5 patients**. Their readings are still loaded and cleaned. `home.enrollment.consent_home_monitoring` carries the flag, and Phase 5b holds their readings back from alerting.

### D20. BP profiles, so that Phase 5 alerts fire (assumption)
- **Profiles:** each patient gets one profile: severe 15% (7 patients), elevated 35% (17), or normal (24). Baselines are 146/94, 138/88 and 124/78 mm Hg, with noise SD 7/5. Outside of spikes, values are capped at 159/109, so only spikes reach the severe range.
- **Spikes:** severe-profile patients get 1–3 spikes of 160–185 systolic and 100–120 diastolic.
- **Result:** **11 severe-range readings from 6 patients** after cleaning. One severe-profile patient's spikes fell on missed or post-dropout slots. 29 patients have at least one reading at ≥140/90 that isn't severe. **Accepted in review as is.**
- **Context, not calibration:** Janssen 2021 reports elevated BP recorded for 35% of participants. The abstract doesn't define "elevated", so the 35% isn't used directly. The thresholds used to label readings are ACOG's (README source 10).

### D21. Messiness rates (one sourced figure was unverifiable, so all are assumptions)
| Issue | Configured | Produced | Of | Basis |
|---|---|---|---|---|
| Patients who stop early | 30% | 14 | 48 patients | Assumption. The plan cites ChristianaCare reporting 70% completing the full 10 days; that figure couldn't be verified (see D17), so it isn't cited. Context: in Janssen 2021, 63% submitted in both days 1–4 and days 7–10. In Hauspurg 2019, 83% continued beyond 3 weeks. |
| Missed readings | 10% | 105 | 850 scheduled slots before dropout or death | Assumption |
| Duplicate submissions | 5% | 32 | 745 original submissions | Assumption. Half are exact resends (same payload) and half are near-duplicates (new id, +1–3 min, ±4/±3 mm Hg). |
| Late submissions | 5% | 36 | 745 | Assumption. Received 1–4 days after measurement. |
| Wrong time zone | 3% | 22 | 745 | Assumption. Local wall-clock time labelled `Z`. |
| Impossible values | 1% | 3 | 745 | Assumption. Systolic 0, 999 or 25, or diastolic above systolic. |
| Unit or field errors | 1% | 6 | 745 | Assumption. kPa, `cmHg`, a missing diastolic, or a non-numeric systolic. |

A reading carries at most one value or unit problem, so each rejection has a single cause. A stopping patient submits on day 1 at least, and not on the last day. No reading is generated at or after `deceased_at` (D15).

### D22. Cleaning rules (assumptions unless stated)
- **Rejected, with a reason:** `invalid_json`, `missing_field`, `unknown_patient`, `non_numeric`, `unknown_unit`, `implausible_value`, `invalid_timestamp`, `outside_monitoring_window`, `after_death`, `duplicate_exact`, `duplicate_near`. Rejections go to `home.reading_rejection` with the exact raw line reference. On this data, every one of the 777 lines is either accepted (737) or rejected (40).
- **Plausible:** systolic 60–260, diastolic 30–160, and diastolic below systolic. Severe readings (up to 185/120 here) pass.
- **Units:** `mmHg`, `mm Hg` and `mm[Hg]` (any case) are accepted. kPa is converted at 7.50062 mm Hg/kPa, rounded, and flagged `unit_converted_kpa`. Any other unit is rejected. Values are never guessed.
- **Time zones:** if a timestamp's offset doesn't match the patient's zone at that wall-clock time, the wall-clock time is kept, re-read in the patient's zone, and flagged `tz_corrected`. This assumes patients stay in their enrolled zone; a traveling patient's correct reading would be "corrected" wrongly. Daylight-saving changes are handled; a test covers the March 2020 switch. **Accepted in review as is.** For lineage, each corrected reading carries the flag on both `home.reading` and `timeline.bp_reading`, and `home.reading.measured_at_source` keeps the timestamp exactly as submitted next to the corrected UTC `measured_at`. On this data that's 22 readings, all flagged, and no `Z`-labelled reading went uncorrected.
- **Late:** received more than 24 h after measurement. The reading is accepted and flagged `late`, not rejected, because a late reading is still a real reading.
- **Duplicates:** checked only among otherwise-valid readings, processed in order received:
  - same `submission_id` → `duplicate_exact`;
  - same patient, within 5 min, both values within 10 mm Hg → `duplicate_near`.

  The earliest received copy is kept, and the rejection names it in `duplicate_of`. A resend of an invalid reading is rejected for the underlying problem; on this data, one near-duplicate of an impossible value shows up as `implausible_value`.
- **Window:** from discharge to the end of the last postpartum day in local time, or `deceased_at` if earlier. Discharge and death come from the cohort table, not the roster.
- **Result:** `make home` reconciles every injected issue line by line. All were handled as expected (or rejected for an underlying problem, as above), none were missed, and no outcome lacked an injected cause.

### D23. Raw home exports are file snapshots; the answer key is never read by the pipeline
- `raw.home_line_version` keeps every version of every line, and `raw.home_line_current` holds the latest snapshot of each file. Unlike FHIR (D9), a line that disappears from a re-exported file is recorded as `removed`, because an export file is a complete snapshot. Re-running on the same files reports everything unchanged, and the cleaned tables come out identical.
- Each run rebuilds `home.*` and `timeline.bp_reading` inside one transaction, recorded in `home.clean_run` with the config hash.
- `injected_issues.json` is the simulator's answer key. Only `ppbp.home.reconcile` and the tests read it.
- **Count reconciliation** (added in review): `make home` prints every count from scheduled readings to accepted and rejected, and fails if any step doesn't add up. The simulator counts each scheduled slot as exactly one of after death, after dropout, missed, or submitted, so the chain is built from direct counts, not subtraction. On this data:

  | Step | Count | Made up of |
  |---|---|---|
  | Scheduled readings | 960 | 850 still expected + 110 after dropout + 0 after death |
  | Still expected | 850 | 745 submitted + 105 missed |
  | Reading lines written | 777 | 745 submitted + 32 duplicate lines |
  | Raw lines loaded | 825 | 777 reading lines + 48 enrollment lines (one per patient) |
  | Reading lines cleaned | 777 | 737 accepted + 40 rejected |
  | Rejected | 40 | 31 duplicates + 9 other reasons |
  | Accepted | 737 | = 737 home points on the timeline |

  The 48-line gap between 777 and 825 is the enrollment roster, which the raw load counts alongside the readings. It isn't a bug.
- `timeline.bp_reading` combines clean home readings with clinical BP panels (LOINC 85354-9) from 280 days before discharge to the end of monitoring. It holds IDs, codes and values only (D6). On this data that's 737 home and 26 clinical points. Panels missing a component would be skipped and counted.
