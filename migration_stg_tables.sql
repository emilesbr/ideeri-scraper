-- Migration : ajout des colonnes manquantes sur stg_lbc et stg_seloger
-- À exécuter dans le SQL Editor de Supabase (dashboard > SQL Editor)
-- Idempotent : utilise IF NOT EXISTS / IF EXISTS

-- ============================================================
-- stg_lbc
-- ============================================================

-- PK auto-incrémentale (génère des valeurs pour les lignes existantes)
ALTER TABLE stg_lbc
    ADD COLUMN IF NOT EXISTS id BIGSERIAL PRIMARY KEY;

-- Métadonnées de scraping
ALTER TABLE stg_lbc
    ADD COLUMN IF NOT EXISTS code_postal TEXT;

ALTER TABLE stg_lbc
    ADD COLUMN IF NOT EXISTS page INTEGER DEFAULT 1;

ALTER TABLE stg_lbc
    ADD COLUMN IF NOT EXISTS url_source TEXT;

ALTER TABLE stg_lbc
    ADD COLUMN IF NOT EXISTS nb_annonces INTEGER;

-- Renommage pour cohérence (date_scrap → scraped_at)
-- Seulement si date_scrap existe et scraped_at n'existe pas encore
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stg_lbc' AND column_name = 'date_scrap'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stg_lbc' AND column_name = 'scraped_at'
    ) THEN
        ALTER TABLE stg_lbc RENAME COLUMN date_scrap TO scraped_at;
    END IF;
END $$;

-- Index pour les requêtes filtrées par CP et date
CREATE INDEX IF NOT EXISTS idx_stg_lbc_cp_date
    ON stg_lbc (code_postal, scraped_at DESC);

-- ============================================================
-- stg_seloger
-- ============================================================

ALTER TABLE stg_seloger
    ADD COLUMN IF NOT EXISTS id BIGSERIAL PRIMARY KEY;

ALTER TABLE stg_seloger
    ADD COLUMN IF NOT EXISTS code_postal TEXT;

ALTER TABLE stg_seloger
    ADD COLUMN IF NOT EXISTS page INTEGER DEFAULT 1;

ALTER TABLE stg_seloger
    ADD COLUMN IF NOT EXISTS url_source TEXT;

ALTER TABLE stg_seloger
    ADD COLUMN IF NOT EXISTS nb_annonces INTEGER;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stg_seloger' AND column_name = 'date_scrap'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stg_seloger' AND column_name = 'scraped_at'
    ) THEN
        ALTER TABLE stg_seloger RENAME COLUMN date_scrap TO scraped_at;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_stg_seloger_cp_date
    ON stg_seloger (code_postal, scraped_at DESC);

-- ============================================================
-- Résultat attendu après migration
-- ============================================================
-- stg_lbc     : id, scraped_at, code_postal, page, url_source, nb_annonces, data_brute
-- stg_seloger : id, scraped_at, code_postal, page, url_source, nb_annonces, data_brute
--
-- data_brute (JSONB) : désormais utilisé pour le JSON extrait
--   LBC     → { "ads": [...35 annonces...] }
--   SeLoger → { "classifieds_ids": [...], "classifieds_data": {...} }
