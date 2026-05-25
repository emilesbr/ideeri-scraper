# Ideeri — Scraping Immobilier

## 1. Contexte métier

**Qui** : Ideeri, éditeur SaaS immobilier (emile.soubeyrand@ideeri.fr).

**Quoi** : Pipeline de scraping SeLoger + LeBonCoin pour identifier les agences immobilières
actives sur un territoire, mesurer leur volume de diffusion par portail, et détecter les biens
diffusés en exclusivité ou en multi-diffusion.

**Pourquoi** : Construire une base de données commerciale des entités actives par commune,
avec suivi temporel (plusieurs runs successifs = série temporelle). La cible de prospection
est **l'agence indépendante** qui n'est pas encore cliente Ideeri. L'analyse "différence en
différences" compare la situation avant / après l'entrée d'un territoire dans le réseau Ideeri.

**Croisements futurs prévus** :
- **DVF (DGFIP)** — ventes immobilières notariées, pour croiser avec les biens scrapés
  et mesurer les délais et taux de vente des agences.
- **ADEME base DPE** — pour enrichir les biens avec les diagnostics énergétiques officiels
  et vérifier la cohérence avec les DPE déclarés dans les annonces.

---

## 2. Architecture technique

```
ideeri/
├── .env                    # Clés API (ne jamais committer)
├── .env.example            # Template à committer
├── CLAUDE.md               # Ce fichier — lu à chaque session
├── README.md               # Vue d'ensemble GitHub
│
├── scrape_givors.py        # Scraper câblé Givors (69700) — modèle à copier par commune
├── test_ecully.py          # Scraper de test Écully (69130) — a validé les paramètres SB
├── test_scrapingbee.py     # Benchmark des paramètres ScrapingBee (4 tests SL + 4 LBC)
│
├── transform.py            # Transformateur stg_* → annonces + entites + runs (PIPELINE PRINCIPAL)
│
├── migration_v2.sql        # SQL idempotent : crée runs, entites, alter annonces/stg_*
├── migration_stg_tables.sql # SQL intermédiaire (peut être ignoré — précède migration_v2)
│
├── analyser_local.py       # Analyse des HTML capturés en local (débogage)
├── debug_page.py           # Utilitaire de debug d'une page HTML brute
├── extractor_exhaustive.py # Extracteur exhaustif (brouillon, remplacé par transform.py)
├── scraper_hybride.py      # Brouillon de scraper hybride (remplacé par scrape_givors.py)
├── capture_seloger.html    # Capture HTML SeLoger de référence (débogage)
│
└── debug/                  # HTML bruts des requêtes ScrapingBee (gitignorés)
    ├── seloger_givors_p1.html
    ├── lbc_givors_p1.html
    └── ...
```

### Rôle détaillé des fichiers actifs

**`scrape_givors.py`** — Scraper opérationnel pour Givors (69700). Hardcodé : 4 pages SeLoger
+ 4 pages LBC en parallèle (max 5 workers ThreadPoolExecutor). Insère dans `stg_lbc` et
`stg_seloger` avec les métadonnées dans `data_brute._meta`. C'est le modèle à copier pour
toute nouvelle commune.

**`test_ecully.py`** — Script de référence qui a validé les paramètres ScrapingBee (stealth
pour LBC, premium pour SeLoger). Architecture identique à `scrape_givors.py`.

**`test_scrapingbee.py`** — Benchmark de 8 combinaisons de paramètres (A–H). À relancer si
la clé API change ou si un portail modifie son anti-bot.

**`transform.py`** — **Le cœur du pipeline**. Lit `stg_lbc` et `stg_seloger`, normalise les
annonces, gère les entités sans doublon, le suivi temporel, le DPE/GES, et le matching
inter-portail. Usage : `python3 transform.py <code_postal> [commune]`

**`migration_v2.sql`** — Migration idempotente à exécuter via le SQL Editor Supabase ou
l'API Management (PAT requis, voir section 10). Crée `runs`, `entites`, alters `annonces`
et `stg_*`.

---

## 3. Schéma Supabase complet

**Projet** : `qhehngzoeocjqpojymso` — URL : `https://qhehngzoeocjqpojymso.supabase.co`

