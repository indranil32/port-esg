# ESG System of Record (ports)

A working ESG system of record: schema-validated **data capture**, a **GHG and KPI engine** with
reproducible runs, **reporting and exports**, and **alerting**, behind role-based access with a
four-eyes approval workflow and an append-only audit log.

> **Everything shipped as seed data is synthetic or illustrative**: operational data, emission
> factors, alert thresholds and framework mappings. Replace or review them before using any output
> for real reporting. In demo mode the UI shows a banner saying so.

## Run it

```bash
docker compose up --build          # app on http://localhost:8000, API docs on /docs
```

Demo mode (`SEED_DEMO_DATA=true`, the compose default) loads sample data on first start. Sign in with
any of these (password `Demo#2026`):

| Email | Role |
|---|---|
| esg.admin@adports-demo.local | esg_admin: everything, incl. users, rules, reference data |
| data.owner@adports-demo.local | data_owner: capture, approve, calculate, handle alerts |
| field.operator@adports-demo.local | field_operator: photo + reading capture on the Field capture page; own records only |
| auditor@adports-demo.local | auditor: read-only incl. audit log |
| tenant1@adports-demo.local | tenant: submit own ESG data only |

**For real use** set `SEED_DEMO_DATA=false`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` and a long random
`APP_SECRET`; then load your entity hierarchy, **approved emission factors** and targets (Data capture >
Reference & setup, or CSV). Without emission factors the calculation reports the gaps instead of guessing.

Local development without Docker: load `db/01_schema.sql` then `db/02_schema_ext.sql` into a Postgres 13+
database, `pip install -r requirements.txt`, set `DATABASE_URL`, then `uvicorn app.main:app`.

## Azure deployment and Microsoft sign-in

Single sign-on (Entra ID), Azure Blob evidence storage, Azure PostgreSQL and a Bicep template are in place and
configured entirely through settings, so a personal subscription can be swapped for the company's without code changes.
See [docs/azure-deploy.md](docs/azure-deploy.md). The template is in `infra/` and compiles cleanly with Bicep; it has not
been deployed from this environment (no Azure access here), so expect to fix small things on the first real run.
Sign-in is tested against a simulated identity provider and storage against a fake blob container, not against live Azure.

## What it does

**Capture.** 37 tables across operations, energy, environment, people and safety, governance, third parties.
Three routes in, all validated against the JSON Schemas generated from the data model, saved per record
(one bad row never sinks a batch), idempotent on `(source_system, source_record_id, version)`, and audited:
the web forms (generated from the schemas), CSV upload (with template download), and `POST /api/ingest/{table}`
for systems such as the TOS, ERP, HRMS and SCADA.

```bash
TOKEN=$(curl -s localhost:8000/api/auth/login -H 'content-type: application/json' \
  -d '{"email":"data.owner@adports-demo.local","password":"Demo#2026"}' | jq -r .token)
