-- Migration : ajout is_exclusive, ref_agence, lat, lng, gps_precision dans annonces
-- Idempotente — safe à rejouer

ALTER TABLE annonces ADD COLUMN IF NOT EXISTS is_exclusive  boolean;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS ref_agence    text;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS lat           double precision;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS lng           double precision;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS gps_precision text;

-- Index pour la passe 0 matching (ref_agence) et pour H3 (is_exclusive)
CREATE INDEX IF NOT EXISTS idx_annonces_ref_agence
    ON annonces(ref_agence) WHERE ref_agence IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_annonces_is_exclusive
    ON annonces(is_exclusive) WHERE is_exclusive IS NOT NULL;

-- gps_precision : valeurs attendues 'streetNumber' | 'street' | 'district' | 'city' | null
-- Seules streetNumber et street sont utilisables pour jointure DVF par adresse.
-- LBC uniquement — SeLoger ne fournit pas de coordonnées en SERP.
