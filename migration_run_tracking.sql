-- =============================================================
-- Migration run tracking — suivi des pages manquantes
-- À exécuter dans le SQL Editor Supabase
-- Idempotent : IF NOT EXISTS / OR REPLACE partout
-- =============================================================

-- Colonnes de suivi des pages en erreur
ALTER TABLE runs ADD COLUMN IF NOT EXISTS pages_erreur_lbc integer[];
ALTER TABLE runs ADD COLUMN IF NOT EXISTS pages_erreur_sl  integer[];
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lbc_total_attendu integer;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS sl_total_attendu  integer;

-- Ajout de 'running' au check constraint existant
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_statut_check;
ALTER TABLE runs ADD CONSTRAINT runs_statut_check
  CHECK (statut IN ('ok', 'partial', 'error', 'running'));

-- Index pour détecter rapidement les runs incomplets
CREATE INDEX IF NOT EXISTS idx_runs_statut ON runs (statut, code_postal)
  WHERE statut IN ('running', 'error');
