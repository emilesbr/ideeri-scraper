-- Migration : ajout colonne commune dans stg_lbc et stg_seloger
-- Idempotent — à exécuter dans le SQL Editor Supabase
-- Permet au transform de distinguer les sessions par (code_postal, commune)

ALTER TABLE stg_lbc     ADD COLUMN IF NOT EXISTS commune text;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS commune text;

-- Index pour accélérer le filtre (cp, commune, scraped_at)
CREATE INDEX IF NOT EXISTS idx_stg_lbc_cp_commune
  ON stg_lbc (code_postal, commune, scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_stg_seloger_cp_commune
  ON stg_seloger (code_postal, commune, scraped_at DESC);
