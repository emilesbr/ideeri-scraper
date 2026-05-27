-- =============================================================
-- Migration SIRENE — Enrichissement légal des entités
-- À exécuter dans le SQL Editor Supabase
-- Idempotent : IF NOT EXISTS partout
-- =============================================================

-- Données SIRENE (API recherche-entreprises.api.gouv.fr)
ALTER TABLE entites ADD COLUMN IF NOT EXISTS denomination_sociale        text;
ALTER TABLE entites ADD COLUMN IF NOT EXISTS libelle_forme_juridique     text;
ALTER TABLE entites ADD COLUMN IF NOT EXISTS code_ape                    text;
ALTER TABLE entites ADD COLUMN IF NOT EXISTS libelle_ape                 text;
ALTER TABLE entites ADD COLUMN IF NOT EXISTS adresse_siege               text;
ALTER TABLE entites ADD COLUMN IF NOT EXISTS effectif_salarie            text;  -- tranche : '01','02','03'...
ALTER TABLE entites ADD COLUMN IF NOT EXISTS date_creation_entreprise    date;

-- Contact (non fourni par SIRENE — à remplir depuis LBC boutique / SeLoger profil)
ALTER TABLE entites ADD COLUMN IF NOT EXISTS telephone                   text;

-- Traçabilité enrichissement
ALTER TABLE entites ADD COLUMN IF NOT EXISTS sirene_enrichi_at           timestamptz;

-- Index APE pour filtrer les entités hors immobilier
CREATE INDEX IF NOT EXISTS idx_entites_ape ON entites (code_ape) WHERE code_ape IS NOT NULL;