### Table `runs`
Journal de chaque exécution du transform.

| Colonne               | Type        | Description |
|-----------------------|-------------|-------------|
| `id`                  | bigserial PK | Auto-incrémenté |
| `scraped_at`          | timestamptz | Timestamp du run |
| `code_postal`         | text        | CP de la commune traitée |
| `commune`             | text        | Nom de la commune |
| `source`              | text        | `'lbc'`, `'seloger'`, ou `'all'` |
| `nb_annonces_trouvees`| integer     | Total lu dans stg_* |
| `nb_nouvelles`        | integer     | Nouvelles insertions annonces |
| `nb_disparues`        | integer     | Annonces passées `est_active=false` |
| `nb_prix_modifies`    | integer     | Changements de prix détectés |
| `duree_secondes`      | integer     | Durée d'exécution |
| `statut`              | text        | `'ok'`, `'partial'`, `'error'` |

### Table `entites`
Une ligne par agence ou particulier, sans doublon inter-portails.

| Colonne               | Type        | Description |
|-----------------------|-------------|-------------|
| `id`                  | uuid PK     | `gen_random_uuid()` |
| `signature`           | text UNIQUE | Clé de dédup : `siren_XXXXXXXXX` > `particulier_CP` > `nom_NORMNAME` |
| `nom_commercial`      | text        | Nom canonique retenu |
| `nom_lbc`             | text        | Nom tel qu'affiché sur LBC |
| `nom_seloger`         | text        | Nom tel qu'affiché sur SeLoger |
| `type_entite`         | text        | `'agence'`, `'particulier'`, `'promoteur'`, `'inconnu'` |
| `siren`               | text        | SIREN 9 chiffres (LBC : `owner.siren`, SeLoger : regex SIRET) |
| `siret`               | text        | SIRET 14 chiffres (SeLoger `agencyLegalInformations`) |
| `sur_lbc`             | boolean     | Présent sur LeBonCoin |
| `sur_seloger`         | boolean     | Présent sur SeLoger |
| `lbc_store_id`        | text        | ID store LBC (`owner.store_id`) |
| `seloger_profile_url` | text        | URL profil agence SeLoger |
| `codes_postaux`       | text[]      | CPs où l'entité a été observée |
| `date_premiere_obs`   | timestamptz | Premier run ayant vu cette entité |
| `nb_annonces_actives` | integer     | Mis à jour à chaque run |
| `est_client_ideeri`   | boolean     | Flag commercial (défaut false) |
| `historique_activite` | jsonb       | `[{date, nb_lbc, nb_seloger, nb_total}]` — snapshot par run |
| `groupe_entite_id`    | uuid FK→entites | Pour fusion manuelle de doublons résiduels |

**Note migration** : la clé primaire a été migrée de `siret` (NOT NULL, ancien schéma SIRENE)
vers `id` (uuid). `siret` est maintenant nullable. La contrainte UNIQUE est sur `signature`.

### Table `annonces`
Une ligne par annonce de portail. **Append-only** — ne jamais écraser, suivi temporel via flags.

