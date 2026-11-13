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
