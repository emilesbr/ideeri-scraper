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
├── pipeline.py             # Point d'entrée unique : scrape + transform + check + reset
├── transform.py            # Cœur du pipeline : stg_* → annonces + entites + runs
├── api.py                  # API Flask → localhost:5000
├── dashboard.html          # Dashboard SPA — ouvrir dans le navigateur
│
├── scrape_givors.py        # LEGACY — remplacé par pipeline.py run
├── test_ecully.py          # Validation des paramètres ScrapingBee (référence)
├── test_scrapingbee.py     # Benchmark 8 combinaisons de paramètres (A–H)
│
├── migration_v2.sql        # SQL idempotent : crée runs, entites, alter annonces/stg_*
├── migration_sirene.sql    # SQL idempotent : colonnes enrichissement SIRENE dans entites
├── migration_stg_tables.sql # SQL intermédiaire (précède migration_v2)
│
├── enrich_sirene.py        # Enrichissement SIRENE des entités (denomination, APE, adresse)
│
├── analyser_local.py       # Debug HTML local
├── debug_page.py           # Debug page HTML brute
├── extractor_exhaustive.py # Brouillon (remplacé par transform.py)
├── scraper_hybride.py      # Brouillon (remplacé par pipeline.py)
├── capture_seloger.html    # Capture HTML SeLoger référence (debug)
│
└── debug/                  # HTML bruts ScrapingBee (gitignorés — vider régulièrement)
```

### Rôle détaillé des fichiers actifs

**`pipeline.py`** — **Point d'entrée principal.** Orchestre scraping + transform en une seule
commande. Sous-commandes : `run`, `scrape`, `transform`, `check`, `status`, `reset`.
Gère les URL LBC et SeLoger, le parallélisme (ThreadPoolExecutor), et les confirmations
interactives. Usage : `python3 pipeline.py run <cp> <commune> [--sl-code ...]`

**`transform.py`** — **Le cœur du pipeline**. Lit `stg_lbc` et `stg_seloger`, normalise les
annonces, gère les entités sans doublon, le suivi temporel, le DPE/GES, et le matching
inter-portail. Appelé par `pipeline.py transform` ou directement :
`python3 transform.py <code_postal> [commune]`

**`api.py`** — Serveur Flask qui expose les données Supabase en JSON pour le dashboard.
Endpoints : `GET /api/zones`, `GET /api/zone/<cp>`, `GET /api/entite/<nom>`,
`GET /api/runs`, `POST /api/run` (SSE streaming). Usage : `python3 api.py`

**`dashboard.html`** — SPA vanilla JS. Sidebar zones, métriques globales (biens, mandats,
entités, multi-mandats, DPE), classement entités avec part de marché, mouvements de prix,
vue détail par entité. Ouvrir dans le navigateur après avoir lancé `api.py`.

**`scrape_givors.py`** — Script legacy, remplacé par `pipeline.py run`. Conservé comme
référence de l'architecture de scraping.

**`test_ecully.py`** — Script de référence qui a validé les paramètres ScrapingBee
(premium pour SeLoger, premium pour LBC depuis mai 2026).

**`test_scrapingbee.py`** — Benchmark de 8 combinaisons de paramètres (A–H). À relancer si
la clé API change ou si un portail modifie son anti-bot.

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
| `denomination_sociale`| text        | Nom officiel SIRENE (ex: PATRIMMO SERVICE SASU) |
| `libelle_forme_juridique` | text    | Ex: "Société par actions simplifiée" |
| `code_ape`            | text        | Ex: `6831Z` (Agences immobilières) |
| `libelle_ape`         | text        | Ex: "Agences immobilières" |
| `adresse_siege`       | text        | Adresse du siège social (SIRENE) |
| `effectif_salarie`    | text        | Tranche effectif INSEE (`'01'`=1-2, `'02'`=3-5…) |
| `telephone`           | text        | Non fourni par SIRENE — à enrichir depuis LBC boutique / SeLoger profil |
| `date_creation_entreprise` | date   | Date de création (SIRENE) |
| `sirene_enrichi_at`   | timestamptz | Timestamp du dernier enrichissement SIRENE |

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
| `bien_id`                | text        | Identifiant canonique du bien : `id_annonce` si non matché, sinon `id_annonce` LBC (intra_entity) ou `cluster_id` hex (cross_entity) |
| `cluster_bien_id`        | text        | ID du cluster cross-entités (null si non regroupé) |
| `cluster_confidence`     | float       | Score moyen du cluster cross-entités |
| `sur_lbc`                | boolean     | Diffusée sur LBC (peut être `true` même si `source='seloger'` après matching) |
| `sur_seloger`            | boolean     | Diffusée sur SeLoger |
| `dpe`                    | text        | Classe DPE : A–G ou N (neuf). LBC: attr `energy_rate` + VEFA→N, SeLoger: `energyClass` |
| `ges`                    | text        | Classe GES : A–G. LBC uniquement (attr `ges`). SeLoger ne l'expose pas. |
| `match_confidence`       | float       | Score intra_entity_match (seuil 0.70) |
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
    bien_id, code_postal,
    MIN(commune) AS commune, MIN(type_bien) AS type_bien,
    ROUND(AVG(surface::numeric), 1) AS surface,
    MIN(prix_affiche) AS prix_min, MAX(prix_affiche) AS prix_max,
    bool_or(source = 'lbc')     AS sur_lbc,
    bool_or(source = 'seloger') AS sur_seloger,
    COUNT(*)                    AS nb_annonces,
    COUNT(DISTINCT entite_id)   AS nb_entites,
    array_agg(DISTINCT nom_commercial) AS entites,
    MIN(dpe) AS dpe, MIN(ges) AS ges,
    MIN(annee_construction) AS annee_construction,
    (COUNT(DISTINCT entite_id) > 1)                    AS multi_mandat,
    (COUNT(DISTINCT entite_id) = 1 AND COUNT(*) > 1)   AS doublon_interne
FROM annonces
WHERE est_active = TRUE AND bien_id IS NOT NULL
GROUP BY bien_id, code_postal;
```

