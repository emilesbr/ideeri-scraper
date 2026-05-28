-- migration_insee.sql — idempotent
-- Ajoute code_insee à annonces + crée zones_ref (table de correspondance)

-- 1. Colonne code_insee sur annonces
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS code_insee text;
CREATE INDEX IF NOT EXISTS idx_annonces_code_insee ON annonces(code_insee);

-- 2. Table de correspondance zones_ref
CREATE TABLE IF NOT EXISTS zones_ref (
    code_insee        text PRIMARY KEY,
    cp                text NOT NULL,
    commune           text NOT NULL,      -- nom saisi par l'utilisateur
    commune_officielle text NOT NULL,     -- nom officiel INSEE (api-adresse)
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_zones_ref_cp ON zones_ref(cp);

-- 3. Trigger updated_at
CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_zones_ref_updated_at ON zones_ref;
CREATE TRIGGER trg_zones_ref_updated_at
    BEFORE UPDATE ON zones_ref
    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();
