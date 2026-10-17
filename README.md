# Postpartum Blood Pressure Data Pipeline with Lineage

> **Synthetic data. Not for clinical use.** This is a data engineering demonstration built on Synthea synthetic patients [[1]][[2]] and simulated home readings. It makes no claim of clinical validity, accuracy, or outcomes.

Work in progress, built in phases. Full README in Phase 8.

## Why postpartum blood pressure

Hypertensive disorders of pregnancy (HDP) were recorded in 15.9% of US delivery hospitalizations in 2019, up from 13.3% in 2017 [[3]]. ACOG recommends a blood pressure check no later than 7–10 days postpartum for patients with HDP, and within 72 hours for those with severe hypertension. It notes that more than half of postpartum strokes occur within 10 days of discharge [[4]]. This project builds the data side of that follow-up: home readings and EHR records combined in one pipeline, where every alert can be traced back to the records that produced it.

## Quick start (so far)

```bash
make setup        # virtualenv + download Synthea FHIR R4 sample (checksum-verified)
make explore      # Phase 1: resource summary, examples, BP structure
make walkthrough  # Phase 1: one patient's record, in plain language
make ingest       # Phase 2: load the FHIR bundles into DuckDB; safe to re-run
make cohort       # Phase 3: build the postpartum cohort (config/cohort.toml)
make home         # Phase 4: simulate, load and clean home BP readings; build timelines
make alerts       # Phase 5: run the alert rules (config/rules.toml); list open alerts
make explain ID=ALR-...  # Phase 5: trace one alert to its rule version and raw records
make privacy      # Phase 5b: rebuild the restricted contact table, then run the leak check
```

The database goes to `data/ppbp.duckdb`. If the project is in an iCloud-synced folder, keep `data/` off iCloud with `mv data data.nosync && ln -s data.nosync data` (DECISIONS D11), or set `PPBP_DB_PATH` to put the database elsewhere.

## Data and standards

- **Source data:** Synthea's published sample, `synthea_sample_data_fhir_r4_nov2021.zip` [[5]], in FHIR R4 (v4.0.1) [[6]]. `make setup` pins it by SHA-256 (DECISIONS D1).
- **This project's numbers** come from the pipeline's own output, not from external sources. `make ingest` loads 557 bundles (555 patients) holding 409,973 resources. `make cohort` finds 84 patients with a delivery and builds a cohort of 48: 11 with a native HDP diagnosis and 37 with a synthetic, tagged one (DECISIONS D3, D12–D15).

| What | Code | Source |
|---|---|---|
| Blood pressure panel | LOINC 85354-9 | [[7]][[8]] |
| Systolic blood pressure | LOINC 8480-6 | [[7]][[8]] |
| Diastolic blood pressure | LOINC 8462-4 | [[7]][[8]] |
| BP unit | UCUM `mm[Hg]` | [[7]] |
| Pre-eclampsia (native HDP) | SNOMED CT 398254007 | [[9]] |
| Eclampsia in pregnancy (native HDP) | SNOMED CT 198992004 | [[9]] |
| Pregnancy-induced hypertension (synthetic HDP) | SNOMED CT 48194001 | [[9]] |

The three SNOMED CT codes are all members of HL7's *Hypertensive Disorders of Pregnancy SNOMED CT* value set [[9]].

## Blood pressure thresholds

The alert rules in `config/rules.toml` use ACOG's definitions [[10]]:
- **Hypertension in pregnancy:** systolic ≥140 mm Hg and/or diastolic ≥90 mm Hg. Gestational hypertension first appears after 20 weeks of pregnancy.
- **Severe range:** systolic ≥160 mm Hg and/or diastolic ≥110 mm Hg.

A third rule, "no readings for 36+ hours", is this project's own assumption (DECISIONS D25). It has no external source.

