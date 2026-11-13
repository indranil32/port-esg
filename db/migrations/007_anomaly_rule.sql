ALTER TABLE esg.alert_rule DROP CONSTRAINT IF EXISTS alert_rule_rule_type_check;
ALTER TABLE esg.alert_rule ADD CONSTRAINT alert_rule_rule_type_check CHECK (rule_type IN (
    'threshold_reading', 'new_incident', 'data_freshness', 'expiry',
    'workflow_backlog', 'kpi_off_track', 'missing_monthly', 'evidence_flagged', 'data_anomaly'));
