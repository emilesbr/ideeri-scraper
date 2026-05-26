-- =============================================================================
-- queries_reference.sql — Référentiel SQL complet Ideeri
-- Projet Supabase : qhehngzoeocjqpojymso
-- Dernière mise à jour : 2026-05-26
--
-- SECTIONS :
--   1. DDL — Schéma complet (tables, index, colonnes, vue)
--   2. Analytique — Requêtes standard sur une commune
--   3. Maintenance — Backfill, clustering, fuzzy matching
-- =============================================================================


-- =============================================================================
-- SECTION 1 — DDL
-- Crée ou met à jour le schéma Supabase.
-- Toutes les commandes sont idempotentes (IF NOT EXISTS / IF EXISTS).
-- À exécuter via le SQL Editor Supabase ou l'API Management (PAT).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1.1 Table runs — journal de chaque exécution du pipeline transform
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    id                   bigserial    PRIMARY KEY,
    scraped_at           timestamptz  NOT NULL DEFAULT now(),
    code_postal          text         NOT NULL,
    commune              text,
    source               text         NOT NULL CHECK (source IN ('lbc','seloger','all')),
    nb_annonces_trouvees integer      DEFAULT 0,
    nb_nouvelles         integer      DEFAULT 0,
    nb_disparues         integer      DEFAULT 0,
    nb_prix_modifies     integer      DEFAULT 0,
    duree_secondes       integer,
    statut               text         DEFAULT 'ok' CHECK (statut IN ('ok','partial','error'))
);

CREATE INDEX IF NOT EXISTS idx_runs_cp_date
    ON runs (code_postal, scraped_at DESC);


-- -----------------------------------------------------------------------------
-- 1.2 Table entites — une ligne par agence/particulier, sans doublon inter-portails
--
-- Clé de déduplication (signature) :
--   'siren_XXXXXXXXX'  si SIREN 9 chiffres connu (priorité max)
--   'particulier_CP'   si owner.type = 'private' sur LBC
--   'nom_NORMNAME'     sinon (nom normalisé sans stopwords immobiliers)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entites (
    id                  uuid         DEFAULT gen_random_uuid() PRIMARY KEY,
    signature           text         UNIQUE NOT NULL,
    nom_commercial      text         NOT NULL,
    nom_lbc             text,
    nom_seloger         text,
    type_entite         text         CHECK (type_entite IN ('agence','particulier','promoteur','inconnu')),
    siren               text,
    siret               text,
    sur_lbc             boolean      DEFAULT false,
    sur_seloger         boolean      DEFAULT false,
    lbc_store_id        text,
    seloger_profile_url text,
    codes_postaux       text[]       DEFAULT '{}',
    date_premiere_obs   timestamptz,
    nb_annonces_actives integer      DEFAULT 0,
    est_client_ideeri   boolean      DEFAULT false,
    historique_activite jsonb        DEFAULT '[]'::jsonb,
    -- [{date, nb_lbc, nb_seloger, nb_total}] — snapshot par run
    groupe_entite_id    uuid         REFERENCES entites(id)
    -- pour fusion manuelle de doublons résiduels
);

CREATE INDEX IF NOT EXISTS idx_entites_signature ON entites (signature);
CREATE INDEX IF NOT EXISTS idx_entites_siren     ON entites (siren) WHERE siren IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entites_cp        ON entites USING GIN (codes_postaux);


-- -----------------------------------------------------------------------------
-- 1.3 Table annonces — une ligne par annonce portail, append-only
--
-- Colonnes de base (existantes avant migration_v2) :
--   id_annonce, source, type_bien, prix_affiche, surface, code_postal,
--   date_premiere_obs, date_derniere_obs, est_active, bien_id, cluster_bien_id,
--   sur_lbc, sur_seloger, signature_entite_bien, nom_commercial
--
-- Colonnes ajoutées en migration V2 :
-- -----------------------------------------------------------------------------
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS titre               text;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS nb_pieces           integer;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS commune             text;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS url_annonce         text;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS historique_prix     jsonb  DEFAULT '[]'::jsonb;
-- [{date: "2026-05-25T14:00:00Z", prix: 180000, ancien_prix: 185000}]
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS run_id_premiere_obs bigint REFERENCES runs(id);
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS entite_id           uuid   REFERENCES entites(id);