| Colonne                  | Type        | Description |
|--------------------------|-------------|-------------|
| `id_annonce`             | text PK     | `lbc_<list_id>` ou `seloger_<legacyId>` |
| `source`                 | text        | `'lbc'` ou `'seloger'` |
| `type_bien`              | text        | `'appartement'` ou `'maison'` |
| `titre`                  | text        | Titre de l'annonce |
| `prix_affiche`           | integer     | Prix en euros |
| `surface`                | float       | Surface en m² |
| `nb_pieces`              | integer     | Nombre de pièces |
| `code_postal`            | text        | Code postal |
| `commune`                | text        | Ville |
| `quartier_calcule`       | text        | Quartier (SeLoger uniquement) |
| `url_annonce`            | text        | URL directe de l'annonce |
| `date_publication`       | date        | Date de mise en ligne sur le portail |
| `date_premiere_obs`      | timestamptz | Premier run qui a vu cette annonce |
| `date_derniere_obs`      | timestamptz | Dernier run actif |
| `est_active`             | boolean     | `false` = annonce retirée du portail |
| `prix_m2_affiche`        | float       | prix / surface |
| `id_unique_bien`         | text        | Hash de dédup — aligné entre portails après fuzzy matching |
| `sur_lbc`                | boolean     | Diffusée sur LBC (peut être `true` même si `source='seloger'` après fuzzy) |
| `sur_seloger`            | boolean     | Diffusée sur SeLoger |
| `dpe`                    | text        | Classe DPE : A–G ou N (neuf). LBC: attr `energy_rate`, SeLoger: `energyClass` |
| `ges`                    | text        | Classe GES : A–G. LBC uniquement (attr `ges`). SeLoger ne l'expose pas. |
| `match_confidence`       | float       | Score fuzzy inter-portail 0.50–1.0 (null si non matché en passe 2) |
| `historique_prix`        | jsonb       | `[{date, prix, ancien_prix}]` |
| `signature_entite_bien`  | text        | `entity_signature()` — conservé pour historique |
| `nom_commercial`         | text        | Nom de l'entité au moment du scraping |
| `entite_id`              | uuid FK→entites | Lien vers l'entité normalisée |
| `run_id_premiere_obs`    | bigint FK→runs | Run qui a découvert cette annonce |

**Contrainte supprimée** : `annonces_signature_entite_bien_key` (UNIQUE sur
`signature_entite_bien`) — supprimée car une même entité peut avoir plusieurs annonces.

### Tables `stg_lbc` / `stg_seloger`
Staging brut — une ligne par page scrapée.

| Colonne       | Type        | Description |
|---------------|-------------|-------------|
| `id`          | bigserial PK | Auto-incrémenté |
| `scraped_at`  | timestamptz | Timestamp du scraping |
| `code_postal` | text        | CP de la commune |
| `page`        | integer     | Numéro de page (1-N) |
| `url_source`  | text        | URL ScrapingBee utilisée |
| `nb_annonces` | integer     | Nombre d'annonces sur la page |
| `processed`   | boolean     | `true` après passage dans transform (non encore implémenté) |
| `processed_at`| timestamptz | Timestamp du traitement |
| `data_brute`  | jsonb       | JSON complet (annonces + `_meta`) |

**Note** : les lignes antérieures à la migration ont les métadonnées dans
`data_brute._meta` plutôt qu'en colonnes. Le transform gère les deux formats.

### Vue `biens_uniques`

```sql
CREATE OR REPLACE VIEW biens_uniques AS
SELECT
  id_unique_bien, code_postal,
  MIN(commune) AS commune, MIN(type_bien) AS type_bien,
  MIN(surface) AS surface, MIN(prix_affiche) AS prix_min, MAX(prix_affiche) AS prix_max,
  bool_or(source = 'lbc')     AS sur_lbc,
  bool_or(source = 'seloger') AS sur_seloger,
  COUNT(*)                    AS nb_annonces,
  COUNT(DISTINCT entite_id)   AS nb_entites,
  array_agg(DISTINCT nom_commercial) AS entites
FROM annonces
WHERE est_active = TRUE AND id_unique_bien IS NOT NULL
GROUP BY id_unique_bien, code_postal;
```

**Ce qu'elle retourne** : une ligne par bien réel (`id_unique_bien`), avec les flags de
présence portail construits par agrégation. Après fuzzy matching, deux annonces LBC+SeLoger
qui représentent le même bien partagent le même `id_unique_bien` → apparaissent comme une
seule ligne avec `sur_lbc=true AND sur_seloger=true`.

**Cas d'usage** :
- Compter les biens uniques sur une commune (pas les doublons inter-portails)
- Répartition LBC / SeLoger / les deux sur un territoire
- Biens diffusés par plusieurs entités (`nb_entites > 1`)
- Point d'entrée pour croisement DVF (surface + type + CP + fourchette prix)
- Point d'entrée pour croisement ADEME DPE

---

## 4. Paramètres ScrapingBee validés

**Clé API** : dans `.env` sous `SCRAPINGBEE_KEY`. Quota mensuel, reset le 1er du mois.

### SeLoger
```python
params = {
    "render_js":     "false",   # Les données sont dans __UFRN_FETCHER__ sans JS
    "premium_proxy": "true",    # Contourne Datadome (~10 crédits/page)
    "country_code":  "fr",      # Proxy géolocalisé France
}
timeout = 45  # secondes
```

