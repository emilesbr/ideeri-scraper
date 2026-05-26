-- =============================================================
-- Migration V2 — Schéma complet Ideeri avec suivi temporel
-- À exécuter dans le SQL Editor Supabase :
-- https://supabase.com/dashboard/project/qhehngzoeocjqpojymso/sql/new
-- Idempotent : IF NOT EXISTS / IF EXISTS partout
-- =============================================================


-- =============================================================
-- 1. TABLE runs — journal de chaque scraping
-- =============================================================
CREATE TABLE IF NOT EXISTS runs (
    id                   bigserial PRIMARY KEY,
    scraped_at           timestamptz NOT NULL DEFAULT now(),
    code_postal          text        NOT NULL,
    commune              text,
    source               text        NOT NULL CHECK (source IN ('lbc','seloger','all')),
    nb_annonces_trouvees integer     DEFAULT 0,
    nb_nouvelles         integer     DEFAULT 0,
    nb_disparues         integer     DEFAULT 0,
    nb_prix_modifies     integer     DEFAULT 0,
    duree_secondes       integer,
    statut               text        DEFAULT 'ok' CHECK (statut IN ('ok','partial','error'))
);

CREATE INDEX IF NOT EXISTS idx_runs_cp_date
    ON runs (code_postal, scraped_at DESC);


-- =============================================================
-- 2. TABLE entites — agences / particuliers sans doublon
-- =============================================================
CREATE TABLE IF NOT EXISTS entites (
    id                  uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    signature           text        UNIQUE NOT NULL,
    -- signature = 'siren_XXXXXXXXX' si SIREN connu
    --           = 'particulier_CP'  si particulier
    --           = 'nom_NORMNAME'    sinon

    nom_commercial      text        NOT NULL,
    nom_lbc             text,
    nom_seloger         text,
    type_entite         text        CHECK (type_entite IN ('agence','particulier','promoteur','inconnu')),

    -- Identifiants légaux (SIREN = 9 chiffres, SIRET = 14)
    siren               text,
    siret               text,

    -- Présence portails
    sur_lbc             boolean     DEFAULT false,
    sur_seloger         boolean     DEFAULT false,
    lbc_store_id        text,
    seloger_profile_url text,

    -- Couverture géographique
    codes_postaux       text[]      DEFAULT '{}',

    -- Suivi temporel
    date_premiere_obs   timestamptz,
    nb_annonces_actives integer     DEFAULT 0,
    est_client_ideeri   boolean     DEFAULT false,

    -- [{date, nb_lbc, nb_seloger, nb_total}]
    historique_activite jsonb       DEFAULT '[]'::jsonb,

    -- Fusion manuelle future d'entités dupliquées
    groupe_entite_id    uuid        REFERENCES entites(id)
);

CREATE INDEX IF NOT EXISTS idx_entites_signature  ON entites (signature);
CREATE INDEX IF NOT EXISTS idx_entites_siren      ON entites (siren) WHERE siren IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entites_cp         ON entites USING GIN (codes_postaux);


-- =============================================================
-- 3. TABLE annonces — ALTER pour ajouter les colonnes manquantes
-- =============================================================

-- Colonnes descriptives
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS titre        text;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS nb_pieces    integer;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS commune      text;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS url_annonce  text;

-- Suivi temporel des prix
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS historique_prix jsonb DEFAULT '[]'::jsonb;
-- [{date: "2026-05-25T14:00:00Z", prix: 180000}]

-- Lien vers le premier run qui a vu cette annonce
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS run_id_premiere_obs bigint REFERENCES runs(id);

-- Lien vers l'entité normalisée
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS entite_id uuid REFERENCES entites(id);

-- Index pour les requêtes analytiques courantes
CREATE INDEX IF NOT EXISTS idx_annonces_cp_active
    ON annonces (code_postal, est_active);

CREATE INDEX IF NOT EXISTS idx_annonces_source_cp
    ON annonces (source, code_postal);

CREATE INDEX IF NOT EXISTS idx_annonces_entite
    ON annonces (entite_id) WHERE entite_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_annonces_premiere_obs
    ON annonces (date_premiere_obs DESC);

CREATE INDEX IF NOT EXISTS idx_annonces_bien_id
    ON annonces (bien_id) WHERE bien_id IS NOT NULL;

ALTER TABLE annonces ADD COLUMN IF NOT EXISTS date_maj_portail date;
-- Date de dernière modification sur le portail SeLoger (metadata.updateDate).
-- Différente de date_derniere_obs (observation Ideeri) : permet de détecter
-- les rediffusions et baisses de prix sans nouveau run Ideeri.


-- =============================================================
-- 4. TABLES stg_lbc / stg_seloger — colonnes manquantes
-- =============================================================

ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS id          bigserial PRIMARY KEY;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS code_postal text;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS page        integer  DEFAULT 1;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS url_source  text;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS nb_annonces integer;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS processed   boolean  DEFAULT false;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS processed_at timestamptz;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='stg_lbc' AND column_name='date_scrap')
    AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name='stg_lbc' AND column_name='scraped_at')
    THEN ALTER TABLE stg_lbc RENAME COLUMN date_scrap TO scraped_at;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_stg_lbc_cp_date
    ON stg_lbc (code_postal, scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_stg_lbc_processed
    ON stg_lbc (processed) WHERE processed = false;


ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS id          bigserial PRIMARY KEY;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS code_postal text;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS page        integer  DEFAULT 1;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS url_source  text;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS nb_annonces integer;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS processed   boolean  DEFAULT false;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS processed_at timestamptz;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='stg_seloger' AND column_name='date_scrap')
    AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name='stg_seloger' AND column_name='scraped_at')
    THEN ALTER TABLE stg_seloger RENAME COLUMN date_scrap TO scraped_at;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_stg_seloger_cp_date
    ON stg_seloger (code_postal, scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_stg_seloger_processed
    ON stg_seloger (processed) WHERE processed = false;


-- =============================================================
-- Résultat final
-- =============================================================
-- runs        : id, scraped_at, code_postal, commune, source,
--               nb_annonces_trouvees, nb_nouvelles, nb_disparues,
--               nb_prix_modifies, duree_secondes, statut
--
-- entites     : id(uuid), signature, nom_commercial, nom_lbc, nom_seloger,
--               type_entite, siren, siret, sur_lbc, sur_seloger,
--               lbc_store_id, seloger_profile_url, codes_postaux,
--               date_premiere_obs, nb_annonces_actives, est_client_ideeri,
--               historique_activite, groupe_entite_id
--
-- annonces    : (existant) + titre, nb_pieces, commune, url_annonce,
--               historique_prix, run_id_premiere_obs, entite_id
--
-- stg_lbc     : id, scraped_at, code_postal, page, url_source,
--               nb_annonces, processed, processed_at, data_brute
--
-- stg_seloger : idem stg_lbc
