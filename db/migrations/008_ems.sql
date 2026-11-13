CREATE TABLE IF NOT EXISTS esg.ems_objective (
    objective_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    standard text NOT NULL CHECK (standard IN ('ISO14001', 'ISO50001')),
    title text NOT NULL,
    entity_id uuid REFERENCES esg.entity(entity_id),
    metric_id uuid REFERENCES esg.metric(metric_id),
    baseline_value numeric(18,4),
    target_value numeric(18,4),
    target_date date,
    owner text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'achieved', 'cancelled')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS esg.ems_action (
    action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    objective_id uuid NOT NULL REFERENCES esg.ems_objective(objective_id),
    title text NOT NULL,
    owner text,
    due_date date,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'done')),
    completed_at timestamptz,
    note text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS esg.ems_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_ref text NOT NULL UNIQUE,
    standard text NOT NULL CHECK (standard IN ('ISO14001', 'ISO50001')),
    scope text,
    planned_date date NOT NULL,
    performed_date date,
    auditor text,
    status text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'done')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS esg.ems_review (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    standard text NOT NULL CHECK (standard IN ('ISO14001', 'ISO50001')),
    review_date date NOT NULL,
    attendees text,
    decisions text,
    snapshot jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE esg.alert_rule DROP CONSTRAINT IF EXISTS alert_rule_rule_type_check;
ALTER TABLE esg.alert_rule ADD CONSTRAINT alert_rule_rule_type_check CHECK (rule_type IN (
    'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
    'workflow_backlog', 'kpi_off_track', 'missing_monthly', 'evidence_flagged', 'data_anomaly', 'ems_overdue'));
