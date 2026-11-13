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
