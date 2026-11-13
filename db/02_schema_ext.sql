-- Application tables layered on the ESG data model: users, immutable audit log, alerting.

ALTER TABLE esg.metric ADD COLUMN IF NOT EXISTS direction text NOT NULL DEFAULT 'lower'
    CHECK (direction IN ('lower', 'higher'));   -- which way is "better" when comparing to targets

CREATE TABLE esg.app_user (
    user_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    full_name     text,
    role          text NOT NULL CHECK (role IN ('esg_admin', 'data_owner', 'field_operator', 'tenant', 'auditor')),
    password_hash text NOT NULL,
    tenant_id     uuid REFERENCES esg.tenant(tenant_id),   -- required for role = tenant
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login    timestamptz,
    CHECK (role <> 'tenant' OR tenant_id IS NOT NULL)
);

-- Append-only: who changed what, with before / after images.
CREATE TABLE esg.audit_log (
    audit_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL,
    action     text NOT NULL,          -- insert | update | validate | reject | calc_run | login | ...
    table_name text,
    record_id  text,
    before     jsonb,
    after      jsonb,
    detail     text
);
CREATE INDEX ix_audit_log_ts ON esg.audit_log (ts DESC);
CREATE INDEX ix_audit_log_record ON esg.audit_log (table_name, record_id);

CREATE FUNCTION esg.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'esg.audit_log is append-only';
END $$;
CREATE TRIGGER trg_audit_log_immutable BEFORE UPDATE OR DELETE ON esg.audit_log
    FOR EACH ROW EXECUTE FUNCTION esg.audit_log_immutable();

CREATE TABLE esg.alert_rule (
    rule_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL UNIQUE,
    rule_type  text NOT NULL CHECK (rule_type IN (
                   'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
                   'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
                   'workflow_backlog', 'kpi_off_track', 'missing_monthly', 'evidence_flagged')),
    severity   text NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
    params     jsonb NOT NULL DEFAULT '{}',
    channels   text[] NOT NULL DEFAULT '{}',       -- subset of: webhook, email
    recipients text[] NOT NULL DEFAULT '{}',       -- email addresses
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE esg.alert (
    alert_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         uuid NOT NULL REFERENCES esg.alert_rule(rule_id) ON DELETE CASCADE,
    dedupe_key      text NOT NULL,
    severity        text NOT NULL,
    title           text NOT NULL,
    message         text,
    context         jsonb NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    raised_at       timestamptz NOT NULL DEFAULT now(),
    acknowledged_by text,
    acknowledged_at timestamptz,
    resolved_by     text,
    resolved_at     timestamptz,
    notify_status   text
);
-- At most one live alert per rule and condition; a resolved one may be raised again later.
CREATE UNIQUE INDEX ux_alert_live ON esg.alert (rule_id, dedupe_key) WHERE status <> 'resolved';
CREATE INDEX ix_alert_status ON esg.alert (status, raised_at DESC);

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