-- Colonnes ajoutées post-V2 (enrichissement DPE / clustering) :
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS dpe                 text;
-- Classe DPE : A–G ou N (neuf/VEFA). LBC: attr energy_rate. SeLoger: energyClass.
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS ges                 text;
-- Classe GES : A–G. LBC uniquement (attr ges). SeLoger ne l'expose pas en SERP.
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS match_confidence    float;
-- Score intra_entity_match (seuil 0.70, LBC↔SeLoger même entité)
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS energie_budget_min  integer;
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS energie_budget_max  integer;
-- Coût énergétique annuel estimé en euros (LBC: annual_energy_budget_min/max)
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS annee_construction  integer;
-- LBC: attr building_year
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS etage               integer;
-- LBC: attr floor_number
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS bien_id             text;
-- Identifiant canonique du bien : id_annonce (non matché), id_annonce LBC (intra_entity), cluster_id hex (cross_entity)
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS cluster_bien_id     text;
-- ID du cluster cross-entités (cross_entity_match). NULL si bien en mandat unique.
ALTER TABLE annonces ADD COLUMN IF NOT EXISTS cluster_confidence  float;
-- Score moyen du cluster cross-entités

CREATE INDEX IF NOT EXISTS idx_annonces_cp_active   ON annonces (code_postal, est_active);
CREATE INDEX IF NOT EXISTS idx_annonces_source_cp   ON annonces (source, code_postal);
CREATE INDEX IF NOT EXISTS idx_annonces_entite       ON annonces (entite_id)       WHERE entite_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_annonces_premiere_obs ON annonces (date_premiere_obs DESC);
CREATE INDEX IF NOT EXISTS idx_annonces_bien_id      ON annonces (bien_id)         WHERE bien_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_annonces_cluster      ON annonces (cluster_bien_id) WHERE cluster_bien_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- 1.4 Tables stg_lbc / stg_seloger — staging brut (une ligne par page scrapée)
-- -----------------------------------------------------------------------------
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS id           bigserial    PRIMARY KEY;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS code_postal  text;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS page         integer      DEFAULT 1;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS url_source   text;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS nb_annonces  integer;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS processed    boolean      DEFAULT false;
ALTER TABLE stg_lbc ADD COLUMN IF NOT EXISTS processed_at timestamptz;

ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS id           bigserial    PRIMARY KEY;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS code_postal  text;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS page         integer      DEFAULT 1;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS url_source   text;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS nb_annonces  integer;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS processed    boolean      DEFAULT false;
ALTER TABLE stg_seloger ADD COLUMN IF NOT EXISTS processed_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_stg_lbc_cp_date       ON stg_lbc     (code_postal, scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_stg_seloger_cp_date   ON stg_seloger (code_postal, scraped_at DESC);


-- -----------------------------------------------------------------------------
-- 1.5 Vue biens_uniques — un bien réel par ligne, tous portails agrégés
--
-- Logique de groupement :
--   bien_id — défini par le pipeline de matching dans transform.py :
--     • id_annonce (non matché)
--     • id_annonce LBC (intra_entity_match : LBC↔SeLoger même entité, seuil 0.70)
--     • cluster_id hex (cross_entity_match : multi-entités, seuil 0.80, Union-Find DPE-safe)
--
-- Flags :
--   multi_mandat     = plusieurs entités différentes diffusent ce bien
--   doublon_interne  = même entité, plusieurs fiches (LBC + SeLoger)
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS biens_uniques;
CREATE VIEW biens_uniques AS
SELECT
    bien_id,
    code_postal,
    MIN(commune)                                      AS commune,
    MIN(type_bien)                                    AS type_bien,
    ROUND(AVG(surface::numeric), 1)                  AS surface,
    MIN(prix_affiche)                                 AS prix_min,
    MAX(prix_affiche)                                 AS prix_max,
    bool_or(source = 'lbc')                          AS sur_lbc,
    bool_or(source = 'seloger')                      AS sur_seloger,
    COUNT(*)                                          AS nb_annonces,
    COUNT(DISTINCT entite_id)                         AS nb_entites,
    array_agg(DISTINCT nom_commercial)                AS entites,
    MIN(dpe)                                          AS dpe,
    MIN(ges)                                          AS ges,
    MIN(annee_construction)                           AS annee_construction,
    (COUNT(DISTINCT entite_id) > 1)                  AS multi_mandat,
    (COUNT(DISTINCT entite_id) = 1 AND COUNT(*) > 1) AS doublon_interne
FROM annonces
WHERE est_active = TRUE
  AND bien_id IS NOT NULL
GROUP BY bien_id, code_postal;


-- =============================================================================
-- SECTION 2 — ANALYTIQUE
-- Requêtes standard à lancer dans le SQL Editor Supabase.
-- Remplacer '69700' par le code postal de la commune cible.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 2.1 Vue d'ensemble : biens uniques, répartition portails, DPE
-- -----------------------------------------------------------------------------

-- Nombre total de biens uniques
SELECT COUNT(*) AS nb_biens_uniques
FROM biens_uniques
WHERE code_postal = '69700';


-- Répartition portails (par bien unique)
SELECT
    CASE
        WHEN sur_lbc AND sur_seloger AND nb_entites = 1 THEN 'doublon_interne'
        WHEN sur_lbc AND sur_seloger AND nb_entites > 1 THEN 'multi_mandat_deux_portails'
        WHEN sur_lbc AND sur_seloger                    THEN 'les_deux'
        WHEN sur_lbc                                    THEN 'lbc_seul'
        ELSE                                                 'seloger_seul'
    END                                      AS categorie,
    COUNT(*)                                 AS nb_biens,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM biens_uniques
WHERE code_postal = '69700'
GROUP BY 1
ORDER BY 2 DESC;


-- Couverture DPE par source
SELECT
    source,
    COUNT(*) FILTER (WHERE dpe IS NOT NULL) AS avec_dpe,
    COUNT(*)                                AS total,
    ROUND(100.0 * COUNT(*) FILTER (WHERE dpe IS NOT NULL) / COUNT(*), 1) AS pct_dpe
FROM annonces
WHERE code_postal = '69700' AND est_active = TRUE
GROUP BY source;


-- Distribution DPE sur les biens uniques
SELECT
    dpe,
    COUNT(*)                                          AS nb_biens,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM biens_uniques
WHERE code_postal = '69700' AND dpe IS NOT NULL
GROUP BY dpe
ORDER BY dpe;


-- -----------------------------------------------------------------------------
-- 2.2 Analyse des mandats (logique métier)
--
-- Un mandat = une relation entité × bien.
-- Si une même entité diffuse le même bien sur LBC ET SeLoger = 1 mandat.
-- -----------------------------------------------------------------------------

-- Décomposition complète biens uniques × diffusion
SELECT
    COUNT(DISTINCT bien_id)                                              AS nb_biens_uniques_total,
    COUNT(DISTINCT CASE WHEN nb_entites = 1 THEN bien_id END)           AS diffuses_par_1_entite,
    COUNT(DISTINCT CASE WHEN nb_entites > 1 THEN bien_id END)           AS diffuses_par_plusieurs_entites,
    COUNT(DISTINCT CASE WHEN sur_lbc AND sur_seloger AND nb_entites = 1
                        THEN bien_id END)                                AS meme_entite_deux_portails,
    COUNT(DISTINCT CASE WHEN sur_lbc AND sur_seloger AND nb_entites > 1
                        THEN bien_id END)                                AS entites_differentes_deux_portails,
    COUNT(DISTINCT CASE WHEN sur_lbc AND NOT sur_seloger
                        THEN bien_id END)                                AS lbc_seulement,
    COUNT(DISTINCT CASE WHEN sur_seloger AND NOT sur_lbc
                        THEN bien_id END)                                AS seloger_seulement
FROM biens_uniques
WHERE code_postal = '69700';


-- Nombre de mandats réels (entité × bien, un seul par entité même sur 2 portails)
SELECT
    COUNT(*)                                            AS nb_mandats_total,
    COUNT(CASE WHEN sur_lbc AND NOT sur_seloger THEN 1 END) AS mandats_lbc_seul,
    COUNT(CASE WHEN sur_seloger AND NOT sur_lbc THEN 1 END) AS mandats_seloger_seul,
    COUNT(CASE WHEN sur_lbc AND sur_seloger     THEN 1 END) AS mandats_deux_portails
FROM (
    SELECT
        a.entite_id,
        a.bien_id,
        bool_or(a.source = 'lbc')     AS sur_lbc,
        bool_or(a.source = 'seloger') AS sur_seloger
    FROM annonces a
    WHERE a.code_postal = '69700'
      AND a.est_active = TRUE
      AND a.entite_id IS NOT NULL
    GROUP BY a.entite_id, a.bien_id
) mandats;


-- -----------------------------------------------------------------------------
-- 2.3 Classements entités
-- -----------------------------------------------------------------------------

-- Top entités par nombre de biens uniques
SELECT
    nom_commercial,
    COUNT(DISTINCT a.bien_id)                                     AS biens_uniques,
    COUNT(DISTINCT CASE WHEN source = 'lbc'     THEN a.id_annonce END) AS ann_lbc,
    COUNT(DISTINCT CASE WHEN source = 'seloger' THEN a.id_annonce END) AS ann_seloger,
    e.sur_lbc,
    e.sur_seloger,
    e.type_entite,
    e.siren
FROM annonces a
JOIN entites e ON e.id = a.entite_id
WHERE a.code_postal = '69700' AND a.est_active = TRUE
GROUP BY a.nom_commercial, e.sur_lbc, e.sur_seloger, e.type_entite, e.siren
ORDER BY biens_uniques DESC
LIMIT 20;


-- Biens en multi-mandat : détail des entités par bien
SELECT
    b.bien_id,
    b.type_bien,
    b.surface,
    b.prix_min,
    b.nb_entites,
    b.sur_lbc,
    b.sur_seloger,
    b.entites
FROM biens_uniques b
WHERE b.code_postal = '69700'
  AND b.multi_mandat = TRUE
ORDER BY b.nb_entites DESC, b.surface DESC;


-- Doublons internes (même entité, LBC + SeLoger)
SELECT
    b.bien_id,
    b.type_bien,
    b.surface,
    b.prix_min,
    b.nb_annonces,
    (b.entites)[1] AS entite,
    b.dpe
FROM biens_uniques b
WHERE b.code_postal = '69700'
  AND b.doublon_interne = TRUE
ORDER BY b.nb_annonces DESC;


-- -----------------------------------------------------------------------------
-- 2.4 Suivi temporel
-- -----------------------------------------------------------------------------

-- Évolution de l'activité d'une entité run par run
SELECT
    (h->>'date')::date   AS date_run,
    (h->>'nb_lbc')::int  AS nb_lbc,
    (h->>'nb_seloger')::int AS nb_seloger,
    (h->>'nb_total')::int   AS nb_total
FROM entites,
     jsonb_array_elements(historique_activite) AS h
WHERE nom_commercial = 'LEONE IMMOBILIER'   -- à adapter
ORDER BY 1;


-- Historique des prix d'une annonce
SELECT
    id_annonce,
    titre,
    prix_affiche AS prix_actuel,
    (h->>'date')::date    AS date_changement,
    (h->>'prix')::int     AS nouveau_prix,
    (h->>'ancien_prix')::int AS ancien_prix
FROM annonces,
     jsonb_array_elements(historique_prix) AS h
WHERE id_annonce = 'seloger_123456789'   -- à adapter
ORDER BY (h->>'date') DESC;


-- Nouvelles annonces détectées à chaque run
SELECT
    r.scraped_at::date AS date_run,
    r.commune,
    r.nb_nouvelles,
    r.nb_disparues,
    r.nb_prix_modifies,
    r.nb_annonces_trouvees,
    r.duree_secondes
FROM runs r
WHERE r.code_postal = '69700'
ORDER BY r.scraped_at DESC;


-- -----------------------------------------------------------------------------
-- 2.5 Enrichissement DPE/GES — préparation croisement ADEME
--
-- L'API ADEME DPE : https://data.ademe.fr/datasets/dpe-v2-logements-existants
-- Clé de jointure : dpe + ges + surface ± 5m² + code_postal + type_bien
--                   + annee_construction ± 5 ans (si renseigné)
-- Objectif : récupérer l'adresse exacte pour le croisement DVF.
-- -----------------------------------------------------------------------------

-- Biens ADEME-prêts (DPE + GES + surface renseignés)
SELECT
    b.bien_id,
    b.code_postal,
    b.commune,
    b.type_bien,
    b.surface,
    b.prix_min,
    b.prix_max,
    b.dpe,
    b.ges,
    b.annee_construction,
    b.sur_lbc,
    b.sur_seloger,
    b.multi_mandat
FROM biens_uniques b
WHERE b.code_postal = '69700'
  AND b.dpe IS NOT NULL
  AND b.ges IS NOT NULL
ORDER BY b.surface;


-- Couverture enrichissement (biens avec données ADEME suffisantes)
SELECT
    COUNT(*)                                           AS nb_biens_total,
    COUNT(*) FILTER (WHERE dpe IS NOT NULL)            AS avec_dpe,
    COUNT(*) FILTER (WHERE ges IS NOT NULL)            AS avec_ges,
    COUNT(*) FILTER (WHERE annee_construction IS NOT NULL) AS avec_annee,
    COUNT(*) FILTER (WHERE dpe IS NOT NULL AND ges IS NOT NULL) AS ademe_ready
FROM biens_uniques
WHERE code_postal = '69700';


-- =============================================================================
-- SECTION 3 — MAINTENANCE
-- Requêtes à exécuter ponctuellement après migration ou pour corriger des données.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 3.1 Vérification de cohérence post-transform
-- -----------------------------------------------------------------------------

-- Annonces sans entite_id (ne devrait pas exister après un run propre)
SELECT COUNT(*), source
FROM annonces
WHERE est_active = TRUE AND entite_id IS NULL
GROUP BY source;

-- Entités sans annonce active (orphelines après disparitions)
SELECT e.nom_commercial, e.signature, e.nb_annonces_actives
FROM entites e
WHERE e.nb_annonces_actives = 0
ORDER BY e.date_premiere_obs DESC
LIMIT 20;

-- Doublons d'entités non fusionnés (même SIREN, deux lignes)
SELECT siren, COUNT(*) AS nb_lignes, array_agg(nom_commercial) AS noms
FROM entites
WHERE siren IS NOT NULL
GROUP BY siren
HAVING COUNT(*) > 1;

-- Annonces matchées intra-entity sans bien_id aligné (sanity check)
SELECT COUNT(*)
FROM annonces
WHERE match_confidence IS NOT NULL
  AND sur_lbc = TRUE AND sur_seloger = TRUE
  AND bien_id = id_annonce;  -- bien_id non aligné = paire non trouvée


-- -----------------------------------------------------------------------------
-- 3.2 Backfill DPE/GES sur les annonces existantes
--
-- À relancer si parse_lbc_ads() ou parse_seloger_ads() a été amélioré.
-- Fait via Python (transform.py expose parse_lbc_ads / parse_seloger_ads).
-- Voir section 8 du CLAUDE.md pour le snippet complet.
-- -----------------------------------------------------------------------------

-- Identifier les annonces LBC sans DPE (pour prioriser le backfill)
SELECT
    a.id_annonce,
    a.titre,
    a.surface,
    a.prix_affiche,
    a.code_postal,
    a.date_premiere_obs
FROM annonces a
WHERE a.source = 'lbc'
  AND a.est_active = TRUE
  AND a.dpe IS NULL
ORDER BY a.code_postal, a.date_premiere_obs DESC;


-- -----------------------------------------------------------------------------
-- 3.3 Réinitialisation complète du matching (intra + cross)
--
-- À utiliser si les seuils de compute_match_score() ont changé,
-- ou après ajout de nouvelles annonces nécessitant un recalcul complet.
-- Après ce reset, relancer via Python :
--   from transform import intra_entity_match, cross_entity_match
--   intra_entity_match('69700')
--   cross_entity_match('69700')
-- -----------------------------------------------------------------------------

-- Étape 1 : reset des flags portails et champs matching
UPDATE annonces
SET sur_lbc          = (source = 'lbc'),
    sur_seloger      = (source = 'seloger'),
    match_confidence  = NULL,
    cluster_bien_id   = NULL,
    cluster_confidence = NULL
WHERE code_postal = '69700'   -- à adapter
  AND est_active = TRUE;

-- Étape 2 : réinitialiser bien_id = id_annonce (SQL pur)
UPDATE annonces
SET bien_id = id_annonce
WHERE code_postal = '69700'   -- à adapter
  AND est_active = TRUE;


-- -----------------------------------------------------------------------------
-- 3.5 Marquer les pages stg_* comme traitées (non encore implémenté dans Python)
-- -----------------------------------------------------------------------------

UPDATE stg_lbc
SET processed = TRUE, processed_at = now()
WHERE processed = FALSE
  AND (data_brute->'_meta'->>'code_postal' = '69700'
       OR code_postal = '69700');

UPDATE stg_seloger
SET processed = TRUE, processed_at = now()
WHERE processed = FALSE
  AND (data_brute->'_meta'->>'code_postal' = '69700'
       OR code_postal = '69700');


-- -----------------------------------------------------------------------------
-- 3.6 Fusion manuelle de deux entités dupliquées
--
-- Cas typique : même agence, une fiche avec SIREN (LBC) et une sans (SeLoger).
-- Étapes :
--   1. Identifier les deux IDs
--   2. Rattacher toutes les annonces de l'entité B vers l'entité A
--   3. Supprimer l'entité B (ou la marquer via groupe_entite_id)
-- -----------------------------------------------------------------------------

-- Étape 1 : trouver les candidats à fusionner
SELECT id, signature, nom_commercial, nom_lbc, nom_seloger, siren, nb_annonces_actives
FROM entites
WHERE nom_commercial ILIKE '%century 21%'   -- à adapter
ORDER BY nb_annonces_actives DESC;

-- Étape 2 : rattacher les annonces (remplacer les UUIDs)
UPDATE annonces
SET entite_id = 'UUID_ENTITE_A'::uuid   -- à remplacer
WHERE entite_id = 'UUID_ENTITE_B'::uuid; -- à remplacer

-- Étape 3 : marquer B comme fusionnée dans A (audit trail)
UPDATE entites
SET groupe_entite_id = 'UUID_ENTITE_A'::uuid,
    nb_annonces_actives = 0
WHERE id = 'UUID_ENTITE_B'::uuid;