Elevated readings raise one alert per patient once there are 2+ readings at least 4 hours apart. The 4 hours follows the ACOG diagnostic criterion of ≥140/90 on two occasions at least 4 hours apart, as summarized in [[13]] and [[14]]. That criterion is for diagnosis during pregnancy; applying it to postpartum home readings is this project's choice. The 2-reading minimum and the alert closing after 48 hours without an elevated reading are assumptions (DECISIONS D26). Severe readings each raise their own alert. On the 48-patient cohort, that change cut alerts from 172 to 60:

| Rule | Alerts before | Alerts after | Patients before | Patients after |
|---|---|---|---|---|
| Severe-range reading (Critical) | 11 | 11 | 6 | 6 |
| Elevated readings (High) | 143 | 31 | 29 | 25 |
| No readings for 36+ hours (Medium) | 18 | 18 | 18 | 18 |
| **Total** | **172** | **60** | **36** | **33** |

The per-patient comparison is in DECISIONS D26.

## Home readings (simulated)

The simulated protocol follows the Heart Safe Motherhood program: readings twice daily for 10 days after discharge [[11]]. That covers ACOG's 7–10 day check [[4]]. The length can be set up to 42 days; a hospital-level program has monitored patients until 6 weeks postpartum [[12]].

The messiness injected into the readings (dropouts, missed readings, duplicates, late arrivals, wrong time zones, impossible values, unit errors) uses assumed rates, recorded in DECISIONS D21. On the 48-patient cohort, `make home` produces 777 submissions. Cleaning accepts 737 and rejects 40, each with a reason. It also checks, line by line, that every injected problem was handled (DECISIONS D22).

## Privacy (synthetic data, handled as if it were real)

- **Study IDs:** outside two restricted zones, patients appear only as a study ID (`SID-...`), a keyed hash of the record number. The key never enters git. The record-number mapping and contact details live in the restricted `phi` schema. The raw FHIR and home-export data live in `raw` (DECISIONS D30, D33).
- **Consent:** alerts for patients without home-monitoring consent are held back from the nurse's list, with the reason recorded per alert. On this cohort that's 6 of 60 alerts (D31).
- **Access:** every read of an alert, timeline or contact detail goes through an access layer. It checks the role's permissions (`config/access.toml`) and logs who read what and when, including denied attempts (D32).
- **Leak check:** `make leak-check` scans every table, log, report, access-layer response and tracked file for record numbers, names, phones, identifiers, addresses and birth dates. It fails if any are found (D33).

## Limitations

- **Held severe alerts.** Alerts for patients without home-monitoring consent are held back from the nurse's list, as R7 requires. That includes severe-range readings: 2 on this cohort, flagged for policy review in the alert report and in the privacy officer's view. A real service would need a safety escalation path for a severe reading from someone who hasn't consented to home monitoring. That's a clinical-governance decision this project doesn't make (DECISIONS D31).
- **Discharge time.** Discharge is the end of Synthea's delivery encounter, about an hour after delivery. Real postpartum stays are longer (D4).
- **Clinical dates.** Clinical dates (delivery, discharge, readings) stay in the analytics and alert tables, because the alert workflow runs on days since discharge. They're documented rather than removed (D30).

## Project documents

- Requirements: [REQUIREMENTS.md](REQUIREMENTS.md)
- Decisions and assumptions: [DECISIONS.md](DECISIONS.md)
- FHIR walkthrough: [docs/phase1_fhir_walkthrough.md](docs/phase1_fhir_walkthrough.md)

## Sources

Every link below was checked on 2026-10-15 to confirm it supports the statement that cites it.