**Ce qu'elle retourne** : une ligne par bien réel (`bien_id`), avec les flags de
présence portail construits par agrégation. Après matching, plusieurs annonces
représentant le même bien partagent le même `bien_id`.

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
    "premium_proxy":   "true",   # Contourne Datadome LBC (validé mai 2026)
    "wait":            "6000",   # 6s d'attente après chargement JS (4000 causait HTTP 500)
    "block_resources": "true",   # Bloquer ressources non nécessaires
}
timeout = 120  # secondes
```

**Backoff automatique** : `_fetch()` réessaie jusqu'à 3 fois en cas de HTTP 500 LBC,
en augmentant le wait de +2000ms à chaque tentative (6000 → 8000 → 10000ms).

**Note** : passage de `stealth_proxy` à `premium_proxy` en mai 2026 (Rive-de-Gier 42800).
Si LBC retourne des erreurs Datadome, revenir à `stealth_proxy` + `block_resources: false`.

### Coût estimé par commune

| Taille    | Pages SL | Pages LBC | Crédits SL | Crédits LBC | Total  |
|-----------|----------|-----------|------------|-------------|--------|
| Petite (~60 ann) | 2   | 2         | 20         | 150         | ~170   |
| Moyenne (~120 ann) | 4 | 4         | 40         | 300         | ~340   |
| Grande (~300 ann) | 10 | 9         | 100        | 675         | ~775   |

Quota mensuel starter ScrapingBee : 1 000 crédits → 3–5 communes/mois selon taille.

### Erreurs anti-bot connues

- **LBC HTTP 500 sur une page** : rendu JS pas encore terminé. Le backoff automatique
  (6000→8000→10000ms) couvre la plupart des cas. Si ça persiste, lancer `pipeline.py retry <cp>`.
- **SeLoger 404** : l'ancien format `/achat/appartements-maisons/<slug>/` est obsolète.
  Utiliser `classified-search?locations=<code_commune_ou_cp>`.

### URL LeBonCoin — format actuel (mai 2026)

```
https://www.leboncoin.fr/recherche/p-{N}?category=9
  &locations={Commune}_{CP}__{lat}_{lon}_5000
  &immo_sell_type=old
  &owner_type=all
  &page={N}    ← pour pages > 1
