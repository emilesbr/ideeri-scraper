# Protocole expérimental — Thèse Ideeri (v35, mai 2026)

## Titre et question de recherche

**Titre** : Frictions informationnelles et efficience du marché immobilier —
Une analyse comparative des mécanismes d'intermédiation et des effets de la
numérisation sur les coûts de transaction.

**Question centrale** : Pourquoi le marché immobilier français, malgré vingt ans
de croissance et l'émergence des outils numériques, présente-t-il des niveaux
d'efficience informationnelle parmi les plus faibles d'Europe occidentale ?

---

## Hypothèse centrale

La défaillance informationnelle du marché immobilier français est auto-entretenue
par quatre mécanismes imbriqués :
1. Rente de situation (North, Olson) — suppression de l'incitation à innover
2. Problème du passager clandestin (Olson) — fragmentation de l'information
3. Paradoxe de la donnée comme avantage concurrentiel (VRIN, Barney)
4. Dépendance de sentier technologique (Arthur, North)

La réduction durable des frictions ne peut résulter d'ajustements partiels.
Elle requiert une infrastructure intégrée modifiant simultanément les incitations
individuelles et les conditions de coordination collective.

---

## Contributions originales

### Contribution I — Monopole informationnel stratégique sur la demande
Les portails d'annonces (LeBoncoin, SeLoger) captent et retiennent stratégiquement
la donnée agrégée de comportement des acquéreurs (Ω = budget, surface, localisation,
urgence de chaque acquéreur actif) pour la revendre aux agents qui l'ont générée.

**Formalisation** :
- Information portail : I_P = Ω (totalité des comportements de recherche)
- Information agent i : I_A^i ⊆ Ω (limité à ses interactions directes)
- Monopole : I_P ⊃ I_A^i avec ΔI = I_P \ I_A^i (rétenu stratégiquement)
- Biais d'estimation : ε_i(t) = E[V*(z,t)|I_A^i] − E[V*(z,t)|I_P] > 0 en marché baissier

**Ce mécanisme est distinct de** : Akerlof (qualité bien), Stigler (coût recherche
individuel), Rochet-Tirole (plateforme biface standard).

### Contribution II — Équilibre de sous-optimalité stable
Démonstration que les quatre mécanismes d'auto-entretien produisent un équilibre
collectivement sous-optimal que les acteurs rationnels n'ont pas intérêt à rompre
unilatéralement (structure de dilemme du prisonnier généralisé).

### Contribution III — Condition de sortie par dominance individuelle
L'équilibre peut être rompu sans réforme institutionnelle si une infrastructure
produit des gains privés directs suffisants : π_i(Adopter | k=0) > π_i(Retenir | k=0).
Cette condition est testable et falsifiable empiriquement (chapitre 6).

---

## Design quasi-expérimental : Différences en Différences (DiD)

**Référence théorique** : Angrist et Pischke (2008) — Mostly Harmless Econometrics ;
Card et Krueger (1994).

**Estimateur standard** :
τ̂_DiD = (Ȳ traité, après − Ȳ traité, avant) − (Ȳ contrôle, après − Ȳ contrôle, avant)

**Variable de traitement binaire** : adoption d'Ideeri (1) ou non (0)
**Variable de traitement continue** : τ_t = k_t / N
(taux d'adoption local — nombre d'adoptants sur la zone / total agences actives)
→ capture les externalités de réseau positives croissantes avec l'adoption

**Hypothèse d'identification** : tendances parallèles pré-adoption
→ testable sur données DVF historiques 2022-2025

---

## Groupe traitement

Agences clientes Ideeri (est_client_ideeri = true dans la table entites).

**Premiers clients** :
- Village Immobilier — date adoption : décembre 2025
- Pietrapolis — date adoption : décembre 2025

**Variable** : date_adoption_ideeri dans la table entites (à implémenter — tâche B1)

---

## Groupe contrôle

Agences non-clientes sur les mêmes zones géographiques ou zones adjacentes
de profil comparable.

**Segmentation du groupe contrôle** :
1. Contrôle pur : agences sur zones géographiquement distinctes (aucun adoptant)
2. Contrôle mixte : agences non-adoptantes sur zones partiellement adoptées
3. Groupe en transition : adoptants futurs observés en pré-adoption

**Construction** : matching par score de propension sur :
- Taille agence (nb conseillers via Sirene)
- Ancienneté (date création via Sirene)
- Zone géographique (code postal, caractéristiques socio-économiques INSEE)
- Volume moyen de transactions pré-adoption (via DVF)
- Délai moyen de commercialisation pré-adoption (via triangle DPE × Annonces × DVF)

---

## Les quatre hypothèses testables

### H1 — Réduction de l'asymétrie d'estimation (niveau transactionnel)
**Formulation** : l'adoption réduit l'écart entre prix d'estimation initial et prix de vente final.
**Logique** : sans donnée agrégée de demande, ε_i(t) > 0 en marché baissier.
**Indicateur Supabase** : dispersion estimation/vente avant-après adoption vs groupe contrôle.
**Source données** : DVF (prix vente) + données opérationnelles Ideeri (prix estimé)
**Période minimale** : 18 mois

