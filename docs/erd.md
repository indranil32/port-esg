# Port ESG data model: entity relationship diagrams

49 tables, split into 8 domain diagrams plus a relationship overview. Generated from the same definition as `schema.sql` and the JSON Schemas.

Legend: `PK` primary key, `FK` foreign key, `UK` unique. A dotted-circle end (`|o`) means the foreign key is optional.

Every fact table in classes A, D, E and G also carries the common metadata columns (`source_system_id`, `source_record_id`, `ingested_at`, `data_quality`, `validation_status`, `version`, `created_by`, `approved_by`). To keep the diagrams readable they are omitted here (including the link to `source_system`); see `schema.sql` for the full definitions.

## Overview (relationships only)

```mermaid
erDiagram
    entity |o--o{ entity : "parent_entity_id"
    entity ||--o{ tenant : "entity_id"
    entity ||--o{ berth : "terminal_id"
    vessel ||--o{ vessel_call : "imo_number"
    berth |o--o{ vessel_call : "berth_id"
    entity |o--o{ vessel_call : "terminal_id"
    entity ||--o{ asset : "entity_id"
    vessel ||--o{ ais_position : "imo_number"
    vessel_call |o--o{ container_move : "call_id"
    asset |o--o{ container_move : "equipment_id"
    entity ||--o{ gate_transaction : "terminal_id"
    tenant |o--o{ gate_transaction : "haulier_tenant_id"
    entity ||--o{ meter : "entity_id"
    asset |o--o{ meter : "asset_id"
    tenant |o--o{ meter : "tenant_id"
    meter ||--o{ meter_register_reading : "meter_id"
    evidence_document |o--o{ meter_register_reading : "evidence_doc_id"
    meter ||--o{ energy_meter_reading : "meter_id"
    asset |o--o{ fuel_transaction : "asset_id"
    entity ||--o{ fuel_transaction : "entity_id"
    vessel_call ||--o{ shore_power_session : "call_id"
    berth |o--o{ shore_power_session : "berth_id"
    asset |o--o{ refrigerant_event : "asset_id"
    entity |o--o{ sensor : "entity_id"
    sensor ||--o{ air_quality_reading : "sensor_id"
    sensor ||--o{ water_quality_reading : "sensor_id"
    sensor ||--o{ noise_reading : "sensor_id"
    sensor ||--o{ weather_observation : "sensor_id"
    entity ||--o{ environmental_incident : "entity_id"
    entity |o--o{ spend_record : "entity_id"
    entity ||--o{ utility_invoice : "entity_id"
    entity ||--o{ employee_snapshot : "entity_id"
    entity ||--o{ training_record : "entity_id"
    entity ||--o{ hours_worked : "entity_id"
    entity ||--o{ safety_incident : "entity_id"
    entity |o--o{ risk : "entity_id"
    evidence_document |o--o{ policy : "doc_id"
    entity |o--o{ audit_finding : "entity_id"
    evidence_document |o--o{ audit_finding : "doc_id"
    entity ||--o{ certification : "entity_id"
    evidence_document |o--o{ certification : "doc_id"
    framework ||--o{ framework_requirement : "framework_id"
    metric ||--o{ metric_requirement_map : "metric_id"
    framework_requirement ||--o{ metric_requirement_map : "requirement_id"
    entity |o--o{ climate_hazard : "entity_id"
    tenant ||--o{ tenant_esg_submission : "tenant_id"
    metric ||--o{ tenant_esg_submission : "metric_id"
    evidence_document |o--o{ tenant_esg_submission : "evidence_doc_id"
    vessel |o--o{ shipping_line_data : "imo_number"
    entity ||--o{ waste_manifest : "entity_id"
    evidence_document |o--o{ waste_manifest : "doc_id"
    entity |o--o{ community_grievance : "entity_id"
    metric ||--o{ kpi_target : "metric_id"
    entity ||--o{ kpi_target : "entity_id"
    calc_run ||--o{ ghg_activity_data : "run_id"
    entity ||--o{ ghg_activity_data : "entity_id"
    ghg_activity_data ||--o{ ghg_emission : "activity_id"
    emission_factor ||--o{ ghg_emission : "factor_id"
    calc_run ||--o{ ghg_emission : "run_id"
    metric ||--o{ kpi_value : "metric_id"
    entity ||--o{ kpi_value : "entity_id"
    kpi_target |o--o{ kpi_value : "target_id"
    calc_run |o--o{ kpi_value : "run_id"
```

## Core: source registry, organisation, tenants, evidence

