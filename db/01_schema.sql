-- Port ESG platform: PostgreSQL 13+ DDL (generated from model.py)
-- 50 tables in schema "esg"
-- High-volume tables (energy_meter_reading, ais_position, *_reading) should be
-- partitioned by month or moved to a time-series extension in production.

CREATE SCHEMA IF NOT EXISTS esg;

-- ======================================================================
-- Core: source registry, organisation, tenants, evidence
-- ======================================================================

CREATE TABLE esg.source_system (
    source_system_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    source_class text NOT NULL CHECK (source_class IN ('A_operations', 'B_energy', 'C_environment', 'D_enterprise', 'E_third_party', 'F_reference', 'G_manual', 'H_derived')),
    ingestion_mode text CHECK (ingestion_mode IN ('api', 'stream', 'batch', 'iot', 'portal', 'manual')),
    refresh_frequency text,
    owner text,
    active boolean NOT NULL DEFAULT true
);
COMMENT ON TABLE esg.source_system IS 'Registry of every system or channel that feeds the platform, classified A-H.';

CREATE TABLE esg.entity (
    entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_entity_id uuid,
    entity_type text NOT NULL CHECK (entity_type IN ('port', 'terminal', 'business_unit', 'facility', 'tenant')),
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    operational_control boolean NOT NULL DEFAULT true,
    equity_share_pct numeric(18,4) NOT NULL DEFAULT 100 CHECK (equity_share_pct BETWEEN 0 AND 100),
    country_code text,
    valid_from date,
    valid_to date,
    FOREIGN KEY (parent_entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.entity IS 'Organisational hierarchy: port > terminal > business unit / facility / tenant. Drives reporting boundaries.';
CREATE INDEX ix_entity_parent_entity_id ON esg.entity (parent_entity_id);

CREATE TABLE esg.evidence_document (
    doc_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_uri text NOT NULL,
    sha256 text,
    mime_type text,
    description text,
    uploaded_by text,
    uploaded_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE esg.evidence_document IS 'Uploaded evidence (invoices, certificates, manifests) with content hash for audit.';

CREATE TABLE esg.tenant (
    tenant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    name text NOT NULL,
    tenant_type text CHECK (tenant_type IN ('terminal_operator', 'logistics', 'industrial', 'service', 'other')),
    contract_ref text,
    esg_clause boolean NOT NULL DEFAULT false,
    operational_control boolean NOT NULL DEFAULT false,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.tenant IS 'Tenants, concessionaires and contractors. operational_control decides Scope 1/2 vs Scope 3.';
CREATE INDEX ix_tenant_entity_id ON esg.tenant (entity_id);

-- ======================================================================
-- A. Port operations: vessels, calls, cargo, gate
-- ======================================================================

CREATE TABLE esg.vessel (
    imo_number text PRIMARY KEY,
    vessel_name text NOT NULL,
    vessel_type text,
    flag text,
    gross_tonnage numeric(18,4) CHECK (gross_tonnage >= 0),
    dwt numeric(18,4) CHECK (dwt >= 0),
    teu_capacity integer CHECK (teu_capacity >= 0),
    built_year integer,
    engine_type text,
    main_engine_kw numeric(18,4) CHECK (main_engine_kw >= 0),
    aux_engine_kw numeric(18,4) CHECK (aux_engine_kw >= 0),
    default_fuel_type text,
    esi_score numeric(18,4),
    cii_rating text CHECK (cii_rating IN ('A', 'B', 'C', 'D', 'E'))
);
COMMENT ON TABLE esg.vessel IS 'Vessel master data (IMO registry, class and engine particulars).';

CREATE TABLE esg.berth (
    berth_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    terminal_id uuid NOT NULL,
    berth_code text NOT NULL,
    length_m numeric(18,4) CHECK (length_m >= 0),
    shore_power_capable boolean NOT NULL DEFAULT false,
    FOREIGN KEY (terminal_id) REFERENCES esg.entity(entity_id),
    UNIQUE (terminal_id, berth_code)
);
COMMENT ON TABLE esg.berth IS 'Berths, with shore-power capability.';
CREATE INDEX ix_berth_terminal_id ON esg.berth (terminal_id);

CREATE TABLE esg.vessel_call (
    call_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    imo_number text NOT NULL,
    berth_id uuid,
    terminal_id uuid,
    voyage_ref text,
    eta timestamptz,
    ata timestamptz,
    atb timestamptz,
    atd timestamptz,
    hours_at_anchor numeric(18,4) CHECK (hours_at_anchor >= 0),
    hours_at_berth numeric(18,4) CHECK (hours_at_berth >= 0),
    hours_manoeuvring numeric(18,4) CHECK (hours_manoeuvring >= 0),
    shore_power_connected boolean NOT NULL DEFAULT false,
    fuel_type_at_berth text,
    cargo_tonnes numeric(18,4) CHECK (cargo_tonnes >= 0),
    teu_moved integer CHECK (teu_moved >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (imo_number) REFERENCES esg.vessel(imo_number),
    FOREIGN KEY (berth_id) REFERENCES esg.berth(berth_id),
    FOREIGN KEY (terminal_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.vessel_call IS 'One row per vessel call; the anchor for Scope 3 vessel emissions (TOS / PCS / berth planning).';
CREATE INDEX ix_vessel_call_berth_id ON esg.vessel_call (berth_id);
CREATE INDEX ix_vessel_call_imo_number ON esg.vessel_call (imo_number);
CREATE INDEX ix_vessel_call_imo_number_atb ON esg.vessel_call (imo_number, atb);
CREATE INDEX ix_vessel_call_source_system_id ON esg.vessel_call (source_system_id);
CREATE INDEX ix_vessel_call_terminal_id ON esg.vessel_call (terminal_id);

CREATE TABLE esg.asset (
    asset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    asset_code text NOT NULL UNIQUE,
    asset_type text NOT NULL CHECK (asset_type IN ('crane', 'rtg', 'truck', 'reach_stacker', 'vessel_owned', 'building', 'generator', 'other')),
    fuel_type text,
    electrified boolean NOT NULL DEFAULT false,
    rated_kw numeric(18,4) CHECK (rated_kw >= 0),
    commissioned_date date,
    decommissioned_date date,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'standby', 'retired')),
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.asset IS 'Port-owned equipment: cranes, RTGs, trucks, generators, buildings.';
CREATE INDEX ix_asset_entity_id ON esg.asset (entity_id);

CREATE TABLE esg.ais_position (
    position_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    imo_number text NOT NULL,
    ts timestamptz NOT NULL,
    lat numeric(9,6) CHECK (lat BETWEEN -90 AND 90),
    lon numeric(9,6) CHECK (lon BETWEEN -180 AND 180),
    speed_knots numeric(18,4) CHECK (speed_knots >= 0),
    heading numeric(18,4) CHECK (heading BETWEEN 0 AND 360),
    nav_status text,
    source_system_id uuid,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (imo_number) REFERENCES esg.vessel(imo_number),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id)
);
COMMENT ON TABLE esg.ais_position IS 'AIS position reports for approach and manoeuvring emission estimates. High volume: partition by month.';
CREATE INDEX ix_ais_position_imo_number ON esg.ais_position (imo_number);
CREATE INDEX ix_ais_position_imo_number_ts ON esg.ais_position (imo_number, ts);
CREATE INDEX ix_ais_position_source_system_id ON esg.ais_position (source_system_id);

CREATE TABLE esg.container_move (
    move_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id uuid,
    equipment_id uuid,
    container_ref text,
    move_type text NOT NULL CHECK (move_type IN ('load', 'discharge', 'restow', 'gate_in', 'gate_out')),
    ts timestamptz NOT NULL,
    teu numeric(18,4) CHECK (teu >= 0),
    weight_kg numeric(18,4) CHECK (weight_kg >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (call_id) REFERENCES esg.vessel_call(call_id),
    FOREIGN KEY (equipment_id) REFERENCES esg.asset(asset_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.container_move IS 'Container / cargo moves from the TOS, linked to equipment for per-asset energy intensity.';
CREATE INDEX ix_container_move_call_id ON esg.container_move (call_id);
CREATE INDEX ix_container_move_call_id_ts ON esg.container_move (call_id, ts);
CREATE INDEX ix_container_move_equipment_id ON esg.container_move (equipment_id);
CREATE INDEX ix_container_move_source_system_id ON esg.container_move (source_system_id);

CREATE TABLE esg.gate_transaction (
    txn_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    terminal_id uuid NOT NULL,
    truck_ref text,
    haulier_tenant_id uuid,
    gate_in timestamptz NOT NULL,
    gate_out timestamptz,
    turnaround_min numeric(18,4) CHECK (turnaround_min >= 0),
    container_ref text,
    truck_engine_class text,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (terminal_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (haulier_tenant_id) REFERENCES esg.tenant(tenant_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.gate_transaction IS 'Truck gate events; basis for turnaround time and idling emissions.';
CREATE INDEX ix_gate_transaction_haulier_tenant_id ON esg.gate_transaction (haulier_tenant_id);
CREATE INDEX ix_gate_transaction_source_system_id ON esg.gate_transaction (source_system_id);
CREATE INDEX ix_gate_transaction_terminal_id ON esg.gate_transaction (terminal_id);
CREATE INDEX ix_gate_transaction_terminal_id_gate_in ON esg.gate_transaction (terminal_id, gate_in);

-- ======================================================================
-- B. Energy and utilities
-- ======================================================================

CREATE TABLE esg.meter (
    meter_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    meter_code text NOT NULL UNIQUE,
    entity_id uuid NOT NULL,
    asset_id uuid,
    tenant_id uuid,
    meter_type text NOT NULL CHECK (meter_type IN ('electricity', 'fuel', 'water', 'gas', 'shore_power')),
    location_desc text,
    scope_boundary text NOT NULL CHECK (scope_boundary IN ('own', 'tenant', 'shared')),
    uom text NOT NULL,
    energy_type text NOT NULL DEFAULT 'grid' CHECK (energy_type IN ('grid', 'solar', 'diesel_gen', 'shore_power', 'other')),
    multiplier numeric(18,4) NOT NULL DEFAULT 1 CHECK (multiplier >= 0),
    register_digits integer,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (asset_id) REFERENCES esg.asset(asset_id),
    FOREIGN KEY (tenant_id) REFERENCES esg.tenant(tenant_id)
);
COMMENT ON TABLE esg.meter IS 'Meter registry. scope_boundary says whose consumption it is.';
CREATE INDEX ix_meter_asset_id ON esg.meter (asset_id);
CREATE INDEX ix_meter_entity_id ON esg.meter (entity_id);
CREATE INDEX ix_meter_tenant_id ON esg.meter (tenant_id);

CREATE TABLE esg.meter_register_reading (
    register_reading_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    meter_id uuid NOT NULL,
    read_at timestamptz NOT NULL,
    register_value numeric(18,4) NOT NULL CHECK (register_value >= 0),
    uom text NOT NULL,
    evidence_doc_id uuid,
    rollover boolean NOT NULL DEFAULT false,
    consumption_raw numeric(18,4) CHECK (consumption_raw >= 0),
    derived_reading_id bigint,
    note text,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (meter_id) REFERENCES esg.meter(meter_id),
    FOREIGN KEY (evidence_doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version),
    UNIQUE (meter_id, read_at)
);
COMMENT ON TABLE esg.meter_register_reading IS 'Cumulative register (odometer) readings keyed in or read from a photo by field operators. Consumption since the previous validated reading is derived into energy_meter_reading once approved.';
CREATE INDEX ix_meter_register_reading_evidence_doc_id ON esg.meter_register_reading (evidence_doc_id);
CREATE INDEX ix_meter_register_reading_meter_id ON esg.meter_register_reading (meter_id);
CREATE INDEX ix_meter_register_reading_meter_id_read_at ON esg.meter_register_reading (meter_id, read_at);
CREATE INDEX ix_meter_register_reading_source_system_id ON esg.meter_register_reading (source_system_id);

CREATE TABLE esg.energy_meter_reading (
    reading_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    meter_id uuid NOT NULL,
    ts timestamptz NOT NULL,
    energy_kwh numeric(18,4) NOT NULL CHECK (energy_kwh >= 0),
    energy_type text NOT NULL CHECK (energy_type IN ('grid', 'solar', 'diesel_gen', 'shore_power', 'other')),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    source_system_id uuid,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (meter_id) REFERENCES esg.meter(meter_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (meter_id, ts, energy_type)
);
COMMENT ON TABLE esg.energy_meter_reading IS 'Interval readings from smart meters / SCADA. High volume: partition by month or use a time-series extension.';
CREATE INDEX ix_energy_meter_reading_meter_id ON esg.energy_meter_reading (meter_id);
CREATE INDEX ix_energy_meter_reading_source_system_id ON esg.energy_meter_reading (source_system_id);

CREATE TABLE esg.fuel_transaction (
    txn_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id uuid,
    entity_id uuid NOT NULL,
    fuel_type text NOT NULL,
    litres numeric(18,4) NOT NULL CHECK (litres >= 0),
    ts timestamptz NOT NULL,
    dispensing_point text,
    cost numeric(18,4) CHECK (cost >= 0),
    currency text,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (asset_id) REFERENCES esg.asset(asset_id),
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.fuel_transaction IS 'Fuel dispensed to assets (Scope 1 mobile and stationary combustion).';
CREATE INDEX ix_fuel_transaction_asset_id ON esg.fuel_transaction (asset_id);
CREATE INDEX ix_fuel_transaction_entity_id ON esg.fuel_transaction (entity_id);
CREATE INDEX ix_fuel_transaction_entity_id_ts ON esg.fuel_transaction (entity_id, ts);
CREATE INDEX ix_fuel_transaction_source_system_id ON esg.fuel_transaction (source_system_id);

CREATE TABLE esg.shore_power_session (
    session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id uuid NOT NULL,
    berth_id uuid,
    start_ts timestamptz NOT NULL,
    end_ts timestamptz,
    kwh_delivered numeric(18,4) CHECK (kwh_delivered >= 0),
    peak_kw numeric(18,4) CHECK (peak_kw >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (call_id) REFERENCES esg.vessel_call(call_id),
    FOREIGN KEY (berth_id) REFERENCES esg.berth(berth_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.shore_power_session IS 'Shore-power (cold ironing) sessions per vessel call.';
CREATE INDEX ix_shore_power_session_berth_id ON esg.shore_power_session (berth_id);
CREATE INDEX ix_shore_power_session_call_id ON esg.shore_power_session (call_id);
CREATE INDEX ix_shore_power_session_source_system_id ON esg.shore_power_session (source_system_id);

CREATE TABLE esg.refrigerant_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id uuid,
    work_order_ref text,
    refrigerant_type text NOT NULL,
    kg_added numeric(18,4) NOT NULL CHECK (kg_added >= 0),
    event_date date NOT NULL,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (asset_id) REFERENCES esg.asset(asset_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.refrigerant_event IS 'Refrigerant top-ups from maintenance work orders (CMMS); basis for fugitive emissions.';
CREATE INDEX ix_refrigerant_event_asset_id ON esg.refrigerant_event (asset_id);
CREATE INDEX ix_refrigerant_event_source_system_id ON esg.refrigerant_event (source_system_id);

-- ======================================================================
-- C. Environmental monitoring
-- ======================================================================

CREATE TABLE esg.sensor (
    sensor_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_code text NOT NULL UNIQUE,
    sensor_type text NOT NULL CHECK (sensor_type IN ('air', 'water', 'noise', 'weather')),
    station_id text,
    entity_id uuid,
    lat numeric(9,6) CHECK (lat BETWEEN -90 AND 90),
    lon numeric(9,6) CHECK (lon BETWEEN -180 AND 180),
    calibration_date date,
    owner text,
    active boolean NOT NULL DEFAULT true,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.sensor IS 'Environmental sensor registry (air, water, noise, weather).';
CREATE INDEX ix_sensor_entity_id ON esg.sensor (entity_id);

CREATE TABLE esg.air_quality_reading (
    reading_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sensor_id uuid NOT NULL,
    ts timestamptz NOT NULL,
    pm25 numeric(18,4) CHECK (pm25 >= 0),
    pm10 numeric(18,4) CHECK (pm10 >= 0),
    no2 numeric(18,4) CHECK (no2 >= 0),
    so2 numeric(18,4) CHECK (so2 >= 0),
    co numeric(18,4) CHECK (co >= 0),
    o3 numeric(18,4) CHECK (o3 >= 0),
    quality_flag text,
    source_system_id uuid,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sensor_id) REFERENCES esg.sensor(sensor_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (sensor_id, ts)
);
COMMENT ON TABLE esg.air_quality_reading IS 'Air quality readings in ug/m3 (CO in mg/m3).';
CREATE INDEX ix_air_quality_reading_sensor_id ON esg.air_quality_reading (sensor_id);
CREATE INDEX ix_air_quality_reading_source_system_id ON esg.air_quality_reading (source_system_id);

CREATE TABLE esg.water_quality_reading (
    reading_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sensor_id uuid NOT NULL,
    ts timestamptz NOT NULL,
    ph numeric(18,4) CHECK (ph BETWEEN 0 AND 14),
    turbidity_ntu numeric(18,4) CHECK (turbidity_ntu >= 0),
    dissolved_oxygen_mgl numeric(18,4) CHECK (dissolved_oxygen_mgl >= 0),
    temperature_c numeric(18,4),
    hydrocarbon_ppm numeric(18,4) CHECK (hydrocarbon_ppm >= 0),
    quality_flag text,
    source_system_id uuid,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sensor_id) REFERENCES esg.sensor(sensor_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (sensor_id, ts)
);
COMMENT ON TABLE esg.water_quality_reading IS 'Harbour / discharge water quality readings.';
CREATE INDEX ix_water_quality_reading_sensor_id ON esg.water_quality_reading (sensor_id);
CREATE INDEX ix_water_quality_reading_source_system_id ON esg.water_quality_reading (source_system_id);

CREATE TABLE esg.water_record (
    water_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    flow_type text NOT NULL CHECK (flow_type IN ('withdrawal', 'discharge', 'recycled')),
    source_type text NOT NULL CHECK (source_type IN ('municipal', 'desalinated', 'groundwater', 'surface', 'seawater', 'third_party', 'treated_effluent')),
    volume_m3 numeric(18,4) NOT NULL CHECK (volume_m3 >= 0),
    water_stress_area boolean NOT NULL DEFAULT false,
    doc_id uuid,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version),
    CHECK (period_end >= period_start)
);
COMMENT ON TABLE esg.water_record IS 'Water withdrawal, discharge and recycling volumes by entity and period (GRI 303).';
CREATE INDEX ix_water_record_doc_id ON esg.water_record (doc_id);
CREATE INDEX ix_water_record_entity_id ON esg.water_record (entity_id);
CREATE INDEX ix_water_record_source_system_id ON esg.water_record (source_system_id);

CREATE TABLE esg.noise_reading (
    reading_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sensor_id uuid NOT NULL,
    ts timestamptz NOT NULL,
    leq_db numeric(18,4),
    lmax_db numeric(18,4),
    quality_flag text,
    source_system_id uuid,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sensor_id) REFERENCES esg.sensor(sensor_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (sensor_id, ts)
);
COMMENT ON TABLE esg.noise_reading IS 'Noise level readings in dB.';
CREATE INDEX ix_noise_reading_sensor_id ON esg.noise_reading (sensor_id);
CREATE INDEX ix_noise_reading_source_system_id ON esg.noise_reading (source_system_id);

CREATE TABLE esg.weather_observation (
    reading_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sensor_id uuid NOT NULL,
    ts timestamptz NOT NULL,
    temp_c numeric(18,4),
    wind_speed_ms numeric(18,4) CHECK (wind_speed_ms >= 0),
    wind_dir_deg numeric(18,4) CHECK (wind_dir_deg BETWEEN 0 AND 360),
    humidity_pct numeric(18,4) CHECK (humidity_pct BETWEEN 0 AND 100),
    tide_level_m numeric(18,4),
    source_system_id uuid,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sensor_id) REFERENCES esg.sensor(sensor_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (sensor_id, ts)
);
COMMENT ON TABLE esg.weather_observation IS 'Weather and tide observations used to normalise and contextualise readings.';
CREATE INDEX ix_weather_observation_sensor_id ON esg.weather_observation (sensor_id);
CREATE INDEX ix_weather_observation_source_system_id ON esg.weather_observation (source_system_id);

CREATE TABLE esg.environmental_incident (
    incident_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    incident_type text NOT NULL CHECK (incident_type IN ('spill', 'exceedance', 'odour', 'other')),
    occurred_at timestamptz NOT NULL,
    location_desc text,
    quantity numeric(18,4) CHECK (quantity >= 0),
    quantity_uom text,
    severity text CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    root_cause text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'closed')),
    regulator_notified boolean NOT NULL DEFAULT false,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.environmental_incident IS 'Spills, exceedances and odour events.';
CREATE INDEX ix_environmental_incident_entity_id ON esg.environmental_incident (entity_id);
CREATE INDEX ix_environmental_incident_source_system_id ON esg.environmental_incident (source_system_id);

-- ======================================================================
-- D. Enterprise systems: HR, HSE, ERP, GRC
-- ======================================================================

CREATE TABLE esg.spend_record (
    spend_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid,
    po_ref text,
    supplier_ref text,
    supplier_name text,
    category text NOT NULL,
    material_code text,
    quantity numeric(18,4) CHECK (quantity >= 0),
    uom text,
    amount numeric(18,4) NOT NULL,
    currency text NOT NULL,
    spend_date date NOT NULL,
    scope3_category integer CHECK (scope3_category BETWEEN 1 AND 15),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.spend_record IS 'Procurement spend (ERP) used for Scope 3 purchased goods and services.';
CREATE INDEX ix_spend_record_entity_id ON esg.spend_record (entity_id);
CREATE INDEX ix_spend_record_source_system_id ON esg.spend_record (source_system_id);

CREATE TABLE esg.utility_invoice (
    invoice_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    utility_type text NOT NULL CHECK (utility_type IN ('electricity', 'water', 'gas', 'fuel')),
    consumption numeric(18,4) CHECK (consumption >= 0),
    uom text,
    period_start date NOT NULL,
    period_end date NOT NULL,
    amount numeric(18,4),
    currency text,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.utility_invoice IS 'Utility invoices; cross-check against meter data.';
CREATE INDEX ix_utility_invoice_entity_id ON esg.utility_invoice (entity_id);
CREATE INDEX ix_utility_invoice_source_system_id ON esg.utility_invoice (source_system_id);

CREATE TABLE esg.employee_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    period_end date NOT NULL,
    department text,
    gender text,
    nationality_group text,
    age_band text,
    grade_band text,
    contract_type text,
    headcount integer NOT NULL CHECK (headcount >= 0),
    leavers integer CHECK (leavers >= 0),
    voluntary_leavers integer CHECK (voluntary_leavers >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.employee_snapshot IS 'Aggregated HR snapshot per period. Aggregates only: no individual-level personal data.';
CREATE INDEX ix_employee_snapshot_entity_id ON esg.employee_snapshot (entity_id);
CREATE INDEX ix_employee_snapshot_source_system_id ON esg.employee_snapshot (source_system_id);

CREATE TABLE esg.training_record (
    training_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    course_category text NOT NULL,
    training_hours numeric(18,4) NOT NULL CHECK (training_hours >= 0),
    participants integer CHECK (participants >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.training_record IS 'Aggregated training hours per period and category.';
CREATE INDEX ix_training_record_entity_id ON esg.training_record (entity_id);
CREATE INDEX ix_training_record_source_system_id ON esg.training_record (source_system_id);

CREATE TABLE esg.hours_worked (
    hours_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    employee_hours numeric(18,4) NOT NULL CHECK (employee_hours >= 0),
    contractor_hours numeric(18,4) CHECK (contractor_hours >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version),
    UNIQUE (entity_id, period_start, period_end, version)
);
COMMENT ON TABLE esg.hours_worked IS 'Exposure hours: the denominator for LTIFR / TRIR.';
CREATE INDEX ix_hours_worked_entity_id ON esg.hours_worked (entity_id);
CREATE INDEX ix_hours_worked_source_system_id ON esg.hours_worked (source_system_id);

CREATE TABLE esg.safety_incident (
    incident_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    incident_type text NOT NULL CHECK (incident_type IN ('lost_time', 'recordable', 'first_aid', 'fatality', 'near_miss', 'dangerous_occurrence')),
    person_type text NOT NULL CHECK (person_type IN ('employee', 'contractor', 'visitor')),
    occurred_at timestamptz NOT NULL,
    lost_days integer CHECK (lost_days >= 0),
    severity text,
    injury_class text,
    location_desc text,
    root_cause text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'closed')),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.safety_incident IS 'Safety incidents and near-misses from the HSE system.';
CREATE INDEX ix_safety_incident_entity_id ON esg.safety_incident (entity_id);
CREATE INDEX ix_safety_incident_entity_id_occurred_at ON esg.safety_incident (entity_id, occurred_at);
CREATE INDEX ix_safety_incident_source_system_id ON esg.safety_incident (source_system_id);

CREATE TABLE esg.risk (
    risk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    risk_code text NOT NULL UNIQUE,
    entity_id uuid,
    category text NOT NULL CHECK (category IN ('E', 'S', 'G', 'operational', 'climate')),
    title text NOT NULL,
    likelihood integer CHECK (likelihood BETWEEN 1 AND 5),
    impact integer CHECK (impact BETWEEN 1 AND 5),
    owner text,
    mitigation text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'mitigating', 'accepted', 'closed')),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.risk IS 'ESG risk register (GRC).';
CREATE INDEX ix_risk_entity_id ON esg.risk (entity_id);
CREATE INDEX ix_risk_source_system_id ON esg.risk (source_system_id);

CREATE TABLE esg.policy (
    policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    version_label text,
    approval_date date,
    next_review_date date,
    owner_role text,
    doc_id uuid,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.policy IS 'Governance policies and their review cycle.';
CREATE INDEX ix_policy_doc_id ON esg.policy (doc_id);
CREATE INDEX ix_policy_source_system_id ON esg.policy (source_system_id);

CREATE TABLE esg.audit_finding (
    finding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid,
    audit_ref text,
    severity text CHECK (severity IN ('low', 'medium', 'high')),
    description text,
    due_date date,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'closed')),
    doc_id uuid,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.audit_finding IS 'Internal / external audit findings.';
CREATE INDEX ix_audit_finding_doc_id ON esg.audit_finding (doc_id);
CREATE INDEX ix_audit_finding_entity_id ON esg.audit_finding (entity_id);
CREATE INDEX ix_audit_finding_source_system_id ON esg.audit_finding (source_system_id);

CREATE TABLE esg.certification (
    certification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    standard text NOT NULL,
    issue_date date,
    expiry_date date,
    result text,
    doc_id uuid,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.certification IS 'Management-system certifications (ISO 14001, ISO 45001, EcoPorts, etc.).';
CREATE INDEX ix_certification_doc_id ON esg.certification (doc_id);
CREATE INDEX ix_certification_entity_id ON esg.certification (entity_id);
CREATE INDEX ix_certification_source_system_id ON esg.certification (source_system_id);

-- ======================================================================
-- F. Reference: frameworks, metrics, emission factors
-- ======================================================================

CREATE TABLE esg.framework (
    framework_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    version_label text
);
COMMENT ON TABLE esg.framework IS 'Reporting frameworks (GRI, IFRS S1/S2, TCFD, SASB, CDP, local regulator).';

CREATE TABLE esg.framework_requirement (
    requirement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    framework_id uuid NOT NULL,
    disclosure_code text NOT NULL,
    title text,
    description text,
    FOREIGN KEY (framework_id) REFERENCES esg.framework(framework_id),
    UNIQUE (framework_id, disclosure_code)
);
COMMENT ON TABLE esg.framework_requirement IS 'Individual disclosures within a framework.';
CREATE INDEX ix_framework_requirement_framework_id ON esg.framework_requirement (framework_id);

CREATE TABLE esg.metric (
    metric_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_code text NOT NULL UNIQUE,
    name text NOT NULL,
    pillar text NOT NULL CHECK (pillar IN ('E', 'S', 'G')),
    unit text,
    formula_ref text,
    owner text,
    frequency text CHECK (frequency IN ('realtime', 'daily', 'monthly', 'quarterly', 'annual')),
    source_classes text[]
);
COMMENT ON TABLE esg.metric IS 'Metric catalog: links raw data to KPIs and disclosures.';

CREATE TABLE esg.metric_requirement_map (
    metric_id uuid NOT NULL,
    requirement_id uuid NOT NULL,
    PRIMARY KEY (metric_id, requirement_id),
    FOREIGN KEY (metric_id) REFERENCES esg.metric(metric_id),
    FOREIGN KEY (requirement_id) REFERENCES esg.framework_requirement(requirement_id)
);
COMMENT ON TABLE esg.metric_requirement_map IS 'Many-to-many mapping of metrics to framework disclosures.';
CREATE INDEX ix_metric_requirement_map_requirement_id ON esg.metric_requirement_map (requirement_id);

CREATE TABLE esg.emission_factor (
    factor_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL,
    activity_type text NOT NULL,
    fuel_or_grid text,
    gas text NOT NULL CHECK (gas IN ('CO2', 'CH4', 'N2O', 'CO2e', 'HFC', 'SF6')),
    value numeric(18,4) NOT NULL CHECK (value >= 0),
    uom text NOT NULL,
    gwp_basis text CHECK (gwp_basis IN ('AR4', 'AR5', 'AR6')),
    region text,
    valid_from date NOT NULL,
    valid_to date
);
COMMENT ON TABLE esg.emission_factor IS 'Versioned emission factors. Never overwrite: add a new row with a new validity window.';
CREATE INDEX ix_emission_factor_activity_type_fuel_or_grid_valid_from ON esg.emission_factor (activity_type, fuel_or_grid, valid_from);

CREATE TABLE esg.climate_hazard (
    hazard_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid,
    hazard_type text NOT NULL,
    scenario text NOT NULL,
    horizon_year integer NOT NULL,
    value numeric(18,4),
    uom text,
    source text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.climate_hazard IS 'Physical climate hazard projections per location / scenario.';
CREATE INDEX ix_climate_hazard_entity_id ON esg.climate_hazard (entity_id);

-- ======================================================================
-- E and G. Third-party submissions and manual records
-- ======================================================================

CREATE TABLE esg.tenant_esg_submission (
    submission_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    metric_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    value numeric(18,4) NOT NULL,
    uom text,
    evidence_doc_id uuid,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'approved', 'rejected')),
    reviewer text,
    review_date date,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (tenant_id) REFERENCES esg.tenant(tenant_id),
    FOREIGN KEY (metric_id) REFERENCES esg.metric(metric_id),
    FOREIGN KEY (evidence_doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.tenant_esg_submission IS 'Metric values submitted by tenants and contractors, with review workflow.';
CREATE INDEX ix_tenant_esg_submission_evidence_doc_id ON esg.tenant_esg_submission (evidence_doc_id);
CREATE INDEX ix_tenant_esg_submission_metric_id ON esg.tenant_esg_submission (metric_id);
CREATE INDEX ix_tenant_esg_submission_source_system_id ON esg.tenant_esg_submission (source_system_id);
CREATE INDEX ix_tenant_esg_submission_tenant_id ON esg.tenant_esg_submission (tenant_id);

CREATE TABLE esg.shipping_line_data (
    record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    line_name text NOT NULL,
    imo_number text,
    period_start date NOT NULL,
    period_end date NOT NULL,
    fuel_consumed_at_berth_t numeric(18,4) CHECK (fuel_consumed_at_berth_t >= 0),
    ets_certificate_ref text,
    cii_rating text CHECK (cii_rating IN ('A', 'B', 'C', 'D', 'E')),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (imo_number) REFERENCES esg.vessel(imo_number),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.shipping_line_data IS 'Voluntary shipping-line disclosures: berth fuel use, ETS and CII.';
CREATE INDEX ix_shipping_line_data_imo_number ON esg.shipping_line_data (imo_number);
CREATE INDEX ix_shipping_line_data_source_system_id ON esg.shipping_line_data (source_system_id);

CREATE TABLE esg.waste_manifest (
    manifest_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL,
    manifest_ref text,
    waste_type text NOT NULL,
    hazardous boolean NOT NULL DEFAULT false,
    quantity_kg numeric(18,4) NOT NULL CHECK (quantity_kg >= 0),
    disposal_method text CHECK (disposal_method IN ('recycle', 'recover', 'landfill', 'incineration', 'treatment', 'other')),
    contractor text,
    manifest_date date NOT NULL,
    doc_id uuid,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (doc_id) REFERENCES esg.evidence_document(doc_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.waste_manifest IS 'Waste transfer manifests.';
CREATE INDEX ix_waste_manifest_doc_id ON esg.waste_manifest (doc_id);
CREATE INDEX ix_waste_manifest_entity_id ON esg.waste_manifest (entity_id);
CREATE INDEX ix_waste_manifest_source_system_id ON esg.waste_manifest (source_system_id);

CREATE TABLE esg.community_grievance (
    grievance_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid,
    received_date date NOT NULL,
    channel text,
    category text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'resolved', 'closed')),
    resolution_days integer CHECK (resolution_days >= 0),
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.community_grievance IS 'Community and stakeholder grievances.';
CREATE INDEX ix_community_grievance_entity_id ON esg.community_grievance (entity_id);
CREATE INDEX ix_community_grievance_source_system_id ON esg.community_grievance (source_system_id);

CREATE TABLE esg.survey_response (
    response_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    survey_ref text NOT NULL,
    stakeholder_group text NOT NULL,
    topic text NOT NULL,
    score numeric(18,4),
    response_date date,
    source_system_id uuid,
    source_record_id text,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    data_quality text NOT NULL DEFAULT 'manual' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    validation_status text NOT NULL DEFAULT 'pending' CHECK (validation_status IN ('pending', 'validated', 'rejected')),
    version integer NOT NULL DEFAULT 1,
    created_by text,
    approved_by text,
    FOREIGN KEY (source_system_id) REFERENCES esg.source_system(source_system_id),
    UNIQUE (source_system_id, source_record_id, version)
);
COMMENT ON TABLE esg.survey_response IS 'Stakeholder survey scores used in double-materiality assessment.';
CREATE INDEX ix_survey_response_source_system_id ON esg.survey_response (source_system_id);

-- ======================================================================
-- H. Derived: GHG inventory and KPIs
-- ======================================================================

CREATE TABLE esg.calc_run (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    period_start date NOT NULL,
    period_end date NOT NULL,
    factor_set_version text,
    triggered_by text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    notes text
);
COMMENT ON TABLE esg.calc_run IS 'A calculation run: pins the factor-set version so results are reproducible.';

CREATE TABLE esg.kpi_target (
    target_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    baseline_year integer,
    baseline_value numeric(18,4),
    target_year integer NOT NULL,
    target_value numeric(18,4) NOT NULL,
    description text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'achieved', 'superseded')),
    FOREIGN KEY (metric_id) REFERENCES esg.metric(metric_id),
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.kpi_target IS 'Baselines and targets per metric and entity.';
CREATE INDEX ix_kpi_target_entity_id ON esg.kpi_target (entity_id);
CREATE INDEX ix_kpi_target_metric_id ON esg.kpi_target (metric_id);

CREATE TABLE esg.ghg_activity_data (
    activity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    scope integer NOT NULL CHECK (scope BETWEEN 1 AND 3),
    scope3_category integer CHECK (scope3_category BETWEEN 1 AND 15),
    activity_type text NOT NULL,
    quantity numeric(18,4) NOT NULL,
    uom text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    data_quality text NOT NULL DEFAULT 'measured' CHECK (data_quality IN ('measured', 'estimated', 'manual', 'imputed')),
    source_table text,
    source_record_ids text[],
    FOREIGN KEY (run_id) REFERENCES esg.calc_run(run_id),
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id)
);
COMMENT ON TABLE esg.ghg_activity_data IS 'Normalised activity data feeding the GHG inventory, with lineage back to source rows.';
CREATE INDEX ix_ghg_activity_data_entity_id ON esg.ghg_activity_data (entity_id);
CREATE INDEX ix_ghg_activity_data_entity_id_period_start ON esg.ghg_activity_data (entity_id, period_start);
CREATE INDEX ix_ghg_activity_data_run_id ON esg.ghg_activity_data (run_id);

CREATE TABLE esg.ghg_emission (
    emission_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id uuid NOT NULL,
    factor_id uuid NOT NULL,
    run_id uuid NOT NULL,
    gas text NOT NULL CHECK (gas IN ('CO2', 'CH4', 'N2O', 'CO2e', 'HFC', 'SF6')),
    co2e_tonnes numeric(18,4) NOT NULL,
    method text,
    uncertainty_pct numeric(18,4) CHECK (uncertainty_pct >= 0),
    FOREIGN KEY (activity_id) REFERENCES esg.ghg_activity_data(activity_id),
    FOREIGN KEY (factor_id) REFERENCES esg.emission_factor(factor_id),
    FOREIGN KEY (run_id) REFERENCES esg.calc_run(run_id)
);
COMMENT ON TABLE esg.ghg_emission IS 'Calculated emissions per activity and gas.';
CREATE INDEX ix_ghg_emission_activity_id ON esg.ghg_emission (activity_id);
CREATE INDEX ix_ghg_emission_factor_id ON esg.ghg_emission (factor_id);
CREATE INDEX ix_ghg_emission_run_id ON esg.ghg_emission (run_id);

CREATE TABLE esg.kpi_value (
    kpi_value_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    value numeric(18,4) NOT NULL,
    target_id uuid,
    status text CHECK (status IN ('on_track', 'at_risk', 'off_track', 'no_target')),
    run_id uuid,
    FOREIGN KEY (metric_id) REFERENCES esg.metric(metric_id),
    FOREIGN KEY (entity_id) REFERENCES esg.entity(entity_id),
    FOREIGN KEY (target_id) REFERENCES esg.kpi_target(target_id),
    FOREIGN KEY (run_id) REFERENCES esg.calc_run(run_id),
    UNIQUE (metric_id, entity_id, period_start, period_end, run_id)
);
COMMENT ON TABLE esg.kpi_value IS 'Computed KPI values per metric, entity and period, with status versus target.';
CREATE INDEX ix_kpi_value_entity_id ON esg.kpi_value (entity_id);
CREATE INDEX ix_kpi_value_metric_id ON esg.kpi_value (metric_id);
CREATE INDEX ix_kpi_value_run_id ON esg.kpi_value (run_id);
CREATE INDEX ix_kpi_value_target_id ON esg.kpi_value (target_id);

-- ======================================================================
-- Convenience view: Scope 1/2/3 totals by entity and period
-- ======================================================================

CREATE VIEW esg.v_ghg_by_scope AS
SELECT a.run_id, a.entity_id, a.scope, a.scope3_category, a.period_start, a.period_end,
       SUM(e.co2e_tonnes) AS co2e_tonnes
FROM esg.ghg_activity_data a
JOIN esg.ghg_emission e ON e.activity_id = a.activity_id
GROUP BY a.run_id, a.entity_id, a.scope, a.scope3_category, a.period_start, a.period_end;

