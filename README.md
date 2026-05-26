# Ideeri — Pipeline de scraping immobilier

Pipeline Python qui scrape SeLoger et LeBonCoin pour cartographier les agences immobilières
actives sur un territoire, suivre leur volume de diffusion dans le temps, et détecter les
biens publiés sur plusieurs portails par la même entité ou par des entités concurrentes.

## Stack

- **Python 3.10+** — scraping, parsing, transform
- **ScrapingBee** — proxy anti-bot (premium pour SeLoger et LBC)
- **Supabase** (PostgreSQL) — stockage des annonces, entités, runs
- **Flask + Flask-CORS** — API locale pour le dashboard
- **lzstring** — décompression des données SeLoger (`__UFRN_FETCHER__`)

## Structure

```
ideeri/
├── pipeline.py           # Point d'entrée unique : scrape, transform, check, reset
├── transform.py          # Cœur du pipeline : stg_* → annonces + entites + runs
├── api.py                # API Flask → localhost:5000
├── dashboard.html        # Dashboard SPA à ouvrir dans le navigateur
│
├── test_ecully.py        # Validation des paramètres ScrapingBee (référence)
├── test_scrapingbee.py   # Benchmark 8 combinaisons de paramètres (A–H)
├── migration_v2.sql      # Schéma Supabase idempotent
├── .env.example          # Template des variables d'environnement
└── debug/                # HTML bruts ScrapingBee (gitignorés)
```

## Installation

```bash
git clone <repo>
cd ideeri
pip install supabase python-dotenv requests lzstring flask flask-cors
cp .env.example .env
# Remplir .env avec vos clés
```

## Variables d'environnement

```bash
SCRAPINGBEE_KEY=   # Clé API ScrapingBee (quota mensuel, reset le 1er)
SUPA_URL=          # https://<ref>.supabase.co
SUPA_KEY=          # Clé anon Supabase
```

## Utilisation

### 1. Scraper et transformer une commune (commande principale)

```bash
python3 pipeline.py run <code_postal> <commune> [--sl-code AD08FRXXXXX]

# Exemples
python3 pipeline.py run 69700 Givors --sl-code AD08FR28776
python3 pipeline.py run 42800 Rive-de-Gier
python3 pipeline.py run 69700 Givors --source lbc   # LBC uniquement
```

### 2. Autres commandes pipeline

```bash
python3 pipeline.py check                        # Quota ScrapingBee + derniers runs
python3 pipeline.py status <cp>                  # État d'une zone
python3 pipeline.py scrape <cp> <commune>        # Scraping seul (sans transform)
python3 pipeline.py transform <cp> <commune>     # Transform seul (données déjà en stg_*)
python3 pipeline.py reset <cp>                   # Réinitialise les données d'une zone
```

### 3. Dashboard

```bash
python3 api.py          # Lance l'API → http://localhost:5000
# Ouvrir dashboard.html dans le navigateur
```

### 4. Requêtes analytiques (Supabase SQL Editor)

```sql
-- Biens uniques sur la commune
SELECT COUNT(*) FROM biens_uniques WHERE code_postal = '69700';

-- Répartition portails
SELECT
  CASE WHEN sur_lbc AND sur_seloger THEN 'les deux'
       WHEN sur_lbc THEN 'lbc seul' ELSE 'seloger seul' END AS portail,
  COUNT(*) AS nb_biens
FROM biens_uniques WHERE code_postal = '69700' GROUP BY 1;

-- Top entités avec part de marché
SELECT nom_commercial,
  COUNT(DISTINCT bien_id) AS biens_uniques,
  COUNT(DISTINCT CASE WHEN source='lbc'     THEN bien_id END) AS lbc,
  COUNT(DISTINCT CASE WHEN source='seloger' THEN bien_id END) AS seloger
FROM annonces
WHERE code_postal = '69700' AND est_active = TRUE
GROUP BY nom_commercial ORDER BY biens_uniques DESC;
```

## Schéma de données

| Table | Rôle |
|-------|------|
| `stg_lbc` / `stg_seloger` | Staging brut — une ligne par page scrapée |
| `annonces` | Une ligne par annonce portail, suivi temporel (`est_active`, `historique_prix`) |
| `entites` | Une ligne par agence/particulier dédupliqué inter-portails via SIREN |
| `runs` | Journal de chaque exécution transform |
| `biens_uniques` (vue) | Un bien réel par ligne, flags portail agrégés, compatible DVF/ADEME |

## Résultats Givors (mai 2026 — 12 runs)

- **240 annonces actives** · **114 entités** · **136 biens uniques**
- 35 biens en multi-mandat (même bien, agences différentes) — 26% du parc
- Couverture DPE : ~90% · GES : ~28% (LBC uniquement)
