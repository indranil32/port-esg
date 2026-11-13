-- Legal and permit register
CREATE TABLE IF NOT EXISTS esg.legal_requirement (
    requirement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ref text NOT NULL UNIQUE,
    title text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('law', 'permit', 'standard', 'customer', 'other')),
    authority text,
    entity_id uuid REFERENCES esg.entity(entity_id),
    obligation text,
    owner text,
    permit_expiry date,
    review_due date,
    status text NOT NULL DEFAULT 'under_review' CHECK (status IN ('compliant', 'non_compliant', 'under_review')),
    last_evaluated date,
    evaluated_by text,
    evaluation_note text,
    doc_id uuid REFERENCES esg.evidence_document(doc_id),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Nonconformities with corrective action; corrective actions reuse ems_action
CREATE TABLE IF NOT EXISTS esg.nonconformity (
    nc_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nc_ref text NOT NULL UNIQUE,
    source text NOT NULL CHECK (source IN ('audit', 'incident', 'complaint', 'inspection', 'legal', 'other')),
    standard text CHECK (standard IN ('ISO14001', 'ISO50001')),
    entity_id uuid REFERENCES esg.entity(entity_id),
    description text NOT NULL,
    severity text NOT NULL DEFAULT 'minor' CHECK (severity IN ('minor', 'major')),
    root_cause text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'corrective_action', 'verification', 'closed')),
    due_date date,
    effectiveness_note text,
    raised_by text NOT NULL,
    raised_at timestamptz NOT NULL DEFAULT now(),
    closed_by text,
    closed_at timestamptz
);
ALTER TABLE esg.ems_action ALTER COLUMN objective_id DROP NOT NULL;
ALTER TABLE esg.ems_action ADD COLUMN IF NOT EXISTS nc_id uuid REFERENCES esg.nonconformity(nc_id);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ems_action_parent') THEN
    ALTER TABLE esg.ems_action ADD CONSTRAINT ems_action_parent CHECK (objective_id IS NOT NULL OR nc_id IS NOT NULL);
  END IF;
END $$;
-- Energy baselines (ISO 50001 EnPI): consumption modelled on an activity driver over a baseline period
CREATE TABLE IF NOT EXISTS esg.enpi_baseline (
    baseline_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    entity_id uuid NOT NULL REFERENCES esg.entity(entity_id),
    driver text NOT NULL CHECK (driver IN ('teu', 'vessel_calls', 'none')),
    baseline_start date NOT NULL,
    baseline_end date NOT NULL,
    intercept numeric NOT NULL,
    slope numeric NOT NULL,
    r2 numeric,
    n_months int NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE esg.alert_rule DROP CONSTRAINT IF EXISTS alert_rule_rule_type_check;
ALTER TABLE esg.alert_rule ADD CONSTRAINT alert_rule_rule_type_check CHECK (rule_type IN (
    'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
    'workflow_backlog', 'kpi_off_track', 'missing_monthly', 'evidence_flagged', 'data_anomaly', 'ems_overdue'));
