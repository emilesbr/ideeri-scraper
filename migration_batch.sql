-- migration_batch.sql — idempotent
-- Ajoute actif + priorite + sl_code + lbc_loc à zones_ref pour le mode batch

ALTER TABLE zones_ref ADD COLUMN IF NOT EXISTS actif    boolean DEFAULT false;
ALTER TABLE zones_ref ADD COLUMN IF NOT EXISTS priorite integer DEFAULT 0;
ALTER TABLE zones_ref ADD COLUMN IF NOT EXISTS sl_code  text;
ALTER TABLE zones_ref ADD COLUMN IF NOT EXISTS lbc_loc  text;

-- Insérer les 9 arrondissements de Lyon (codes INSEE officiels 69381–69389)
-- Format commune = format INSEE officiel
INSERT INTO zones_ref (code_insee, cp, commune, commune_officielle, actif, priorite)
VALUES
  ('69381', '69001', 'Lyon 1er Arrondissement', 'Lyon 1er Arrondissement', true, 1),
  ('69382', '69002', 'Lyon 2e Arrondissement',  'Lyon 2e Arrondissement',  true, 2),
  ('69383', '69003', 'Lyon 3e Arrondissement',  'Lyon 3e Arrondissement',  true, 3),
  ('69384', '69004', 'Lyon 4e Arrondissement',  'Lyon 4e Arrondissement',  true, 4),
  ('69385', '69005', 'Lyon 5e Arrondissement',  'Lyon 5e Arrondissement',  true, 5),
  ('69386', '69006', 'Lyon 6e Arrondissement',  'Lyon 6e Arrondissement',  true, 6),
  ('69387', '69007', 'Lyon 7e Arrondissement',  'Lyon 7e Arrondissement',  true, 7),
  ('69388', '69008', 'Lyon 8e Arrondissement',  'Lyon 8e Arrondissement',  true, 8),
  ('69389', '69009', 'Lyon 9e Arrondissement',  'Lyon 9e Arrondissement',  true, 9)
ON CONFLICT (code_insee) DO UPDATE SET
  actif              = EXCLUDED.actif,
  priorite           = EXCLUDED.priorite,
  commune            = EXCLUDED.commune,
  commune_officielle = EXCLUDED.commune_officielle;
