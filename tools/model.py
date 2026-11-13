"""Single source of truth for the Port ESG data model.

Column DSL:  <name> <type> [opts...]
  types : text int bigint num geo bool ts date json uuid text[]
  opts  : pk  nn  uq  nonneg  identity  fk=table.col  in=a|b|c  def=<sql>  range=lo,hi
"""

COMMON = """source_system_id uuid fk=source_system.source_system_id
source_record_id text
ingested_at ts nn def=now()
data_quality text nn def='{dq}' in=measured|estimated|manual|imputed
validation_status text nn def='pending' in=pending|validated|rejected
version int nn def=1
created_by text
approved_by text"""

LEAN = """source_system_id uuid fk=source_system.source_system_id
ingested_at ts nn def=now()"""


def T(name, dom, desc, cols, common=None, lean=False, cons=(), idx=()):
    return dict(name=name, dom=dom, desc=desc, cols=cols, common=common,
                lean=lean, cons=list(cons), idx=list(idx))


TABLES = [
    # ------------------------------------------------------------------ core
    T("source_system", "core", "Registry of every system or channel that feeds the platform, classified A-H.", """
source_system_id uuid pk
code text nn uq
name text nn
source_class text nn in=A_operations|B_energy|C_environment|D_enterprise|E_third_party|F_reference|G_manual|H_derived
ingestion_mode text in=api|stream|batch|iot|portal|manual
refresh_frequency text
owner text
active bool nn def=true"""),

    T("entity", "core", "Organisational hierarchy: port > terminal > business unit / facility / tenant. Drives reporting boundaries.", """
entity_id uuid pk
parent_entity_id uuid fk=entity.entity_id
entity_type text nn in=port|terminal|business_unit|facility|tenant
code text nn uq
name text nn
operational_control bool nn def=true
equity_share_pct num nn def=100 range=0,100
country_code text
valid_from date
valid_to date"""),

    T("evidence_document", "core", "Uploaded evidence (invoices, certificates, manifests) with content hash for audit.", """
doc_id uuid pk
file_uri text nn
sha256 text
mime_type text
description text
uploaded_by text
uploaded_at ts nn def=now()"""),

    T("tenant", "core", "Tenants, concessionaires and contractors. operational_control decides Scope 1/2 vs Scope 3.", """
tenant_id uuid pk
entity_id uuid nn fk=entity.entity_id
name text nn
tenant_type text in=terminal_operator|logistics|industrial|service|other
contract_ref text
esg_clause bool nn def=false
operational_control bool nn def=false"""),

    # ------------------------------------------------------- A. operations
    T("vessel", "operations", "Vessel master data (IMO registry, class and engine particulars).", """
imo_number text pk
vessel_name text nn
vessel_type text
flag text
gross_tonnage num nonneg
dwt num nonneg
teu_capacity int nonneg
built_year int
engine_type text
main_engine_kw num nonneg
aux_engine_kw num nonneg
default_fuel_type text
esi_score num
cii_rating text in=A|B|C|D|E"""),

    T("berth", "operations", "Berths, with shore-power capability.", """
berth_id uuid pk
terminal_id uuid nn fk=entity.entity_id
berth_code text nn
length_m num nonneg
shore_power_capable bool nn def=false""", cons=["UNIQUE (terminal_id, berth_code)"]),

    T("vessel_call", "operations", "One row per vessel call; the anchor for Scope 3 vessel emissions (TOS / PCS / berth planning).", """
call_id uuid pk
imo_number text nn fk=vessel.imo_number
berth_id uuid fk=berth.berth_id
terminal_id uuid fk=entity.entity_id
voyage_ref text
eta ts
ata ts
atb ts
atd ts
hours_at_anchor num nonneg
hours_at_berth num nonneg
hours_manoeuvring num nonneg
shore_power_connected bool nn def=false
fuel_type_at_berth text
cargo_tonnes num nonneg
teu_moved int nonneg""", common="measured", idx=[["imo_number", "atb"]]),

    T("asset", "operations", "Port-owned equipment: cranes, RTGs, trucks, generators, buildings.", """
asset_id uuid pk
entity_id uuid nn fk=entity.entity_id
asset_code text nn uq
asset_type text nn in=crane|rtg|truck|reach_stacker|vessel_owned|building|generator|other
fuel_type text
electrified bool nn def=false
rated_kw num nonneg
commissioned_date date
decommissioned_date date
status text nn def='active' in=active|standby|retired"""),

    T("ais_position", "operations", "AIS position reports for approach and manoeuvring emission estimates. High volume: partition by month.", """
position_id bigint pk identity
imo_number text nn fk=vessel.imo_number
ts ts nn
lat geo range=-90,90
lon geo range=-180,180
speed_knots num nonneg
heading num range=0,360
nav_status text""", lean=True, idx=[["imo_number", "ts"]]),

    T("container_move", "operations", "Container / cargo moves from the TOS, linked to equipment for per-asset energy intensity.", """
move_id uuid pk
call_id uuid fk=vessel_call.call_id
equipment_id uuid fk=asset.asset_id
container_ref text
move_type text nn in=load|discharge|restow|gate_in|gate_out
ts ts nn
teu num nonneg
weight_kg num nonneg""", common="measured", idx=[["call_id", "ts"]]),

    T("gate_transaction", "operations", "Truck gate events; basis for turnaround time and idling emissions.", """
txn_id uuid pk
terminal_id uuid nn fk=entity.entity_id
truck_ref text
haulier_tenant_id uuid fk=tenant.tenant_id
gate_in ts nn
gate_out ts
turnaround_min num nonneg
container_ref text
truck_engine_class text""", common="measured", idx=[["terminal_id", "gate_in"]]),

    # ---------------------------------------------------------- B. energy
    T("meter", "energy", "Meter registry. scope_boundary says whose consumption it is.", """
meter_id uuid pk
meter_code text nn uq
entity_id uuid nn fk=entity.entity_id
asset_id uuid fk=asset.asset_id
tenant_id uuid fk=tenant.tenant_id
meter_type text nn in=electricity|fuel|water|gas|shore_power
location_desc text
scope_boundary text nn in=own|tenant|shared
uom text nn
energy_type text nn def='grid' in=grid|solar|diesel_gen|shore_power|other
multiplier num nn def=1 nonneg
register_digits int"""),

    T("meter_register_reading", "energy", "Cumulative register (odometer) readings keyed in or read from a photo by field operators. Consumption since the previous validated reading is derived into energy_meter_reading once approved.", """
register_reading_id uuid pk
meter_id uuid nn fk=meter.meter_id
read_at ts nn
register_value num nn nonneg
uom text nn
evidence_doc_id uuid fk=evidence_document.doc_id
rollover bool nn def=false
consumption_raw num nonneg
derived_reading_id bigint
note text""", common="manual", cons=["UNIQUE (meter_id, read_at)"], idx=[["meter_id", "read_at"]]),

    T("energy_meter_reading", "energy", "Interval readings from smart meters / SCADA. High volume: partition by month or use a time-series extension.", """
reading_id bigint pk identity
meter_id uuid nn fk=meter.meter_id
ts ts nn
energy_kwh num nn nonneg
energy_type text nn in=grid|solar|diesel_gen|shore_power|other
data_quality text nn def='measured' in=measured|estimated|manual|imputed""",
      lean=True, cons=["UNIQUE (meter_id, ts, energy_type)"]),

    T("fuel_transaction", "energy", "Fuel dispensed to assets (Scope 1 mobile and stationary combustion).", """
txn_id uuid pk
asset_id uuid fk=asset.asset_id
entity_id uuid nn fk=entity.entity_id
fuel_type text nn
litres num nn nonneg
ts ts nn
dispensing_point text
cost num nonneg
currency text""", common="measured", idx=[["entity_id", "ts"]]),

    T("shore_power_session", "energy", "Shore-power (cold ironing) sessions per vessel call.", """
session_id uuid pk
call_id uuid nn fk=vessel_call.call_id
berth_id uuid fk=berth.berth_id
start_ts ts nn
end_ts ts
kwh_delivered num nonneg
peak_kw num nonneg""", common="measured"),

    T("refrigerant_event", "energy", "Refrigerant top-ups from maintenance work orders (CMMS); basis for fugitive emissions.", """
event_id uuid pk
asset_id uuid fk=asset.asset_id
work_order_ref text
refrigerant_type text nn
kg_added num nn nonneg
event_date date nn""", common="measured"),

    # ------------------------------------------------------ C. environment
    T("sensor", "environment", "Environmental sensor registry (air, water, noise, weather).", """
sensor_id uuid pk
sensor_code text nn uq
sensor_type text nn in=air|water|noise|weather
station_id text
entity_id uuid fk=entity.entity_id
lat geo range=-90,90
lon geo range=-180,180
calibration_date date
owner text
active bool nn def=true"""),

    T("air_quality_reading", "environment", "Air quality readings in ug/m3 (CO in mg/m3).", """
reading_id bigint pk identity
sensor_id uuid nn fk=sensor.sensor_id
ts ts nn
pm25 num nonneg
pm10 num nonneg
no2 num nonneg
so2 num nonneg
co num nonneg
o3 num nonneg
quality_flag text""", lean=True, cons=["UNIQUE (sensor_id, ts)"]),

    T("water_quality_reading", "environment", "Harbour / discharge water quality readings.", """
reading_id bigint pk identity
sensor_id uuid nn fk=sensor.sensor_id
ts ts nn
ph num range=0,14
turbidity_ntu num nonneg
dissolved_oxygen_mgl num nonneg
temperature_c num
hydrocarbon_ppm num nonneg
quality_flag text""", lean=True, cons=["UNIQUE (sensor_id, ts)"]),

    T("noise_reading", "environment", "Noise level readings in dB.", """
reading_id bigint pk identity
sensor_id uuid nn fk=sensor.sensor_id
ts ts nn
leq_db num
lmax_db num
quality_flag text""", lean=True, cons=["UNIQUE (sensor_id, ts)"]),

    T("weather_observation", "environment", "Weather and tide observations used to normalise and contextualise readings.", """
reading_id bigint pk identity
sensor_id uuid nn fk=sensor.sensor_id
ts ts nn
temp_c num
wind_speed_ms num nonneg
wind_dir_deg num range=0,360
humidity_pct num range=0,100
tide_level_m num""", lean=True, cons=["UNIQUE (sensor_id, ts)"]),

    T("environmental_incident", "environment", "Spills, exceedances and odour events.", """
incident_id uuid pk
entity_id uuid nn fk=entity.entity_id
incident_type text nn in=spill|exceedance|odour|other
occurred_at ts nn
location_desc text
quantity num nonneg
quantity_uom text
severity text in=low|medium|high|critical
root_cause text
status text nn def='open' in=open|investigating|closed
regulator_notified bool nn def=false""", common="manual"),

    # ------------------------------------------------------- D. enterprise
    T("spend_record", "enterprise", "Procurement spend (ERP) used for Scope 3 purchased goods and services.", """
spend_id uuid pk
entity_id uuid fk=entity.entity_id
po_ref text
supplier_ref text
supplier_name text
category text nn
material_code text
quantity num nonneg
uom text
amount num nn
currency text nn
spend_date date nn
scope3_category int range=1,15""", common="measured"),

    T("utility_invoice", "enterprise", "Utility invoices; cross-check against meter data.", """
invoice_id uuid pk
entity_id uuid nn fk=entity.entity_id
utility_type text nn in=electricity|water|gas|fuel
consumption num nonneg
uom text
period_start date nn
period_end date nn
amount num
currency text""", common="measured"),

    T("employee_snapshot", "enterprise", "Aggregated HR snapshot per period. Aggregates only: no individual-level personal data.", """
snapshot_id uuid pk
entity_id uuid nn fk=entity.entity_id
period_end date nn
department text
gender text
nationality_group text
age_band text
grade_band text
contract_type text
headcount int nn nonneg
leavers int nonneg
voluntary_leavers int nonneg""", common="measured"),

    T("training_record", "enterprise", "Aggregated training hours per period and category.", """
training_id uuid pk
entity_id uuid nn fk=entity.entity_id
period_start date nn
period_end date nn
course_category text nn
training_hours num nn nonneg
participants int nonneg""", common="measured"),

    T("hours_worked", "enterprise", "Exposure hours: the denominator for LTIFR / TRIR.", """
hours_id uuid pk
entity_id uuid nn fk=entity.entity_id
period_start date nn
period_end date nn
employee_hours num nn nonneg
contractor_hours num nonneg""", common="measured",
      cons=["UNIQUE (entity_id, period_start, period_end, version)"]),

    T("safety_incident", "enterprise", "Safety incidents and near-misses from the HSE system.", """
incident_id uuid pk
entity_id uuid nn fk=entity.entity_id
incident_type text nn in=lost_time|recordable|first_aid|fatality|near_miss|dangerous_occurrence
person_type text nn in=employee|contractor|visitor
occurred_at ts nn
lost_days int nonneg
severity text
injury_class text
location_desc text
root_cause text
status text nn def='open' in=open|investigating|closed""", common="measured", idx=[["entity_id", "occurred_at"]]),

    T("risk", "enterprise", "ESG risk register (GRC).", """
risk_id uuid pk
risk_code text nn uq
entity_id uuid fk=entity.entity_id
category text nn in=E|S|G|operational|climate
title text nn
likelihood int range=1,5
impact int range=1,5
owner text
mitigation text
status text nn def='open' in=open|mitigating|accepted|closed""", common="manual"),

    T("policy", "enterprise", "Governance policies and their review cycle.", """
policy_id uuid pk
title text nn
version_label text
approval_date date
next_review_date date
owner_role text
doc_id uuid fk=evidence_document.doc_id""", common="manual"),

    T("audit_finding", "enterprise", "Internal / external audit findings.", """
finding_id uuid pk
entity_id uuid fk=entity.entity_id
audit_ref text
severity text in=low|medium|high
description text
due_date date
status text nn def='open' in=open|in_progress|closed
doc_id uuid fk=evidence_document.doc_id""", common="manual"),

    T("certification", "enterprise", "Management-system certifications (ISO 14001, ISO 45001, EcoPorts, etc.).", """
certification_id uuid pk
entity_id uuid nn fk=entity.entity_id
standard text nn
issue_date date
expiry_date date
result text
doc_id uuid fk=evidence_document.doc_id""", common="manual"),

    # ---------------------------------------------------------- reference
    T("framework", "reference", "Reporting frameworks (GRI, IFRS S1/S2, TCFD, SASB, CDP, local regulator).", """
framework_id uuid pk
code text nn uq
name text nn
version_label text"""),

    T("framework_requirement", "reference", "Individual disclosures within a framework.", """
requirement_id uuid pk
framework_id uuid nn fk=framework.framework_id
disclosure_code text nn
title text
description text""", cons=["UNIQUE (framework_id, disclosure_code)"]),

    T("metric", "reference", "Metric catalog: links raw data to KPIs and disclosures.", """
metric_id uuid pk
metric_code text nn uq
name text nn
pillar text nn in=E|S|G
unit text
formula_ref text
owner text
frequency text in=realtime|daily|monthly|quarterly|annual
source_classes text[]"""),

    T("metric_requirement_map", "reference", "Many-to-many mapping of metrics to framework disclosures.", """
metric_id uuid pk fk=metric.metric_id
requirement_id uuid pk fk=framework_requirement.requirement_id"""),

    T("emission_factor", "reference", "Versioned emission factors. Never overwrite: add a new row with a new validity window.", """
factor_id uuid pk
source text nn
activity_type text nn
fuel_or_grid text
gas text nn in=CO2|CH4|N2O|CO2e|HFC|SF6
value num nn nonneg
uom text nn
gwp_basis text in=AR4|AR5|AR6
region text
valid_from date nn
valid_to date""", idx=[["activity_type", "fuel_or_grid", "valid_from"]]),

    T("climate_hazard", "reference", "Physical climate hazard projections per location / scenario.", """
hazard_id uuid pk
entity_id uuid fk=entity.entity_id
hazard_type text nn
scenario text nn
horizon_year int nn
value num
uom text
source text"""),

    # ------------------------------------------ E. third party / G. manual
    T("tenant_esg_submission", "thirdparty_manual", "Metric values submitted by tenants and contractors, with review workflow.", """
submission_id uuid pk
tenant_id uuid nn fk=tenant.tenant_id
metric_id uuid nn fk=metric.metric_id
period_start date nn
period_end date nn
value num nn
uom text
evidence_doc_id uuid fk=evidence_document.doc_id
status text nn def='draft' in=draft|submitted|approved|rejected
reviewer text
review_date date""", common="manual"),

    T("shipping_line_data", "thirdparty_manual", "Voluntary shipping-line disclosures: berth fuel use, ETS and CII.", """
record_id uuid pk
line_name text nn
imo_number text fk=vessel.imo_number
period_start date nn
period_end date nn
fuel_consumed_at_berth_t num nonneg
ets_certificate_ref text
cii_rating text in=A|B|C|D|E""", common="manual"),

    T("waste_manifest", "thirdparty_manual", "Waste transfer manifests.", """
manifest_id uuid pk
entity_id uuid nn fk=entity.entity_id
manifest_ref text
waste_type text nn
hazardous bool nn def=false
quantity_kg num nn nonneg
disposal_method text in=recycle|recover|landfill|incineration|treatment|other
contractor text
manifest_date date nn
doc_id uuid fk=evidence_document.doc_id""", common="manual"),

    T("community_grievance", "thirdparty_manual", "Community and stakeholder grievances.", """
grievance_id uuid pk
entity_id uuid fk=entity.entity_id
received_date date nn
channel text
category text
status text nn def='open' in=open|investigating|resolved|closed
resolution_days int nonneg""", common="manual"),

    T("survey_response", "thirdparty_manual", "Stakeholder survey scores used in double-materiality assessment.", """
response_id uuid pk
survey_ref text nn
stakeholder_group text nn
topic text nn
score num
response_date date""", common="manual"),

    # ------------------------------------------------------- H. derived
    T("calc_run", "derived", "A calculation run: pins the factor-set version so results are reproducible.", """
run_id uuid pk
period_start date nn
period_end date nn
factor_set_version text
triggered_by text
started_at ts nn def=now()
finished_at ts
status text nn def='running' in=running|succeeded|failed
notes text"""),

    T("kpi_target", "derived", "Baselines and targets per metric and entity.", """
target_id uuid pk
metric_id uuid nn fk=metric.metric_id
entity_id uuid nn fk=entity.entity_id
baseline_year int
baseline_value num
target_year int nn
target_value num nn
description text
status text nn def='active' in=active|achieved|superseded"""),

    T("ghg_activity_data", "derived", "Normalised activity data feeding the GHG inventory, with lineage back to source rows.", """
activity_id uuid pk
run_id uuid nn fk=calc_run.run_id
entity_id uuid nn fk=entity.entity_id
scope int nn range=1,3
scope3_category int range=1,15
activity_type text nn
quantity num nn
uom text nn
period_start date nn
period_end date nn
data_quality text nn def='measured' in=measured|estimated|manual|imputed
source_table text
source_record_ids text[]""", idx=[["entity_id", "period_start"]]),

    T("ghg_emission", "derived", "Calculated emissions per activity and gas.", """
emission_id uuid pk
activity_id uuid nn fk=ghg_activity_data.activity_id
factor_id uuid nn fk=emission_factor.factor_id
run_id uuid nn fk=calc_run.run_id
gas text nn in=CO2|CH4|N2O|CO2e|HFC|SF6
co2e_tonnes num nn
method text
uncertainty_pct num nonneg"""),

    T("kpi_value", "derived", "Computed KPI values per metric, entity and period, with status versus target.", """
kpi_value_id uuid pk
metric_id uuid nn fk=metric.metric_id
entity_id uuid nn fk=entity.entity_id
period_start date nn
period_end date nn
value num nn
target_id uuid fk=kpi_target.target_id
status text in=on_track|at_risk|off_track|no_target
run_id uuid fk=calc_run.run_id""",
      cons=["UNIQUE (metric_id, entity_id, period_start, period_end, run_id)"]),
]

DOMAINS = [
    ("core", "Core: source registry, organisation, tenants, evidence"),
    ("operations", "A. Port operations: vessels, calls, cargo, gate"),
    ("energy", "B. Energy and utilities"),
    ("environment", "C. Environmental monitoring"),
    ("enterprise", "D. Enterprise systems: HR, HSE, ERP, GRC"),
    ("reference", "F. Reference: frameworks, metrics, emission factors"),
    ("thirdparty_manual", "E and G. Third-party submissions and manual records"),
    ("derived", "H. Derived: GHG inventory and KPIs"),
]