### LeBonCoin
```python
params = {
    "render_js":       "true",   # SPA React — nécessite rendu JS
    "stealth_proxy":   "true",   # Contourne Datadome LBC (~75 crédits/page)
    "wait":            "4000",   # 4s d'attente après chargement JS
    "block_resources": "false",  # Garder toutes les ressources
}
timeout = 120  # secondes (stealth est lent)
```

**Attention** : `premium_proxy` ne fonctionne pas sur LBC (Datadome différent de SeLoger).
Seul `stealth_proxy` bypass Datadome LBC.

### Coût estimé par commune

| Taille    | Pages SL | Pages LBC | Crédits SL | Crédits LBC | Total  |
|-----------|----------|-----------|------------|-------------|--------|
| Petite (~60 ann) | 2   | 2         | 20         | 150         | ~170   |
| Moyenne (~120 ann) | 4 | 4         | 40         | 300         | ~340   |
| Grande (~300 ann) | 10 | 9         | 100        | 675         | ~775   |

Quota mensuel starter ScrapingBee : 1 000 crédits → 3–5 communes/mois selon taille.

### Erreurs anti-bot connues

- **LBC 520** : URL avec `__` (double underscore). Utiliser
  `/cl/ventes_immobilieres/cp_<ville>_<cp>/real_estate_type:2/p-N`
  et **non** `/recherche?locations=<ville>_<cp>__<lat>_<lon>_<radius>`.
- **SeLoger 404** : l'ancien format `/achat/appartements-maisons/<slug>/` est obsolète.
  Utiliser `classified-search?locations=<code_commune_ou_cp>`.
- **LBC HTTP 500 sur une page** : intermittent, relancer avec `wait=5000`.

---

## 5. Format des données sources

### SeLoger — bloc `__UFRN_FETCHER__`

Chemin de décodage :
```
HTML → <script id="__UFRN_FETCHER__"> → JSON.parse(unicode_escape)
     → data["classified-serp-init-data"]  (base64 LZString)
     → LZString.decompressFromBase64()
     → pageProps.classifieds          ← liste d'IDs (ordre d'affichage)
     → pageProps.classifiedsData      ← dict keyed by ID
     → pageProps.totalCount           ← total annonces commune
```

Structure d'un item `classifiedsData[id]` :
```json
{
  "hardFacts": {
    "title": "Appartement à vendre",
    "price": { "ariaLabel": "175000 €" },
    "facts": [
      { "type": "livingSpace",      "splitValue": "57,2" },
      { "type": "numberOfRooms",    "splitValue": "3" },
      { "type": "numberOfBedrooms", "splitValue": "2" }
    ]
  },
  "location": { "address": { "zipCode": "69700", "city": "Givors", "district": "" } },
  "provider": {
    "intermediaryCard": { "title": "NOM AGENCE" },
    "publisherType": "AGENCY",
    "isPrivateOwner": false,
    "agencyLegalInformations": ["... SIRET 12345678901234 ..."],
    "profileUrl": "https://..."
  },
  "metadata": { "legacyId": "12345678", "creationDate": "2026-01-15T..." },
  "energyClass": "C"
}
```

- **DPE** : `item["energyClass"]` — valeurs A–G, absent si non renseigné (~3% des cas Givors)
- **GES** : **absent** dans les données SERP SeLoger. Non exposé dans `classifiedsData`.
- **SIREN/SIRET** : regex `SIRET[^0-9]*(\d{14})` dans `agencyLegalInformations`

### LeBonCoin — bloc `__NEXT_DATA__`

Chemin d'accès :
```
HTML → <script id="__NEXT_DATA__" type="application/json">
     → props.pageProps.searchData.ads   ← liste d'annonces
     → props.pageProps.searchData.total ← total
```

