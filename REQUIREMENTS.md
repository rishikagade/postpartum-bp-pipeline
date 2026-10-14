# Requirements

> Synthetic data only. This is a data engineering demonstration, not a clinical tool.

## Problem

Patients with a hypertensive disorder of pregnancy are often asked to take home blood pressure readings after delivery. Severe readings need to reach a clinician quickly. The data arrives from two places that don't agree on format or quality: the EHR (as FHIR R4) and home devices or apps (messy, late, duplicated, sometimes impossible values). Every alert has to be explainable: someone must be able to see exactly which readings and which rule produced it.

## Users and what each needs

### 1. Care nurse reviewing alerts
*Wants to know who needs attention now, and why.*
- A list of open alerts, sorted by severity and then time, using plain labels ("Severe-range reading", not a rule ID alone).
- For each alert: the reading(s) that triggered it, the threshold that was crossed, days since discharge, and recent readings for context.
- A clear signal when a patient has gone quiet (no readings for N hours), kept distinct from "readings are normal".
- No identifying details beyond what's needed to act. The alert screen uses a patient ID, and contact details are a separate, logged lookup.

### 2. Data engineer debugging a load
*Wants to know what came in, what changed, what was rejected, and why.*
- Per load: files and records in, new / changed / unchanged FHIR resources, rejected readings by reason, and duplicates removed.
- Any alert or clean record traced back to the raw FHIR JSON or raw home reading, plus the rule ID and version.
- Re-running a load must be safe (idempotent), and results must be reproducible (fixed seeds, pinned inputs).

### 3. Privacy officer auditing access
*Wants to know who saw which patient's data, and whether the data was allowed to be used.*
- An access log covering every API read of a patient timeline or alert: role, resource, and time.
- Identifying fields stored separately from analytics and alert data, plus evidence that they don't leak into logs, reports, or API responses.
- Readings from patients without home monitoring consent held back from alerting, with the reason recorded.

## Functional requirements (summary)

| # | Requirement | Primary user |
|---|---|---|
| R1 | Ingest FHIR R4 bundles; detect new / changed / unchanged resources; idempotent reloads | Engineer |
| R2 | Clean home readings: UTC timestamps, deduplication, rejection with a reason (nothing silently dropped) | Engineer |
| R3 | Align readings to days since discharge; one longitudinal timeline per patient | Nurse |
| R4 | Config-driven, versioned validation rules; elevated, severe, and stale-data alerts | Nurse |
| R5 | `explain_alert(alert_id)` returns the rule, its version, and the full source records down to raw | Nurse, Engineer |
| R6 | Per-load quality report (JSON + PDF) with a score whose every deduction names a rule result | Engineer, Ops lead |
| R7 | PHI separation, access log, consent gating, leak check | Privacy officer |
| R8 | Read-only API + viewer; "Synthetic data. Not for clinical use." on every screen | All |

## Out of scope

Real patient data, clinical decision-making, notification delivery (paging, SMS), authentication beyond simulated roles, and live FHIR server polling (listed as future work).