```mermaid
erDiagram
    source_system {
        string source_system_id PK
        string code UK
        string name
        string source_class
        string ingestion_mode
        string refresh_frequency
        string owner
        boolean active
    }
    entity {
        string entity_id PK
        string parent_entity_id FK
        string entity_type
        string code UK
        string name
        boolean operational_control
        string country_code
        date valid_from
        date valid_to
    }
    evidence_document {
        string doc_id PK
        string file_uri
        string sha256
        string mime_type
        string description
        string uploaded_by
        datetime uploaded_at
    }
    tenant {
        string tenant_id PK
        string entity_id FK
        string name
        string tenant_type
        string contract_ref
        boolean esg_clause
        boolean operational_control
    }
    entity |o--o{ entity : "parent_entity_id"
    entity ||--o{ tenant : "entity_id"
```

## A. Port operations: vessels, calls, cargo, gate

```mermaid
erDiagram
    vessel {
        string imo_number PK
        string vessel_name
        string vessel_type
        string flag
        float gross_tonnage
        float dwt
        int teu_capacity
        int built_year
        string engine_type
        float main_engine_kw
        float aux_engine_kw
        string default_fuel_type
        float esi_score
        string cii_rating
    }
    berth {
        string berth_id PK
        string terminal_id FK
        string berth_code
        float length_m
        boolean shore_power_capable
    }
    vessel_call {
        string call_id PK
        string imo_number FK
        string berth_id FK
        string terminal_id FK
        string voyage_ref
        datetime eta
        datetime ata
        datetime atb
        datetime atd
        float hours_at_anchor
        float hours_at_berth
        float hours_manoeuvring
        boolean shore_power_connected
        string fuel_type_at_berth
        float cargo_tonnes
        int teu_moved
    }
    asset {
        string asset_id PK
        string entity_id FK
        string asset_code UK
        string asset_type
        string fuel_type
        boolean electrified
        float rated_kw
        date commissioned_date
        date decommissioned_date
        string status
    }
    ais_position {
        int position_id PK
        string imo_number FK
        datetime ts
        float lat
        float lon
        float speed_knots
        float heading
        string nav_status
    }
    container_move {
        string move_id PK
        string call_id FK
        string equipment_id FK
        string container_ref
        string move_type
        datetime ts
        float teu
        float weight_kg
    }
    gate_transaction {
        string txn_id PK
        string terminal_id FK
        string truck_ref
        string haulier_tenant_id FK
        datetime gate_in
        datetime gate_out
        float turnaround_min
        string container_ref
        string truck_engine_class
    }
    entity {
        string entity_id PK
    }
    tenant {
        string tenant_id PK
    }
    entity ||--o{ berth : "terminal_id"
    vessel ||--o{ vessel_call : "imo_number"
    berth |o--o{ vessel_call : "berth_id"
    entity |o--o{ vessel_call : "terminal_id"
    entity ||--o{ asset : "entity_id"
    vessel ||--o{ ais_position : "imo_number"
    vessel_call |o--o{ container_move : "call_id"
    asset |o--o{ container_move : "equipment_id"
    entity ||--o{ gate_transaction : "terminal_id"
    tenant |o--o{ gate_transaction : "haulier_tenant_id"
```

## B. Energy and utilities

```mermaid
erDiagram
    meter {
        string meter_id PK
        string meter_code UK
        string entity_id FK
        string asset_id FK
        string tenant_id FK
        string meter_type
        string location_desc
        string scope_boundary
        string uom
        string energy_type
        float multiplier
        int register_digits
    }
    meter_register_reading {
        string register_reading_id PK
        string meter_id FK
        datetime read_at
        float register_value
        string uom
        string evidence_doc_id FK
        boolean rollover
        float consumption_raw
        int derived_reading_id
        string note
    }
    energy_meter_reading {
        int reading_id PK
        string meter_id FK
        datetime ts
        float energy_kwh
        string energy_type
    }
    fuel_transaction {
        string txn_id PK
        string asset_id FK
        string entity_id FK
        string fuel_type
        float litres
        datetime ts
        string dispensing_point
        float cost
        string currency
    }
    shore_power_session {
        string session_id PK
        string call_id FK
        string berth_id FK
        datetime start_ts
        datetime end_ts
        float kwh_delivered
        float peak_kw
    }
    refrigerant_event {
        string event_id PK
        string asset_id FK
        string work_order_ref
        string refrigerant_type
        float kg_added
        date event_date
    }
    entity {
        string entity_id PK
    }
    asset {
        string asset_id PK
    }
    tenant {
        string tenant_id PK
    }
    evidence_document {
        string doc_id PK
    }
    vessel_call {
        string call_id PK
    }
    berth {
        string berth_id PK
    }
    entity ||--o{ meter : "entity_id"
    asset |o--o{ meter : "asset_id"
    tenant |o--o{ meter : "tenant_id"
    meter ||--o{ meter_register_reading : "meter_id"
    evidence_document |o--o{ meter_register_reading : "evidence_doc_id"
    meter ||--o{ energy_meter_reading : "meter_id"
    asset |o--o{ fuel_transaction : "asset_id"
    entity ||--o{ fuel_transaction : "entity_id"
    vessel_call ||--o{ shore_power_session : "call_id"
    berth |o--o{ shore_power_session : "berth_id"
    asset |o--o{ refrigerant_event : "asset_id"
```

