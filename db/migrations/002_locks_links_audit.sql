-- 002: period locks, third-party collection links, request context on the audit log. Idempotent.
SET search_path = esg, public;

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
