-- Archive of every original file loaded into the system (CSV loads and reference imports).
-- Rows are append-only: the file, its hash and who sent it can never be changed or removed.
CREATE TABLE IF NOT EXISTS esg.source_file (
    file_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('csv_ingest', 'reference_import')),
    target text,
    filename text NOT NULL,
    content_type text,
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    file_uri text NOT NULL,
    uploaded_by text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    batch_id uuid REFERENCES esg.ingest_batch(batch_id)
);
CREATE INDEX IF NOT EXISTS source_file_batch_idx ON esg.source_file (batch_id);
CREATE OR REPLACE FUNCTION esg.source_file_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'source_file rows cannot be deleted'; END IF;
    IF NEW.file_id IS DISTINCT FROM OLD.file_id OR NEW.filename IS DISTINCT FROM OLD.filename OR NEW.sha256 IS DISTINCT FROM OLD.sha256
       OR NEW.file_uri IS DISTINCT FROM OLD.file_uri OR NEW.uploaded_by IS DISTINCT FROM OLD.uploaded_by
       OR NEW.uploaded_at IS DISTINCT FROM OLD.uploaded_at OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
       OR OLD.batch_id IS NOT NULL THEN
        RAISE EXCEPTION 'source_file rows are append-only';
    END IF;
    RETURN NEW;   -- the only allowed change: linking the file to its batch once
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS source_file_guard ON esg.source_file;
CREATE TRIGGER source_file_guard BEFORE UPDATE OR DELETE ON esg.source_file FOR EACH ROW EXECUTE FUNCTION esg.source_file_guard();
