# Phase 1: FHIR in plain language

> Synthetic data (Synthea). Not real patients, not for clinical use.
> Reproduce everything here with `make explore` and `make walkthrough`.

## FHIR in five ideas

1. **Everything is a "resource".** A resource is a small JSON document of one kind: `Patient`, `Encounter` (a visit), `Condition` (a diagnosis), `Observation` (a measurement), `Procedure`, and so on. Each has a `resourceType` and an `id`.
2. **Resources point at each other with references.** An Observation doesn't repeat the patient's details. It says `"subject": {"reference": "urn:uuid:<patient id>"}` and `"encounter": {"reference": "urn:uuid:<encounter id>"}`. To answer "which visit was this BP taken at?", you follow the reference.
3. **Meaning comes from codes, not text.** A diagnosis is `{"system": "http://snomed.info/sct", "code": "198992004", "display": "Antepartum eclampsia"}`. The `system` names the code list (SNOMED CT for diagnoses and procedures, LOINC for lab tests and vital signs), and the `code` is what software should match on. `display` is just for humans.
4. **A Bundle is an envelope of resources.** Synthea writes one `transaction` Bundle per patient, containing every resource in that patient's history, plus two `batch` Bundles of shared hospitals and practitioners. Encounters point at those by identifier (`Practitioner?identifier=...|9999999989`) rather than by `urn:uuid`.
5. **Times carry their own offset.** `"2021-09-28T06:41:56-04:00"` is local time with its UTC offset. The offset changes with daylight saving (`-04:00` in summer, `-05:00` in winter), so we normalize to UTC before comparing.

## What's in this dataset

From `make explore` (full output: `data/out/explore_summary.txt`):

| Resource | Count | What it is here |
|---|---:|---|
| Patient | 555 | One per person (302 female, 253 male; 55 deceased) |
| Encounter | 27,812 | Every visit: wellness exams, prenatal visits, ER, admissions |
| Condition | 17,253 | Diagnoses *and* social findings ("Full-time employment", "Stress") |
| Observation | 131,703 | Vitals, labs, and survey answers. BP panels are 6,071 of these. |

The rest (Claim, ExplanationOfBenefit, DiagnosticReport, and others) is billing and reporting detail we don't need for this project.

## How blood pressure is stored

One BP reading is **one Observation with two components**, not two separate Observations:

```jsonc
{
  "resourceType": "Observation",
  "id": "75096338-...",
  "status": "final",
  "category": [{ "coding": [{ "code": "vital-signs" }] }],
  "code": { "coding": [{ "system": "http://loinc.org", "code": "85354-9", "display": "Blood Pressure" }] },
  "subject":   { "reference": "urn:uuid:<record number>" },   // which patient
  "encounter": { "reference": "urn:uuid:841edb84-..." },   // which visit
  "effectiveDateTime": "2021-11-16T00:14:56-05:00",         // when it was measured
  "component": [
    { "code": { "coding": [{ "code": "8462-4", "display": "Diastolic Blood Pressure" }] },
      "valueQuantity": { "value": 74,  "unit": "mm[Hg]", "system": "http://unitsofmeasure.org" } },
    { "code": { "coding": [{ "code": "8480-6", "display": "Systolic Blood Pressure" }] },
      "valueQuantity": { "value": 136, "unit": "mm[Hg]", "system": "http://unitsofmeasure.org" } }
  ]
}
```

- `code` 85354-9 is the LOINC **panel** code ("this is a blood pressure"). The numbers sit in `component`, each tagged with its own LOINC code: **8480-6 = systolic**, **8462-4 = diastolic**.
- **Don't rely on component order.** Here diastolic comes first. The mapper looks components up by code.
- Across all 6,071 panels: every one has both components, every value is in `mm[Hg]` (UCUM units), and every one links to an encounter. There are no standalone systolic Observations.
- Values range from 42.5 to 200 systolic and 43.3 to 121 diastolic, and some are fractional. That's real-world-shaped noise the mapper should keep (see DECISIONS D7).

## One patient, start to finish

Enrolled patient #47, the 47th of the 48 cohort patients in order of discharge (`make walkthrough`, or `make walkthrough PATIENT=47`). She's referred to only by enrollment number. Her FHIR `Patient.id` doubles as her medical record number, so since Phase 5b it isn't printed or committed anywhere. The enrollment number reads the same on any machine (DECISIONS D30).

**Who she is.** A female patient born in 1974, living. Her bundle holds 913 resources, mostly Observations, Claims, and Procedures. The Patient resource carries name, address, and phone, which are the identifying fields Phase 5b moves into their own table.

**Background.** Her Condition list starts in 1992 with **Hypertension** (still active) and later adds obesity, prediabetes, and migraine. Synthea also records social findings as Conditions (employment, stress, social isolation), so a "diagnoses" table must filter by code, not just take every Condition.

**Pregnancy.** In 2021 the record tells a clear story:

| Date | Resource | What happened |
|---|---|---|
| 2021-02-23 | Encounter + Condition | Prenatal initial visit; "Normal pregnancy" begins; BP 149/115 recorded |
| Mar–Sep 2021 | Encounter ×8 | Monthly, then biweekly, prenatal visits |
| 2021-09-07 | Condition | **Antepartum eclampsia** diagnosed: a hypertensive disorder of pregnancy |
| 2021-09-28 06:41 | Encounter (EMER) + Procedure | Obstetric emergency admission; **Childbirth** procedure |
| 2021-09-28 07:41 | Encounter `period.end` | Discharge, one hour later (a Synthea simplification) |
| 2021-11-09 | Encounter | Postnatal visit |

The "Normal pregnancy" and eclampsia Conditions both have `abatementDateTime` 2021-09-28, the delivery date. That's how FHIR says "this condition ended".

She also has a 2000 "Obstetric emergency hospital admission" with **no delivery procedure attached**. So the cohort logic keys off the delivery *Procedure*, not the encounter type.

**Her blood pressure.** Ten BP panels, almost all at yearly "General examination" visits from 2012 to 2020, between 140–173 systolic and 93–118 diastolic, consistent with her chronic hypertension. The last is at the prenatal initial visit. **There's no BP at delivery or at the postnatal visit.** That gap is exactly what home monitoring is meant to fill, and why the timeline in later phases combines clinical readings with home readings.

## What this means for the pipeline

- **Mapping** follows references (`subject`, `encounter`) and matches on codes (LOINC and SNOMED), never on display text.
- **Cohort** is anchored on delivery Procedures (84 patients). 18 have a native Preeclampsia or Eclampsia diagnosis, and roughly 11 of those fall in the pregnancy that ended in their most recent delivery. See DECISIONS D3.
- **PHI lives in more places than Patient**: names in `Encounter.subject.display`, SSN and passport numbers in `Patient.identifier`. See DECISIONS D6.
- **Time** must be normalized to UTC, because offsets shift with daylight saving.
