ALTER TABLE esg.entity ADD COLUMN IF NOT EXISTS equity_share_pct numeric(18,4) NOT NULL DEFAULT 100;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'entity_equity_share_range') THEN
    ALTER TABLE esg.entity ADD CONSTRAINT entity_equity_share_range CHECK (equity_share_pct BETWEEN 0 AND 100);
  END IF;
END $$;