## C. Environmental monitoring

```mermaid
erDiagram
    sensor {
        string sensor_id PK
        string sensor_code UK
        string sensor_type
        string station_id
        string entity_id FK
        float lat
        float lon
        date calibration_date
        string owner
        boolean active
    }
    air_quality_reading {
        int reading_id PK
        string sensor_id FK
        datetime ts
        float pm25
        float pm10
        float no2
        float so2
        float co
        float o3
        string quality_flag
    }
    water_quality_reading {
        int reading_id PK
        string sensor_id FK
        datetime ts
        float ph
        float turbidity_ntu
        float dissolved_oxygen_mgl
        float temperature_c
        float hydrocarbon_ppm
        string quality_flag
    }
    noise_reading {
        int reading_id PK
        string sensor_id FK
        datetime ts
        float leq_db
        float lmax_db
        string quality_flag
    }
    weather_observation {
        int reading_id PK
        string sensor_id FK
        datetime ts
        float temp_c
        float wind_speed_ms
        float wind_dir_deg
        float humidity_pct
        float tide_level_m
    }
    environmental_incident {
        string incident_id PK
        string entity_id FK
        string incident_type
        datetime occurred_at
        string location_desc
        float quantity
        string quantity_uom
        string severity
        string root_cause
        string status
        boolean regulator_notified
    }
    entity {
        string entity_id PK
    }
    entity |o--o{ sensor : "entity_id"
    sensor ||--o{ air_quality_reading : "sensor_id"
    sensor ||--o{ water_quality_reading : "sensor_id"
    sensor ||--o{ noise_reading : "sensor_id"
    sensor ||--o{ weather_observation : "sensor_id"
    entity ||--o{ environmental_incident : "entity_id"
```

## D. Enterprise systems: HR, HSE, ERP, GRC

```mermaid
erDiagram
    spend_record {
        string spend_id PK
        string entity_id FK
        string po_ref
        string supplier_ref
        string supplier_name
        string category
        string material_code
        float quantity
        string uom
        float amount
        string currency
        date spend_date
        int scope3_category
    }
    utility_invoice {
        string invoice_id PK
        string entity_id FK
        string utility_type
        float consumption
        string uom
        date period_start
        date period_end
        float amount
        string currency
    }
    employee_snapshot {
        string snapshot_id PK
        string entity_id FK
        date period_end
        string department
        string gender
        string nationality_group
        string age_band
        string grade_band
        string contract_type
        int headcount
        int leavers
        int voluntary_leavers
    }
    training_record {
        string training_id PK
        string entity_id FK
        date period_start
        date period_end
        string course_category
        float training_hours
        int participants
    }
    hours_worked {
        string hours_id PK
        string entity_id FK
        date period_start
        date period_end
        float employee_hours
        float contractor_hours
    }
    safety_incident {
        string incident_id PK
        string entity_id FK
        string incident_type
        string person_type
        datetime occurred_at
        int lost_days
        string severity
        string injury_class
        string location_desc
        string root_cause
        string status
    }
    risk {
        string risk_id PK
        string risk_code UK
        string entity_id FK
        string category
        string title
        int likelihood
        int impact
        string owner
        string mitigation
        string status
    }
    policy {
        string policy_id PK
        string title
        string version_label
        date approval_date
        date next_review_date
        string owner_role
        string doc_id FK
    }
    audit_finding {
        string finding_id PK
        string entity_id FK
        string audit_ref
        string severity
        string description
        date due_date
        string status
        string doc_id FK
    }
    certification {
        string certification_id PK
        string entity_id FK
        string standard
        date issue_date
        date expiry_date
        string result
        string doc_id FK
    }
    entity {
        string entity_id PK
    }
    evidence_document {
        string doc_id PK
    }
    entity |o--o{ spend_record : "entity_id"
    entity ||--o{ utility_invoice : "entity_id"
    entity ||--o{ employee_snapshot : "entity_id"
    entity ||--o{ training_record : "entity_id"
    entity ||--o{ hours_worked : "entity_id"
    entity ||--o{ safety_incident : "entity_id"
    entity |o--o{ risk : "entity_id"
    evidence_document |o--o{ policy : "doc_id"
    entity |o--o{ audit_finding : "entity_id"
    evidence_document |o--o{ audit_finding : "doc_id"
    entity ||--o{ certification : "entity_id"
    evidence_document |o--o{ certification : "doc_id"
```

## F. Reference: frameworks, metrics, emission factors