Structure d'un item `ads[i]` :
```json
{
  "list_id": 3176252402,
  "subject": "Appartement 4 pièces 90 m²",
  "price": [180000],
  "location": { "zipcode": "69700", "city": "Givors" },
  "first_publication_date": "2026-05-10 14:30:00",
  "url": "https://www.leboncoin.fr/ad/ventes_immobilieres/...",
  "owner": {
    "name": "PATRIMMO SERVICE",
    "type": "pro",
    "siren": "501238356",
    "store_id": "85597288"
  },
  "attributes": [
    { "key": "real_estate_type", "value": "1" },
    { "key": "square",           "value": "90" },
    { "key": "rooms",            "value": "4" },
    { "key": "energy_rate",      "value": "d" },
    { "key": "ges",              "value": "d" }
  ]
}
```

- **DPE** : `_lbc_attr(ad, "energy_rate")` → normaliser `.upper()` → A–G ou N (neuf)
- **GES** : `_lbc_attr(ad, "ges")` → normaliser `.upper()` — présent sur ~58% des annonces
- **SIREN** : `owner.siren` — présent pour les pros (`owner.type == "pro"`)
- **Type** : `owner.type == "private"` → particulier, sinon agence

---

## 6. Algorithme de déduplication et matching

### Passe 1 — hash exact (`id_unique_bien`)

```python
def id_unique_bien(type_bien, surface, code_postal, prix) -> str:
    s = round(float(surface or 0) / 5) * 5        # arrondi 5m²
    p = round(float(prix or 0) / 10_000) * 10_000 # arrondi 10k€
    key = f"{type_bien}|{s}|{code_postal}|{p}"
    return hashlib.md5(key.encode()).hexdigest()[:16]
```

Limites : le même bien affiché à 180 000€ sur LBC et 185 000€ sur SeLoger → hashs différents.
La passe 2 corrige ces cas.

### Passe 2 — fuzzy matching inter-portail (`compute_match_score`)

