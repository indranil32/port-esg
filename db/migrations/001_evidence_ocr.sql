-- 001: field evidence capture with OCR. Idempotent: safe to run more than once.
SET search_path = esg, public;

ALTER TABLE esg.meter ADD COLUMN IF NOT EXISTS energy_type text NOT NULL DEFAULT 'grid'
    CHECK (energy_type IN ('grid', 'solar', 'diesel_gen', 'shore_power', 'other'));
ALTER TABLE esg.meter ADD COLUMN IF NOT EXISTS multiplier numeric(18,4) NOT NULL DEFAULT 1 CHECK (multiplier >= 0);
ALTER TABLE esg.meter ADD COLUMN IF NOT EXISTS register_digits integer;

CREATE TABLE IF NOT EXISTS esg.meter_register_reading (
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
CREATE INDEX IF NOT EXISTS ix_meter_register_reading_evidence_doc_id ON esg.meter_register_reading (evidence_doc_id);
CREATE INDEX IF NOT EXISTS ix_meter_register_reading_meter_id ON esg.meter_register_reading (meter_id);
CREATE INDEX IF NOT EXISTS ix_meter_register_reading_meter_id_read_at ON esg.meter_register_reading (meter_id, read_at);
CREATE INDEX IF NOT EXISTS ix_meter_register_reading_source_system_id ON esg.meter_register_reading (source_system_id);

ALTER TABLE esg.app_user DROP CONSTRAINT IF EXISTS app_user_role_check;
ALTER TABLE esg.app_user ADD CONSTRAINT app_user_role_check CHECK (role IN ('esg_admin', 'data_owner', 'field_operator', 'tenant', 'auditor'));

ALTER TABLE esg.alert_rule DROP CONSTRAINT IF EXISTS alert_rule_rule_type_check;
ALTER TABLE esg.alert_rule ADD CONSTRAINT alert_rule_rule_type_check CHECK (rule_type IN (
    'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
                   'workflow_backlog', 'kpi_off_track', 'missing_monthly', 'evidence_flagged'));

-- Photo evidence captured in the field (meter faces, nameplates, delivery notes), OCR'd on arrival.
-- One capture can be linked to exactly one business record (target_table / target_record_id).
CREATE TABLE IF NOT EXISTS esg.evidence_capture (
    capture_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id             uuid NOT NULL REFERENCES esg.evidence_document(doc_id),
    captured_by        text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    meter_id           uuid REFERENCES esg.meter(meter_id),
    status             text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'linked', 'discarded')),
    target_table       text,
    target_record_id   text,
    linked_at          timestamptz,
    entered_value      numeric(18,4),                 -- what the operator keyed in, for comparison with OCR
    ocr_provider       text,
    ocr_ms             integer,
    ocr_text           text,
    ocr_lines          jsonb NOT NULL DEFAULT '[]',   -- [{text, confidence}]
    ocr_confidence     numeric(5,4),
    candidates         jsonb NOT NULL DEFAULT '[]',   -- numeric readings found: [{value, confidence, raw}]
    exif_taken_at      timestamptz,                   -- camera clock, read from image metadata
    client_captured_at timestamptz,                   -- phone clock when the photo was taken in the app
    gps_lat            numeric(9,6),
    gps_lon            numeric(9,6),
    gps_accuracy_m     numeric(9,2),
    gps_source         text CHECK (gps_source IN ('exif', 'browser')),
    device             text,
    phash              text,                          -- 64-bit difference hash, hex
    image_w            integer,
    image_h            integer,
    flags              jsonb NOT NULL DEFAULT '[]',   -- integrity / quality findings: [{code, severity, message}]
    CHECK ((target_table IS NULL) = (target_record_id IS NULL))
);
COMMENT ON TABLE esg.evidence_capture IS 'Field photo evidence with OCR output, capture time/location and integrity flags; linked to the record it supports.';
CREATE INDEX IF NOT EXISTS ix_evidence_capture_target ON esg.evidence_capture (target_table, target_record_id);
CREATE INDEX IF NOT EXISTS ix_evidence_capture_meter ON esg.evidence_capture (meter_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_evidence_capture_phash ON esg.evidence_capture (phash);
CREATE INDEX IF NOT EXISTS ix_evidence_capture_doc ON esg.evidence_capture (doc_id);
CREATE INDEX IF NOT EXISTS ix_evidence_capture_flagged ON esg.evidence_capture (created_at DESC) WHERE flags <> '[]'::jsonb;
