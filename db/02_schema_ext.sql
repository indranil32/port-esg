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

-- ---- 002: period locks, collection links, audit request context
-- 002: period locks, third-party collection links, request context on the audit log. Idempotent.

CREATE TABLE IF NOT EXISTS esg.schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

ALTER TABLE esg.audit_log ADD COLUMN IF NOT EXISTS ip text;
ALTER TABLE esg.audit_log ADD COLUMN IF NOT EXISTS request_id text;

-- Third parties submit through a link instead of a login. Only a hash of the token is stored.
CREATE TABLE IF NOT EXISTS esg.collection_link (
    link_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash       text NOT NULL UNIQUE,
    tenant_id        uuid NOT NULL REFERENCES esg.tenant(tenant_id),
    metric_ids       uuid[] NOT NULL,
    period_start     date NOT NULL,
    period_end       date NOT NULL,
    message          text,
    created_by       text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    revoked_at       timestamptz,
    used_at          timestamptz,
    submission_count integer NOT NULL DEFAULT 0,
    CHECK (period_end >= period_start),
    CHECK (cardinality(metric_ids) BETWEEN 1 AND 50)
);
CREATE INDEX IF NOT EXISTS ix_collection_link_tenant ON esg.collection_link (tenant_id, created_at DESC);

