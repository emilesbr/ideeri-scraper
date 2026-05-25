# Ideeri — Pipeline de scraping immobilier

Pipeline Python qui scrape SeLoger et LeBonCoin pour cartographier les agences immobilières
actives sur un territoire, suivre leur volume de diffusion dans le temps, et détecter les
biens publiés sur plusieurs portails par la même entité.

## Stack

- **Python 3.10+** — scraping, parsing, transform
- **ScrapingBee** — proxy anti-bot (premium pour SeLoger, stealth pour LBC)
- **Supabase** (PostgreSQL) — stockage des annonces, entités, runs
- **lzstring** — décompression des données SeLoger (`__UFRN_FETCHER__`)

## Structure

```
ideeri/
├── scrape_givors.py      # Scraper opérationnel Givors — modèle par commune
├── transform.py          # Pipeline principal : stg_* → annonces + entites + runs
├── test_scrapingbee.py   # Benchmark des paramètres ScrapingBee
├── test_ecully.py        # Script de validation des paramètres (Écully)
├── migration_v2.sql      # Schéma Supabase idempotent
├── .env.example          # Template des variables d'environnement
└── debug/                # HTML bruts ScrapingBee (gitignorés)
```

## Installation

```bash
git clone <repo>
cd ideeri
pip install supabase python-dotenv requests lzstring
cp .env.example .env
# Remplir .env avec vos clés
```

## Variables d'environnement

```bash
SCRAPINGBEE_KEY=   # Clé API ScrapingBee
SUPA_URL=          # https://<ref>.supabase.co
SUPA_KEY=          # Clé anon ou service_role Supabase
```

## Utilisation

### 1. Scraper une commune

Copier `scrape_givors.py`, adapter les 6 constantes (COMMUNE, CODE_POSTAL, NB_PAGES_SL,
NB_PAGES_LBC, SL_BASE, LBC_BASE), puis :

```bash
python3 scrape_<commune>.py
```

### 2. Transformer les données en annonces structurées

```bash
python3 transform.py <code_postal> <commune>
# Exemple :
python3 transform.py 69700 Givors
```

### 3. Requêtes analytiques (Supabase SQL Editor)

```sql
-- Biens uniques sur la commune
SELECT COUNT(*) FROM biens_uniques WHERE code_postal = '69700';

-- Répartition portails
SELECT
  CASE WHEN sur_lbc AND sur_seloger THEN 'les deux'
       WHEN sur_lbc THEN 'lbc seul' ELSE 'seloger seul' END AS portail,
  COUNT(*) AS nb_biens
FROM biens_uniques WHERE code_postal = '69700' GROUP BY 1;

-- Top entités
SELECT nom_commercial, nb_annonces_actives, sur_lbc, sur_seloger
FROM entites ORDER BY nb_annonces_actives DESC LIMIT 20;
```

## Schéma de données

| Table | Rôle |
|-------|------|
| `stg_lbc` / `stg_seloger` | Staging brut — une ligne par page scrapée |
| `annonces` | Une ligne par annonce portail, suivi temporel (date_premiere_obs, est_active, historique_prix) |
| `entites` | Une ligne par agence/particulier dédupliqué inter-portails via SIREN |
| `runs` | Journal de chaque exécution transform |
| `biens_uniques` (vue) | Un bien réel par ligne, flags portail agrégés, compatible DVF/ADEME |

## Résultats Givors (run de référence)

- 231 annonces actives · 79 entités · 142 biens uniques
- 45 biens détectés sur les deux portails par fuzzy matching (score confiance 0.5–1.0)
- Couverture DPE : 78% (LBC 58% · SeLoger 97%)