curl -s localhost:8000/api/ingest/fuel_transaction -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"source_system_code":"ERP","records":[{"source_record_id":"ERP-123","entity_id":"<uuid>","fuel_type":"diesel","litres":420,"ts":"2026-11-01T06:30:00Z"}]}'
```

**Field evidence capture (photo + OCR).** A `field_operator` role works from a phone-friendly *Field capture*
page: choose the meter, take a photo of the display, confirm the number the OCR read, submit. The photo is kept as
evidence with its OCR text and confidence, the camera's capture time (EXIF) or the device clock, the location
(EXIF or browser GPS), and a perceptual hash. Any normal data-entry form also has an optional *Evidence photo*
field for tables that take evidence. The approver sees the photo, the typed value, the OCR value and any flags
side by side. Cumulative register readings are turned into interval consumption (with the meter multiplier and
dial rollover) only when a second person approves, and flow into the Scope 2 calculation as `energy_meter_reading`.

Integrity flags (they never block a capture; they go to the approver and the `Suspect field evidence` alert):
`duplicate_file`, `near_duplicate` (same shot and same number), `wrong_meter` (plate shows another meter's code),
`value_mismatch` (typed value is not what the photo says), `future_timestamp`, `stale_photo`, `clock_mismatch`,
`read_at_mismatch`, `reading_decreased`, `implausible_jump`, `low_ocr_confidence`, `no_reading_found`, and
informational `no_gps`, `timestamp_unverified`, `ocr_unavailable`, `first_reading`, `no_evidence`. One rule is hard:
a register value below the previous approved one cannot be approved unless marked as rollover.

OCR engine (`OCR_PROVIDER`): `rapidocr` (local, default if installed; photos stay on your server), `tesseract`
(local fallback), `azure` (Azure AI Document Intelligence `prebuilt-read`; photos are sent to Azure), or `none`.
`auto` picks Azure only if its endpoint and key are set.

**Approval (four-eyes).** New records start `pending`. A second person validates or rejects (reject needs a
comment). Corrections send a record back to pending, with before/after images in the audit log. Tenant
submissions follow the same path and become `approved`. Rejected records are excluded from calculations.

**Calculation.** `POST /api/calc/run` (or the UI) builds the inventory for a period: activity data x versioned
emission factor, per entity and month, with lineage to the source rows. Every run is kept, so earlier results
reproduce exactly. KPIs are computed in the same run and scored against targets.

| Scope | Source | Notes |
|---|---|---|
| 1 | Fuel transactions; refrigerant top-ups | factor per fuel / refrigerant |
| 2 | Grid electricity on own and shared meters; shore-power electricity purchased | location-based |
| 3 cat 1 | Spend records in AED | spend-based; categories without a factor are reported as gaps |
| 3 cat 9 | Vessel auxiliary engines at berth **without** shore power | kW x load factor (0.4, `VESSEL_AUX_LOAD_FACTOR`) x hours |
| 3 cat 13 | Electricity on tenant meters | |

These mappings are assumptions: confirm them against your reporting policy and boundary.

**Reporting.** Dashboard, GHG inventory (group by activity, category, entity, month, scope), KPI scorecard
(25 metrics, E/S/G), disclosure coverage (GRI and IFRS S2 starter mappings, indicative only), CSV exports, and a
printable annual report (open it, then Print > Save as PDF).

**Alerting.** Seven rule types, evaluated every `ALERT_INTERVAL_SEC` (default 300 s) and on demand:
sensor thresholds, new environmental or safety incidents, silent meters, expiring certifications and policies,
workflow backlogs, KPIs off track, missing monthly data. One live alert per condition (no duplicates), alerts clear
themselves when the condition ends, and acknowledge / resolve is audited. New alerts go to a Teams / Slack-style
webhook (`ALERT_WEBHOOK_URL`, posts `{"text": ...}`) and/or email (SMTP settings). Thresholds seeded here are
illustrative: set yours under Alerts > Rules.

## Layout

```
app/        FastAPI app: capture.py, evidence.py, ocr.py, calc.py, kpis.py, reports.py, alerts.py, auth.py, seed.py, static/ (UI)
db/         01_schema.sql (data model), 02_schema_ext.sql (users, audit log, alerts, evidence_capture),
            migrations/ (001_evidence_ocr.sql: applied automatically at start to databases built before it)