1. <a id="s1"></a>Walonoski J, Kramer M, Nichols J, et al. Synthea: An approach, method, and software mechanism for generating synthetic patients and the synthetic electronic health care record. *J Am Med Inform Assoc.* 25(3):230–238. https://doi.org/10.1093/jamia/ocx079
2. <a id="s2"></a>Synthea source code. https://github.com/synthetichealth/synthea
3. <a id="s3"></a>Ford ND, Cox S, Ko JY, et al. Hypertensive Disorders in Pregnancy and Mortality at Delivery Hospitalization — United States, 2017–2019. *MMWR Morb Mortal Wkly Rep.* 2022;71(17):585–591. https://www.cdc.gov/mmwr/volumes/71/wr/mm7117a1.htm
4. <a id="s4"></a>ACOG Committee Opinion No. 736: Optimizing Postpartum Care. *Obstet Gynecol.* 2018;131(5):e140–e150. https://doi.org/10.1097/AOG.0000000000002633
5. <a id="s5"></a>Synthea sample data, downloads listing (FHIR R4, November 2021). https://github.com/synthetichealth/synthea-sample-data/tree/main/downloads
6. <a id="s6"></a>HL7 FHIR Release 4 (v4.0.1). https://hl7.org/fhir/R4/
7. <a id="s7"></a>HL7 FHIR R4, Observation Blood Pressure profile (fixes LOINC 85354-9, 8480-6, 8462-4 and the `mm[Hg]` unit). https://hl7.org/fhir/R4/bp.html
8. <a id="s8"></a>U.S. National Library of Medicine, Clinical Table Search Service: LOINC lookups for [85354-9](https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search?terms=85354-9&sf=LOINC_NUM&df=LOINC_NUM,LONG_COMMON_NAME), [8480-6](https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search?terms=8480-6&sf=LOINC_NUM&df=LOINC_NUM,LONG_COMMON_NAME), [8462-4](https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search?terms=8462-4&sf=LOINC_NUM&df=LOINC_NUM,LONG_COMMON_NAME).
9. <a id="s9"></a>HL7 FHIR IG *Longitudinal Maternal & Infant Health Information for Research* (STU 1.0.0), ValueSet: Hypertensive Disorders of Pregnancy SNOMED CT. https://hl7.org/fhir/us/mihr/STU1/ValueSet-hypertensive-disorders-pregnancy-SNOMED.html
10. <a id="s10"></a>ACOG. Preeclampsia and High Blood Pressure During Pregnancy (FAQ). https://www.acog.org/womens-health/faqs/preeclampsia-and-high-blood-pressure-during-pregnancy
11. <a id="s11"></a>Janssen MK, Demers S, Srinivas SK, et al. Implementation of a text-based postpartum blood pressure monitoring program at 3 different academic sites. *Am J Obstet Gynecol MFM.* 2021;3(6):100446. https://doi.org/10.1016/j.ajogmf.2021.100446
12. <a id="s12"></a>Hauspurg A, Lemon LS, Quinn BA, et al. A Postpartum Remote Hypertension Monitoring Protocol Implemented at the Hospital Level. *Obstet Gynecol.* 2019;134(4):685–691. https://doi.org/10.1097/AOG.0000000000003479

13. <a id="s13"></a>Khosla K, Heimberger S, Nieman KM, et al. Long-Term Cardiovascular Disease Risk in Women After Hypertensive Disorders of Pregnancy: Recent Advances in Hypertension. *Hypertension.* 2021;78(4):927–935. Defines hypertension during pregnancy as ≥140 and/or ≥90 mm Hg "on 2 occasions at least 4 hours apart", citing ACOG Practice Bulletin 222. https://pmc.ncbi.nlm.nih.gov/articles/PMC8678921/ (doi:10.1161/HYPERTENSIONAHA.121.16506)
14. <a id="s14"></a>Carter EB, Conner SN, Cahill AG, et al. Impact of fetal growth on pregnancy outcomes in women with severe preeclampsia. *Pregnancy Hypertens.* 2017;8:21–25. Table 1 quotes the 2013 ACOG Task Force definition: ≥140 or ≥90 mm Hg "on two occasions at least 4 hours apart". https://pmc.ncbi.nlm.nih.gov/articles/PMC5435450/ (doi:10.1016/j.preghy.2017.02.002)

[1]: #s1
[2]: #s2
[3]: #s3
[4]: #s4
[5]: #s5
[6]: #s6
[7]: #s7
[8]: #s8
[9]: #s9
[10]: #s10
[11]: #s11
[12]: #s12
[13]: #s13
[14]: #s14