```

- Géocodage via `api-adresse.data.gouv.fr` pour obtenir lat/lon
- Sans le `__lat_lon_radius`, LBC ignore le filtre de localisation → 882k résultats nationaux
- `immo_sell_type=old` : ventes dans l'ancien (exclut le neuf)
- `owner_type=all` : pros + particuliers
- Pagination : `&page=2`, `&page=3`… (pas de `/p-N` dans le path)

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

### Identifiant canonique (`bien_id`)

Chaque annonce reçoit `bien_id = id_annonce` à l'insertion. Le matching peut ensuite
l'aligner sur un bien partagé :
- `intra_entity_match` : `bien_id` = `id_annonce` LBC pour la paire LBC↔SeLoger
- `cross_entity_match` : `bien_id` = `cluster_id` hex (uuid4[:16]) partagé entre toutes
  les annonces du cluster

### Score unique `compute_match_score(a, b) → float`

**Conditions bloquantes** (→ 0.0 si l'une échoue) :
1. Même `code_postal`
2. Même `type_bien` (si renseigné sur les deux)
3. Écart surface ≤ 5% : `|s1-s2| / max(s1,s2) ≤ 0.05`
4. Écart prix ≤ 10% : `|p1-p2| / max(p1,p2) ≤ 0.10`
5. Deux DPE standards différents (ex. C vs D) → rejet
6. Un DPE = N et l'autre dans {A–G} (neuf vs ancien) → rejet

**Score** (si conditions passées) :
```
score = 0.50  (base)
+ 0.20 × (1 - écart_surface / 0.05)   → max +0.20
+ 0.20 × (1 - écart_prix    / 0.10)   → max +0.20
+ 0.15  si DPE identique
- 0.25  si DPE différents (mais non bloquants — cas N vs None, ou None vs None n'applique pas)
+ 0.08  si GES identique
+ 0.10  si nb_pieces identique
+ 0.05  si annee_construction ±5 ans
```
Plage : **0.50** (price/surface au seuil, pas de bonus) → **1.0** (parfait). Plafonné à 1.0.

### Deux appels distincts

**`intra_entity_match(cp)` — seuil 0.70** :
- Uniquement les paires LBC↔SeLoger de la **même entité** (`entite_id` identique)
- Greedy one-to-one (tri score décroissant)
- Action : `sur_seloger=True` côté LBC, `sur_lbc=True` côté SeLoger, `bien_id` = `id_annonce` LBC, `match_confidence`

**`cross_entity_match(cp)` — seuil 0.80** :
- Toutes les annonces actives, paires d'**entités différentes**
- Union-Find avec protection DPE-safe : jamais de merge VEFA (N) + ancien (A–G), même par transitivité
- Action : `cluster_bien_id` + `bien_id` = `cluster_id` partagé, `cluster_confidence` = score moyen

### Déduplication des entités (`entity_signature`)

Clé de dédup par priorité décroissante :
1. `siren_XXXXXXXXX` — si SIREN 9 chiffres valide (même agence sur LBC et SeLoger)
2. `particulier_CP` — si `owner.type == "private"`
3. `nom_NORMNAME` — `normalize_name()` du nom commercial

`normalize_name()` : supprime accents, lowercase, retire stopwords immobiliers
(`agence`, `immobilier`, `cabinet`, `groupe`, `reseau`...), colle les mots par `_`.

---

## 7. Résultats Givors — état au 26 mai 2026 (12 runs)

- **Commune** : Givors, 69700
- **Scraping** : 4 pages SeLoger (30/page) + 4 pages LBC (35/page)
- **Annonces actives** : 240
- **Entités identifiées** : 114
- **Biens uniques** : 136
  - Multi-mandats : 35/136 (26%) — même bien, agences différentes
  - Couverture DPE : ~90% | GES : ~28% (LBC uniquement)

Top entités (mandats = biens uniques par entité) :
| Entité | Mandats | LBC | SeLoger |
|--------|---------|-----|---------|
| GUY HOQUET L'IMMOBILIER   | 15 | –  | 16 |
| LES CLÉS D'ALEXIA Lyon    | 11 | –  | 11 |
| CENTURY 21 HESTIA LDI     | 10 | –  | 10 |
| PATRIMMO SERVICE          | 9  | 11 | –  |
| LEONE IMMOBILIER          | 8  | 5  | 8  |

Note : CENTURY 21 HESTIA LDI et CENTURY 21 HESTIA LDI GRIGNY = même SIREN → une seule entité.

**Zones disponibles en base** : 69700 Givors (240 ann) + 42800 Rive-de-Gier (120 ann).
Les deux communes sont adjacentes — plusieurs agences opèrent sur les deux (Leone, Pilat…).
Le pipeline sépare par CP exact ; le portail les agrège géographiquement.

---

## 8. Commandes de lancement

### Scraper une nouvelle commune

```bash
python3 pipeline.py run <cp> <commune> [--sl-code AD08FRXXXXX] [--source lbc|seloger]
```

- `--sl-code` : code SeLoger optionnel (ex : `AD08FR28776` pour Givors). Sans ce paramètre,
  le pipeline utilise le CP directement dans l'URL SeLoger.
- `--source` : pour scraper un seul portail (utile pour tests ou reruns partiels).

**Trouver le code SeLoger** : chercher la commune sur seloger.com, copier le paramètre
`locations=` dans l'URL. Le code postal direct fonctionne aussi (`locations=69700`).

**Estimer le nombre de pages** : vérifier sur les portails avant de lancer.
- SeLoger : ceil(nb_annonces / 30) pages
- LBC : ceil(nb_annonces / 35) pages

### Commandes pipeline complètes

```bash
python3 pipeline.py check                          # Quota ScrapingBee + derniers runs
python3 pipeline.py status <cp>                    # État d'une zone
python3 pipeline.py scrape <cp> <commune>          # Scraping seul
python3 pipeline.py transform <cp> <commune>       # Transform seul (stg_* déjà rempli)
python3 pipeline.py run <cp> <commune>             # Scrape + transform
python3 pipeline.py retry <cp>                     # Re-scrape les pages manquantes d'un run incomplet
python3 pipeline.py reset <cp>                     # Réinitialise données d'une zone
python3 pipeline.py enrich                         # Enrichit les entités avec données SIRENE
python3 pipeline.py enrich --all                   # Ré-enrichit même les déjà traités
python3 pipeline.py enrich --dry-run               # Affiche sans écrire en base
```

### Dashboard

```bash
python3 api.py          # API → http://localhost:5000
# Ouvrir dashboard.html dans le navigateur
```

Pipeline exécuté par `run_transform()` :
1. Crée un enregistrement dans `runs`
2. Lit `stg_lbc` et `stg_seloger` pour le CP donné
3. Parse et upsert chaque annonce dans `annonces` (new / price_change / unchanged)
4. Upsert les entités dans `entites`
5. Marque inactives les annonces non vues ce run
6. Lance `intra_entity_match()` (LBC↔SeLoger même entité, seuil 0.70)
7. Lance `cross_entity_match()` (multi-entités, seuil 0.80, Union-Find DPE-safe)
8. Met à jour `historique_activite` des entités
9. Met à jour les stats dans `runs`

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
  COUNT(DISTINCT bien_id)                                     AS biens_uniques,
  COUNT(DISTINCT CASE WHEN source='lbc'     THEN bien_id END) AS lbc,
  COUNT(DISTINCT CASE WHEN source='seloger' THEN bien_id END) AS seloger
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

### Réinitialiser et relancer le matching (après changement d'algorithme)

```bash
python3 -c "
from dotenv import load_dotenv; load_dotenv()
import os
from supabase import create_client
from transform import intra_entity_match, cross_entity_match
sb = create_client(os.environ['SUPA_URL'], os.environ['SUPA_KEY'])

# 1. Reset
ids = [r['id_annonce'] for r in sb.table('annonces').select('id_annonce').eq('code_postal','69700').eq('est_active',True).execute().data]
for i in range(0, len(ids), 50):
    sb.table('annonces').update({'sur_lbc': False, 'sur_seloger': False, 'match_confidence': None, 'cluster_bien_id': None, 'cluster_confidence': None}).in_('id_annonce', ids[i:i+50]).execute()
for iid in [r['id_annonce'] for r in sb.table('annonces').select('id_annonce').eq('code_postal','69700').eq('source','lbc').eq('est_active',True).execute().data]:
    sb.table('annonces').update({'sur_lbc': True, 'bien_id': iid}).eq('id_annonce', iid).execute()
for iid in [r['id_annonce'] for r in sb.table('annonces').select('id_annonce').eq('code_postal','69700').eq('source','seloger').eq('est_active',True).execute().data]:
    sb.table('annonces').update({'sur_seloger': True, 'bien_id': iid}).eq('id_annonce', iid).execute()

# 2. Re-run
n = intra_entity_match('69700')
cl = cross_entity_match('69700')
print(f'{n} paires intra, {cl[\"n_clusters\"]} clusters cross')
"
```

---

## 9. Résilience et suivi des runs

### Suivi de l'état d'un run

- **Scrape** : état sauvegardé dans `debug/scrape_state_{cp}.json` après chaque scrape.
  Contient `pages_erreur_lbc`, `pages_erreur_sl`, `lbc_base`, `sl_base`.
  Permet `pipeline.py retry <cp>` pour re-scraper uniquement les pages manquantes.

- **Transform** : `runs.statut` vaut `'running'` pendant le traitement, `'ok'` à la fin,
  `'error'` si le transform crash. Un run `'running'` bloqué = crash détectable.
  Relancer : `python3 pipeline.py transform <cp> <commune>` (idempotent, upsert).

- **Supabase ReadTimeout** : `_sb_execute()` dans transform.py réessaie 3 fois avec
  backoff 2s/4s avant de lever l'exception.

### Détection des runs incomplets

```sql
-- Runs crashés ou incomplets
SELECT id, code_postal, commune, scraped_at, statut, nb_annonces_trouvees
FROM runs
WHERE statut IN ('running', 'error')
ORDER BY scraped_at DESC;
```

## 10. Bugs connus et limitations

**`nb_annonces_actives` dans `entites` n'agrège pas les CPs** :
Le compteur est écrasé à chaque run du transform sur un CP donné. Pour une entité présente
sur plusieurs CPs (ex : AGENCE PILAT sur 69700 et 42800), la valeur reflète uniquement le
CP du dernier run. Fix attendu : calculer depuis `annonces` en agrégeant tous les CPs, ou
cumuler dans `historique_activite`.

**Pattern multi-CP** :
Les agences locales opèrent souvent sur des communes adjacentes avec des CPs différents
(ex : Givors 69700 / Rive-de-Gier 42800). Le portail agrège géographiquement, le pipeline
sépare par CP exact. Pour le volume complet d'une agence, additionner ses mandats sur tous
ses `codes_postaux` dans la table `entites`.

**`processed` dans `stg_*`** : le flag `processed=true` n'est pas positionné après
transform — toutes les lignes restent `processed=false`. Non bloquant mais à implémenter.

**Encodage LBC** : les réponses ScrapingBee sont décodées en UTF-8 explicite (`r.content.decode('utf-8')`).
L'ancien `r.text` utilisait l'encodage déclaré dans le Content-Type, parfois latin-1, provoquant
des noms cassés (`Ã©` au lieu de `é`).

---

## 11. Prochaines étapes

### Court terme
- **Fix `nb_annonces_actives`** : agréger sur tous les CPs dans `transform.py`.
- **Retry Rive-de-Gier 42800** : page 4 LBC manquante — `python3 pipeline.py retry 42800`.
- **`processed=true`** dans `stg_*` après chaque transform.
- **Dashboard retry** : afficher les runs incomplets (`scrape_state_{cp}.json`) avec bouton retry et slider wait.

### Moyen terme
- **GES SeLoger** : absent de la SERP. Chercher dans la page détail annonce individuelle.
- **Croisement DVF** : données DGFiP sur data.gouv.fr, join sur (type_bien, surface±10m²,
  code_postal, prix±15%). Objectif : mesurer délais et taux de vente par agence.
- **Croisement ADEME DPE** : API `https://data.ademe.fr/datasets/dpe-v2-logements-existants`.
- **Multi-communes** : pipeline batch avec gestion du quota ScrapingBee mensuel.

---

## 12. Infra et accès

### Variables d'environnement (`.env`)
```
SCRAPINGBEE_KEY=   # Clé API ScrapingBee (quota mensuel, reset le 1er)
SUPA_URL=          # https://qhehngzoeocjqpojymso.supabase.co
SUPA_KEY=          # Clé anon Supabase (suffisante pour INSERT/SELECT/UPDATE)
```

### PAT Supabase (opérations DDL uniquement)
Pour exécuter des migrations SQL via l'API Management :
```python
PAT = "sbp_VOTRE_PAT_ICI"  # Ne jamais committer — passer en variable locale de session
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