-- A locked period fixes the reported figures: data dated inside it cannot change until an admin unlocks it with a reason.
CREATE TABLE IF NOT EXISTS esg.reporting_period_lock (
    lock_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label         text NOT NULL,
    period_start  date NOT NULL,
    period_end    date NOT NULL,
    run_id        uuid REFERENCES esg.calc_run(run_id),
    locked_by     text NOT NULL,
    locked_at     timestamptz NOT NULL DEFAULT now(),
    reason        text NOT NULL,
    unlocked_by   text,
    unlocked_at   timestamptz,
    unlock_reason text,
    CHECK (period_end >= period_start),
    CHECK ((unlocked_at IS NULL) = (unlocked_by IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_period_lock_active ON esg.reporting_period_lock (period_start, period_end) WHERE unlocked_at IS NULL;

CREATE OR REPLACE FUNCTION esg.enforce_period_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    col text := TG_ARGV[0];
    rec jsonb;
    v timestamptz;
    lk record;
BEGIN
    FOR rec IN
        SELECT to_jsonb(OLD) WHERE TG_OP IN ('UPDATE', 'DELETE')
        UNION ALL
        SELECT to_jsonb(NEW) WHERE TG_OP IN ('INSERT', 'UPDATE')
    LOOP
        v := (rec ->> col)::timestamptz;
        IF v IS NULL THEN CONTINUE; END IF;
        SELECT label, period_start, period_end INTO lk FROM esg.reporting_period_lock
         WHERE unlocked_at IS NULL AND v >= period_start::timestamptz AND v < (period_end + 1)::timestamptz LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'Period "%" (% to %) is locked: data dated % cannot change. An administrator can unlock it with a reason.',
                lk.label, lk.period_start, lk.period_end, v::date USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN COALESCE(NEW, OLD);
END $$;

DO $$
DECLARE t text[];
BEGIN
    FOREACH t SLICE 1 IN ARRAY ARRAY[
        ['fuel_transaction', 'ts'], ['refrigerant_event', 'event_date'], ['energy_meter_reading', 'ts'],
        ['spend_record', 'spend_date'], ['vessel_call', 'atb'], ['meter_register_reading', 'read_at']]
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_period_lock ON esg.%I', t[1]);
        EXECUTE format('CREATE TRIGGER trg_period_lock BEFORE INSERT OR UPDATE OR DELETE ON esg.%I '
                       'FOR EACH ROW EXECUTE FUNCTION esg.enforce_period_lock(%L)', t[1], t[2]);
    END LOOP;
END $$;

-- An emission factor behind a reported figure is never edited in place: add a new factor with a new valid_from instead.
CREATE OR REPLACE FUNCTION esg.protect_locked_factor() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM esg.ghg_emission e JOIN esg.reporting_period_lock l ON l.run_id = e.run_id AND l.unlocked_at IS NULL
                WHERE e.factor_id = OLD.factor_id) THEN
        RAISE EXCEPTION 'This emission factor is used by a locked reporting period. Add a new factor with a later valid_from instead of editing it.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
DROP TRIGGER IF EXISTS trg_protect_locked_factor ON esg.emission_factor;
CREATE TRIGGER trg_protect_locked_factor BEFORE UPDATE OR DELETE ON esg.emission_factor
    FOR EACH ROW EXECUTE FUNCTION esg.protect_locked_factor();

INSERT INTO esg.schema_migration (name) VALUES ('001_evidence_ocr.sql'), ('002_locks_links_audit.sql') ON CONFLICT DO NOTHING;

-- 003 ingest batches
CREATE TABLE IF NOT EXISTS esg.ingest_batch (
    batch_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name text NOT NULL,
    source_code text,
    filename text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    received int NOT NULL,
    inserted int NOT NULL,
    duplicates int NOT NULL DEFAULT 0,
    errors int NOT NULL DEFAULT 0,
    withdrawn_by text,
    withdrawn_at timestamptz,
    withdraw_reason text,
    withdrawn_count int
);
CREATE TABLE IF NOT EXISTS esg.ingest_batch_record (
    batch_id uuid NOT NULL REFERENCES esg.ingest_batch(batch_id) ON DELETE CASCADE,
    record_id text NOT NULL,
    PRIMARY KEY (batch_id, record_id)
);
INSERT INTO esg.schema_migration (name) VALUES ('003_ingest_batches.sql') ON CONFLICT DO NOTHING;

-- 004 equity share
ALTER TABLE esg.entity ADD COLUMN IF NOT EXISTS equity_share_pct numeric(18,4) NOT NULL DEFAULT 100;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'entity_equity_share_range') THEN
    ALTER TABLE esg.entity ADD CONSTRAINT entity_equity_share_range CHECK (equity_share_pct BETWEEN 0 AND 100);
  END IF;
END $$;
INSERT INTO esg.schema_migration (name) VALUES ('004_equity_share.sql') ON CONFLICT DO NOTHING;

-- 005 water
CREATE TABLE IF NOT EXISTS esg.water_record (
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
CREATE INDEX IF NOT EXISTS ix_water_record_entity_id ON esg.water_record (entity_id);
DROP TRIGGER IF EXISTS trg_period_lock ON esg.water_record;
CREATE TRIGGER trg_period_lock BEFORE INSERT OR UPDATE OR DELETE ON esg.water_record
    FOR EACH ROW EXECUTE FUNCTION esg.enforce_period_lock('period_start');
INSERT INTO esg.schema_migration (name) VALUES ('005_water.sql') ON CONFLICT DO NOTHING;

-- 006 direct-change audit
-- Audit changes made directly in the database (SQL console, scripts, other tools). The application's own connections set
-- esg.via_app = on and write richer audit rows themselves, so the trigger skips them and nothing is logged twice.
CREATE OR REPLACE FUNCTION esg.audit_direct() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o jsonb; n jsonb; pk text; rid text;
BEGIN
    IF current_setting('esg.via_app', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    o := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
    n := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
    pk := TG_ARGV[0];
    rid := COALESCE(n, o) ->> pk;
    INSERT INTO esg.audit_log (actor, action, table_name, record_id, before, after, detail)
    VALUES ('db:' || session_user, lower(TG_OP), TG_TABLE_NAME, rid, o, n, 'direct database change');
    RETURN COALESCE(NEW, OLD);
END $$;

DO $$
DECLARE t record;
BEGIN
    FOR t IN
        SELECT c.relname AS tbl, (SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                                   WHERE i.indrelid = c.oid AND i.indisprimary) AS pk
        FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
        WHERE s.nspname = 'esg' AND c.relkind = 'r'
          AND c.relname NOT IN ('audit_log', 'schema_migration', 'ingest_batch_record', 'ais_position', 'air_quality_reading',
                                'water_quality_reading', 'noise_reading', 'weather_observation', 'ghg_activity_data', 'ghg_emission',
                                'kpi_value', 'alert')
    LOOP
        CONTINUE WHEN t.pk IS NULL;
        EXECUTE format('DROP TRIGGER IF EXISTS trg_audit_direct ON esg.%I', t.tbl);
        EXECUTE format('CREATE TRIGGER trg_audit_direct AFTER INSERT OR UPDATE OR DELETE ON esg.%I '
                       'FOR EACH ROW EXECUTE FUNCTION esg.audit_direct(%L)', t.tbl, t.pk);
    END LOOP;
END $$;
INSERT INTO esg.schema_migration (name) VALUES ('006_db_audit.sql') ON CONFLICT DO NOTHING;

-- 007 anomaly rule type
ALTER TABLE esg.alert_rule DROP CONSTRAINT IF EXISTS alert_rule_rule_type_check;
ALTER TABLE esg.alert_rule ADD CONSTRAINT alert_rule_rule_type_check CHECK (rule_type IN (
    'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
    'workflow_backlog', 'kpi_off_track', 'missing_monthly', 'evidence_flagged', 'data_anomaly'));
INSERT INTO esg.schema_migration (name) VALUES ('007_anomaly_rule.sql') ON CONFLICT DO NOTHING;