**Conditions obligatoires** (retourne 0.0 si l'une échoue) :
1. Même `code_postal`
2. Même `type_bien` (si renseigné sur les deux)
3. Écart surface ≤ 5% : `|s1-s2| / max(s1,s2) ≤ 0.05`
4. Écart prix ≤ 10% : `|p1-p2| / max(p1,p2) ≤ 0.10`
5. Même entité : `entite_id` identique **OU** `normalize_name(nom)` identique

**Score de confiance** (si toutes les conditions passent) :
```
score = 0.50  (base — conditions obligatoires respectées)
+ 0.15 × (1 - écart_surface / 0.05)    → jusqu'à +0.15
+ 0.15 × (1 - écart_prix / 0.10)       → jusqu'à +0.15
+ 0.10 si entite_id exact (vs nom normalisé)
+ 0.10 si DPE identique sur les deux
```

Plage résultante : **0.50** (entity par nom, price/surface au seuil) → **1.0** (parfait).

**Greedy one-to-one** : tri par score décroissant, chaque annonce LBC et SeLoger ne peut
être appariée qu'une fois. Les deux annonces de la paire reçoivent :
- `sur_seloger=True` (côté LBC) et `sur_lbc=True` (côté SeLoger)
- `id_unique_bien` aligné sur la valeur LBC
- `match_confidence` = score arrondi à 3 décimales

### Déduplication des entités (`entity_signature`)

Clé de dédup par priorité décroissante :
1. `siren_XXXXXXXXX` — si SIREN 9 chiffres valide (même agence sur LBC et SeLoger)
2. `particulier_CP` — si `owner.type == "private"`
3. `nom_NORMNAME` — `normalize_name()` du nom commercial

`normalize_name()` : supprime accents, lowercase, retire stopwords immobiliers
(`agence`, `immobilier`, `cabinet`, `groupe`, `reseau`...), colle les mots par `_`.

---

## 7. Résultats Givors — run de référence (mai 2026)

- **Commune** : Givors, 69700
- **Scraping** : 4 pages SeLoger (30/page) + 4 pages LBC (35/page)
- **Annonces actives** : 231 (112 LBC + 119 SeLoger)
- **Entités identifiées** : 79
- **Biens uniques** : 142
- **Biens sur les deux portails** : 45/142 (31.7%) après fuzzy matching
  - Avant fuzzy : 0 (hash exact ne matchait rien entre portails)
  - 40 paires trouvées, score médian ≈ 1.0
- **Biens diffusés par 2+ entités** : 40/142 (28.2%)
- **Couverture DPE** : 180/231 (78%) — LBC 58%, SeLoger 97%
- **Couverture GES** : 65/231 (28%) — LBC uniquement

Top entités par nb_annonces_actives :
| Entité | Biens | LBC | SeLoger |
|--------|-------|-----|---------|
| GUY HOQUET L'IMMOBILIER | 16 | 0 | 16 |
| LES CLÉS D'ALEXIA Lyon | 11 | 0 | 11 |
| PATRIMMO SERVICE | 11 | 11 | 0 |
| CENTURY 21 HESTIA LDI GRIGNY | 10 | 0 | 10 |
| LEONE IMMOBILIER | 8 | 5 | 8 |

---

## 8. Commandes de lancement

### Scraper une nouvelle commune

1. Trouver le code SeLoger : rechercher la commune sur seloger.com, copier le paramètre
   `locations=` dans l'URL (ex : `AD08FR28776` pour Givors, `AD08FR28766` pour Écully).
   Le code postal direct fonctionne aussi (ex : `locations=69700`).

2. Construire l'URL LBC : format `/cl/ventes_immobilieres/cp_<ville>_<cp>/real_estate_type:2`
   Exemple : `cp_givors_69700`, `cp_saint-priest_69800`. **Ne jamais utiliser
   `/recherche?locations=` qui contient `__` et provoque un HTTP 520.**

3. Copier `scrape_givors.py` → `scrape_<commune>.py`, modifier les 6 constantes :
   ```python
   COMMUNE      = "Saint-Priest"
   CODE_POSTAL  = "69800"
   NB_PAGES_SL  = 3   # ceil(nb_annonces_seloger / 30), vérifier sur le site
   NB_PAGES_LBC = 3   # ceil(nb_annonces_lbc / 35)
   SL_BASE      = "https://www.seloger.com/classified-search?...&locations=AD08FRXXXXX&..."
   LBC_BASE     = "https://www.leboncoin.fr/cl/ventes_immobilieres/cp_saint-priest_69800/real_estate_type:2"
   ```

4. Lancer :
   ```bash
   python3 scrape_<commune>.py
   python3 transform.py <code_postal> <commune>
   ```

### Transform seul (données déjà dans stg_*)

```bash
python3 transform.py 69700 Givors
```

Pipeline exécuté par `run_transform()` :
1. Crée un enregistrement dans `runs`
2. Lit `stg_lbc` et `stg_seloger` pour le CP donné
3. Parse et upsert chaque annonce dans `annonces` (new / price_change / unchanged)
4. Upsert les entités dans `entites`
5. Marque inactives les annonces non vues ce run
6. Lance `fuzzy_match_cross_portal()` (passe 2)
7. Met à jour `historique_activite` des entités
8. Met à jour les stats dans `runs`

### Requêtes analytiques standard

```sql
-- Biens uniques sur une commune
SELECT COUNT(*) FROM biens_uniques WHERE code_postal = '69700';

-- Répartition portails (biens)
SELECT
  CASE WHEN sur_lbc AND sur_seloger THEN 'les deux'
       WHEN sur_lbc                 THEN 'lbc seul'
       ELSE                              'seloger seul' END AS portail,
  COUNT(*) AS nb_biens,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM biens_uniques WHERE code_postal = '69700'
GROUP BY 1 ORDER BY 2 DESC;

-- Top entités avec répartition portails
SELECT
  nom_commercial,
  COUNT(DISTINCT id_unique_bien)                                       AS biens_uniques,
  COUNT(DISTINCT CASE WHEN source='lbc'     THEN id_unique_bien END)  AS lbc,
  COUNT(DISTINCT CASE WHEN source='seloger' THEN id_unique_bien END)  AS seloger
FROM annonces
WHERE code_postal = '69700' AND est_active = TRUE
GROUP BY nom_commercial ORDER BY biens_uniques DESC;

-- Biens diffusés par N entités
SELECT nb_entites, COUNT(*) AS nb_biens,
       ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(),1) AS pct
FROM biens_uniques WHERE code_postal = '69700'
GROUP BY 1 ORDER BY 1;

-- Couverture DPE
SELECT source,
  COUNT(*) FILTER (WHERE dpe IS NOT NULL) AS avec_dpe,
  COUNT(*) AS total
FROM annonces WHERE code_postal = '69700' AND est_active = TRUE
GROUP BY source;

-- Évolution temporelle d'une entité
SELECT (h->>'date') AS date, (h->>'nb_total')::int AS nb_annonces
FROM entites, jsonb_array_elements(historique_activite) AS h
WHERE nom_commercial = 'LEONE IMMOBILIER'
ORDER BY 1;
```

### Backfill DPE/GES sur données antérieures

```bash
python3 -c "
import os; from dotenv import load_dotenv; load_dotenv()
from supabase import create_client
from transform import parse_lbc_ads, parse_seloger_ads, fuzzy_match_cross_portal
sb = create_client(os.environ['SUPA_URL'], os.environ['SUPA_KEY'])

for row in sb.table('stg_lbc').select('data_brute').execute().data:
    for ann in parse_lbc_ads(row['data_brute']):
        u = {k: ann[k] for k in ('dpe','ges') if ann.get(k)}
        if u: sb.table('annonces').update(u).eq('id_annonce', ann['id_annonce']).execute()

for row in sb.table('stg_seloger').select('data_brute').execute().data:
    for ann in parse_seloger_ads(row['data_brute']):
        if ann.get('dpe'):
            sb.table('annonces').update({'dpe': ann['dpe']}).eq('id_annonce', ann['id_annonce']).execute()

print(fuzzy_match_cross_portal('69700', 'backfill'), 'biens matchés')
"
```

---

## 9. Prochaines étapes

### Court terme
- **Scraper paramétrique** : créer `scraper.py <code_postal> <commune> <sl_locations_code>`
  pour éviter de dupliquer `scrape_givors.py` par commune.
- **GES SeLoger** : absent de la SERP. Chercher dans la page détail annonce individuelle
  (`/annonces/achat/<ville>/<id>.htm`) ou via l'API SeLoger non documentée.
- **Deuxième run Givors** : scraping + transform dans ~2 semaines pour obtenir les premières
  évolutions (nouvelles annonces, disparitions, baisses de prix).
- **processed=true** dans `stg_*` après chaque transform (actuellement non implémenté dans
  `run_transform()`).

### Moyen terme
- **Croisement DVF** : données DGFiP sur data.gouv.fr, join sur (type_bien, surface±10m²,
  code_postal, prix±15%). Objectif : mesurer délais et taux de vente par agence.
- **Croisement ADEME DPE** : API `https://data.ademe.fr/datasets/dpe-v2-logements-existants`,
  valider cohérence DPE annonce vs DPE officiel.
- **Multi-communes** : pipeline batch avec gestion du quota ScrapingBee mensuel.
- **Dashboard analytique** : Metabase ou Supabase Studio sur les requêtes standard.

---

## 10. Infra et accès

### Variables d'environnement (`.env`)
```
SCRAPINGBEE_KEY=   # Clé API ScrapingBee (quota mensuel, reset le 1er)
SUPA_URL=          # https://qhehngzoeocjqpojymso.supabase.co
SUPA_KEY=          # Clé anon Supabase (suffisante pour INSERT/SELECT/UPDATE)
```

### PAT Supabase (opérations DDL uniquement)
Pour exécuter des migrations SQL via l'API Management :
```python
PAT = "sbp_c9acbafef178ae005ffaa339e2fabba72a23a2a3"
url = "https://api.supabase.com/v1/projects/qhehngzoeocjqpojymso/database/query"
headers = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
# Retourne HTTP 201 (pas 200) pour une DDL réussie
```
Ne pas mettre le PAT dans `.env` — à passer en variable locale de session.

### Dépendances Python
```bash
pip install supabase python-dotenv requests lzstring
```

### Conventions de code
- Python 3.10+ — unions `X | Y` plutôt que `Optional[X]`
- `snake_case` pour toutes les variables, fonctions et fichiers
- Logs explicites à chaque étape (console)
- Variables d'environnement via `.env` + `python-dotenv` — jamais de clé en dur
- Pas de commentaires évidents — seulement les invariants non-triviaux