### H2 — Réduction des frictions opérationnelles (niveau structurel)
**Formulation** : l'adoption réduit la durée du cycle de vente et augmente le taux de transformation.
**Logique** : fragmentation des outils génère des ruptures dans la chaîne de traitement.
**Indicateurs** : durée moyenne prise de mandat → signature définitive, taux de conversion par étape.
**Source données** : données opérationnelles Ideeri (funnel) + DVF (dates transaction)
**Période minimale** : 18 mois

### H3 — Augmentation du taux de mandats exclusifs
**Formulation** : l'adoption augmente le taux de mandats exclusifs diffusés.
**Logique** : Ideeri permet de démontrer scientifiquement la valeur de l'exclusivité au vendeur.
**Indicateur Supabase** : is_exclusive dans annonces (à implémenter — tâche A2)
**Requête de mesure** :
```sql
SELECT entite_id, nom_commercial,
  AVG(is_exclusive::int) FILTER (WHERE date_premiere_obs < date_adoption) AS taux_avant,
  AVG(is_exclusive::int) FILTER (WHERE date_premiere_obs >= date_adoption) AS taux_apres
FROM annonces a
JOIN entites e ON a.entite_id = e.id
WHERE e.est_client_ideeri = true
GROUP BY entite_id, nom_commercial;
```
**Période minimale** : 18 mois

### H4 — Point de bascule et effet de réseau local
**Formulation** : les gains d'adoption croissent avec τ_t jusqu'à un point de bascule τ*
au-delà duquel la non-adoption devient un désavantage concurrentiel objectif.
**Indicateur** : relation entre τ_t = k_t/N et les gains de performance mesurés.
**Période minimale** : 24-36 mois

---

## Terrain de mesure

**Communes traitées** : Givors (69700), Rive-de-Gier (42800)
**Communes contrôle** : à définir — communes adjacentes non-Ideeri de profil comparable
**T0** : mai 2026 — **T1** : juillet 2026 — **Dépôt thèse** : 2028-2029

---

## Architecture des données

| Source | Usage | Statut |
|--------|-------|--------|
| Données opérationnelles Ideeri | Variables outcome H1-H4 | Disponibles déc. 2025 |
| DVF DGFIP | Groupe contrôle + tendances parallèles | code_insee en base ✅ |
| Sirene INSEE (NAF 6831Z) | Matching score propension | ~35 000 agences |
| Triangle DPE × Annonces × DVF | Matching probabiliste | Conditionnel accès portails |

---

## Variables Supabase

### Déjà en base
- `nb_mandats_actifs` — historique_activite par run
- `prix_affiche` — prix demandé
- `date_premiere_obs` / `date_derniere_obs` — chronologie
- `dpe`, `ges` — couverture ~90% LBC / ~97% SeLoger
- `code_insee` — clé jointure DVF ✅
- `bien_id`, `cluster_bien_id`, `match_confidence` — matching inter-portail

### À implémenter
- `is_exclusive` (tâche A2) → H3
- `date_adoption_ideeri` dans entites (tâche B1) → design DiD
- `date_resiliation_ideeri` dans entites (tâche B1)
- `custom_ref` (tâche A1) → matching parfait inter-portail
- `lat`, `lng` (tâche A3) → croisement DVF par adresse

---

## Précautions méthodologiques

**Biais principal** : biais de confirmation (chercheur = acteur du terrain)

**Dispositifs** :
1. Hypothèses formulées a priori, explicitement falsifiables
2. Groupe contrôle sur mêmes zones, n'ayant pas encore adopté
3. Données opérationnelles objectives uniquement
4. Test de placebo (décalage fictif de 12 mois)
5. Test de tendances pré-traitement (coefficients ≈ 0 avant adoption)

**Cadre institutionnel** : Convention CIFRE (en discussion)

---

## Référence comparative

| Marché | Infrastructure | Productivité | Commissions |
|--------|---------------|--------------|-------------|
| Suède | Hemnet 90% | 12-15 txn/an | 2-3% |
| USA | MLS 90% | ~10-12 txn/an | 5-6% |
| Pays-Bas | Funda 75% | 8-10 txn/an | ~1,5% |
| France | Fragmentée | 5-6 txn/an | 5,78% |
| Allemagne | Fragmentée | ~5 txn/an | 7,14% |
| Australie | REA 90% extractif | ~6 txn/an | ~2,5% |

**Variable causale : l'infrastructure partagée, NON les commissions.**
Purplebricks UK (commissions basses sans infrastructure) = désintermédiation dégradée.

---

## Coût estimé de l'équilibre sous-optimal

35 000 agences × 3-4 transactions manquantes/an × 8 000 € = **840 M€ à 1,1 Md€/an détruits**.
*(Hypothèse conservatrice : 50% de l'écart France/Suède attribuable aux frictions informationnelles)*