docs/       erd.md (Mermaid ERDs)
tools/      model.py + generate.py: single source for the SQL, ERD and JSON Schemas (app/esg_schemas.json)
tests/      pytest suite (needs DATABASE_URL pointing at a disposable Postgres with both schema files loaded)
```

To change the data model, edit `tools/model.py`, run `python tools/generate.py`, and copy `build/schema.sql`
and `build/esg_schemas.json` into `db/01_schema.sql` and `app/esg_schemas.json`.

**Round 1 of replacing vendor tools.**
- *Source trace* (Source trace page): any reported activity opens to its emission factor, the source records, who
  entered and approved them, and the evidence photos; hand-read meter intervals link back to the register reading.
- *Period lock* (Admin): an administrator locks a calculation run for a reporting period. Database triggers then refuse
  new, changed or deleted data inside the period, recalculation of it, and edits to emission factors the run used.
  Unlocking needs a reason and is audited. Corrections for later years use a new factor with a later `valid_from`.
- *Third-party collection links* (Admin): a private, expiring, single-use link lets a tenant or contractor submit chosen
  metrics without an account, with their last values pre-filled and an optional file. Submissions wait for approval.
  Only a hash of the token is stored; the token travels in a header and the page strips it from the address bar.
- *Audit context*: audit rows now carry the client IP and a request id (also returned as `X-Request-ID`).
  The IP honours `X-Forwarded-For`, so only trust it behind your own proxy.
- *Flat readings*: an unchanged hand-read register value is flagged.
- *Ingest batches* (Admin): every API or CSV load is a batch; a bad file can be withdrawn in one audited step (records are
  rejected, not deleted, and a locked period blocks it).
- *Consolidation approach*: each entity has an equity share (default 100%); the GHG inventory can be viewed as all entities,
  equity share, or operational control (`?approach=`). Annual report still uses all entities.
- *Water footprint* (GRI 303): `water_record` holds withdrawal, discharge and recycled volumes by source and entity. KPIs:
  withdrawal, consumption (withdrawal less discharge) and recycled share, mapped to GRI 303-3 and 303-5.
  Not yet modelled: per-source water-stress classification, quality of discharge against permit limits.
- *Direct-change audit*: a trigger on the data tables logs SQL run outside the app (see limits).
- *Anomaly detection*: alert rule type `data_anomaly` compares recent fuel, meter-interval or water values with their own
  history (median and MAD, modified z-score) and raises an alert showing the usual level. No model or training, so every
  flag is explainable; it needs about 8 earlier records per series. Seeded rules are on by default; tune them under Alerts.
- *Environmental and energy management* (EMS page): ISO 14001 / 50001 objectives linked to KPIs with progress from baseline to
  target, action plans with owners and due dates, internal audit planning (findings are the existing audit findings, linked by
  audit reference), and management reviews that store a snapshot of KPIs, findings and overdue actions. An alert rule flags
  overdue actions and audits. It supports running and evidencing the system; it does not certify it. 
- *Legal and permit register*, *nonconformities* (root cause → corrective actions → verification → closure, major ones closed by a
  second person) and *energy baselines* (monthly consumption regressed on TEU or vessel calls; later months compared with the
  expected level, current month excluded). Overdue items, permits expiring within 60 days and non-compliance raise alerts.
  Still not built: document control, operational inspections, permit limit exceedance checks, significant-energy-user ranking.
- *CSV loading by code*: CSV and API rows take codes (`entity_code`, `meter_code`, `asset_code`, `tenant_name`, `metric_code` ...) instead of UUIDs.
  A downloadable template pack (`/api/templates.zip`: header-only templates, loadable example rows, data dictionary, table index) is on the
  Data capture CSV tab. Admins can also import the metric library, framework mappings and legal register by CSV (Admin page).
  Still one-at-a-time: EMS objectives, actions and audits, evidence documents. Utility bills are stored but not read automatically.
- Migrations are tracked in `esg.schema_migration` and applied at startup.

**Not yet built (planned rounds):** per-framework completeness, AI document classification, data-logger connectors, report templates. Vendor ontology size, patents,
SOC 2 and 24/7 support cannot be matched by code alone.

## Known limits (read before production)

- Auth is email + password with signed tokens. No SSO, MFA, lockout or rate limiting yet; put it behind your
  reverse proxy / identity provider and TLS.
- The application writes detailed audit entries. Direct SQL changes to data tables are also logged by a database trigger
  (actor `db:<user>`, before/after row), except bulk sensor, AIS and calculation-output tables. A database user who
  deliberately sets `esg.via_app = on` bypasses it; restrict who can connect as the owner role. The audit table itself
  rejects edits and deletes.
- High-volume tables (meter, AIS, sensor readings) are plain tables: partition by month at scale.
- Evidence files are stored on local disk (`EVIDENCE_DIR`); use a persistent volume or object storage.
- Alert evaluation runs inside each app process: run one instance, or move `alerts.evaluate_all` to a single scheduler.
- Evidence photos prove a photo was taken, not that the meter was read honestly. Flags raise the cost of
  re-using or mistyping photos; they cannot detect a staged or edited photo. EXIF can be edited on the handset,
  so the capture time is corroborated by the device clock and the upload time rather than trusted alone.
- Browsers give no tamper-proof camera: the file input opens the phone camera on mobile, but a user can still pick
  a gallery image (the stale-photo and duplicate flags are the control). A native app would close that gap.
- OCR is only as good as the photo. It reads LCD and printed digits; analogue dials and odometer rollers need a
  trained model. Always confirm the number by eye; the typed value is what is stored. The Azure provider and the
  Docker OCR libraries were not exercised in the build environment (rapidocr and tesseract were).
- No offline queue: the field page needs a connection when the photo is taken.
- HEIC photos must be converted to JPEG by the handset or browser (iPhones do this for web uploads by default).
- The UI is light-theme only and has had desktop-browser checks, not a full accessibility audit.
- Shore-power electricity is treated as purchased Scope 2; vessel emissions avoided by shore power are not
  yet reported as a separate figure.

## Tests

```bash
DATABASE_URL=postgresql://... pytest -q tests
```
44 tests cover role boundaries, ingestion edge cases, four-eyes, audit immutability, calculation arithmetic
against independent queries, rejected-record exclusion, KPI formulas, exports, alert de-duplication,
auto-resolve, webhook delivery, and rule-parameter injection guards, period locks, collection links, source trace, and the evidence flow
(OCR of a synthetic meter photo, flags, atomic linking, rollover, derived consumption, field-operator limits).
