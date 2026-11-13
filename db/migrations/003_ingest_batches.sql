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