```mermaid
erDiagram
    framework {
        string framework_id PK
        string code UK
        string name
        string version_label
    }
    framework_requirement {
        string requirement_id PK
        string framework_id FK
        string disclosure_code
        string title
        string description
    }
    metric {
        string metric_id PK
        string metric_code UK
        string name
        string pillar
        string unit
        string formula_ref
        string owner
        string frequency
        string source_classes
    }
    metric_requirement_map {
        string metric_id PK, FK
        string requirement_id PK, FK
    }
    emission_factor {
        string factor_id PK
        string source
        string activity_type
        string fuel_or_grid
        string gas
        float value
        string uom
        string gwp_basis
        string region
        date valid_from
        date valid_to
    }
    climate_hazard {
        string hazard_id PK
        string entity_id FK
        string hazard_type
        string scenario
        int horizon_year
        float value
        string uom
        string source
    }
    entity {
        string entity_id PK
    }
    framework ||--o{ framework_requirement : "framework_id"
    metric ||--o{ metric_requirement_map : "metric_id"
    framework_requirement ||--o{ metric_requirement_map : "requirement_id"
    entity |o--o{ climate_hazard : "entity_id"
```

## E and G. Third-party submissions and manual records

```mermaid
erDiagram
    tenant_esg_submission {
        string submission_id PK
        string tenant_id FK
        string metric_id FK
        date period_start
        date period_end
        float value
        string uom
        string evidence_doc_id FK
        string status
        string reviewer
        date review_date
    }
    shipping_line_data {
        string record_id PK
        string line_name
        string imo_number FK
        date period_start
        date period_end
        float fuel_consumed_at_berth_t
        string ets_certificate_ref
        string cii_rating
    }
    waste_manifest {
        string manifest_id PK
        string entity_id FK
        string manifest_ref
        string waste_type
        boolean hazardous
        float quantity_kg
        string disposal_method
        string contractor
        date manifest_date
        string doc_id FK
    }
    community_grievance {
        string grievance_id PK
        string entity_id FK
        date received_date
        string channel
        string category
        string status
        int resolution_days
    }
    survey_response {
        string response_id PK
        string survey_ref
        string stakeholder_group
        string topic
        float score
        date response_date
    }
    tenant {
        string tenant_id PK
    }
    metric {
        string metric_id PK
    }
    evidence_document {
        string doc_id PK
    }
    vessel {
        string imo_number PK
    }
    entity {
        string entity_id PK
    }
    tenant ||--o{ tenant_esg_submission : "tenant_id"
    metric ||--o{ tenant_esg_submission : "metric_id"
    evidence_document |o--o{ tenant_esg_submission : "evidence_doc_id"
    vessel |o--o{ shipping_line_data : "imo_number"
    entity ||--o{ waste_manifest : "entity_id"
    evidence_document |o--o{ waste_manifest : "doc_id"
    entity |o--o{ community_grievance : "entity_id"
```

## H. Derived: GHG inventory and KPIs

```mermaid
erDiagram
    calc_run {
        string run_id PK
        date period_start
        date period_end
        string factor_set_version
        string triggered_by
        datetime started_at
        datetime finished_at
        string status
        string notes
    }
    kpi_target {
        string target_id PK
        string metric_id FK
        string entity_id FK
        int baseline_year
        float baseline_value
        int target_year
        float target_value
        string description
        string status
    }
    ghg_activity_data {
        string activity_id PK
        string run_id FK
        string entity_id FK
        int scope
        int scope3_category
        string activity_type
        float quantity
        string uom
        date period_start
        date period_end
        string data_quality
        string source_table
        string source_record_ids
    }
    ghg_emission {
        string emission_id PK
        string activity_id FK
        string factor_id FK
        string run_id FK
        string gas
        float co2e_tonnes
        string method
        float uncertainty_pct
    }
    kpi_value {
        string kpi_value_id PK
        string metric_id FK
        string entity_id FK
        date period_start
        date period_end
        float value
        string target_id FK
        string status
        string run_id FK
    }
    metric {
        string metric_id PK
    }
    entity {
        string entity_id PK
    }
    emission_factor {
        string factor_id PK
    }
    metric ||--o{ kpi_target : "metric_id"
    entity ||--o{ kpi_target : "entity_id"
    calc_run ||--o{ ghg_activity_data : "run_id"
    entity ||--o{ ghg_activity_data : "entity_id"
    ghg_activity_data ||--o{ ghg_emission : "activity_id"
    emission_factor ||--o{ ghg_emission : "factor_id"
    calc_run ||--o{ ghg_emission : "run_id"
    metric ||--o{ kpi_value : "metric_id"
    entity ||--o{ kpi_value : "entity_id"
    kpi_target |o--o{ kpi_value : "target_id"
    calc_run |o--o{ kpi_value : "run_id"
```
