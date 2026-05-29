# Protocole expérimental — Thèse Ideeri

## Design : Différences en Différences (DiD)

### Variable de traitement
Adoption de la plateforme Ideeri par une agence.
date_adoption_ideeri dans la table entites.

### Groupe traitement
Agences clientes Ideeri (est_client_ideeri = true).
Premier client : Village Immobilier (déc. 2025).

### Groupe contrôle
Agences non-clientes sur communes adjacentes
de profil comparable (même taille de marché,
même type de bien, même gamme de prix).

### Hypothèses testées
H1 : Réduction du délai moyen de vente
     → mesure : DVF — délai annonce→transaction
H2 : Amélioration de la précision d'estimation
     → mesure : décote prix affiché→prix DVF
H3 : Augmentation du taux de mandats exclusifs
     → mesure : is_exclusive dans annonces
H4 : Augmentation du volume de mandats actifs
     → mesure : nb_mandats_actifs dans historique_activite

### Variables disponibles en base
- nb_mandats_actifs : historique_activite par run
- is_exclusive : à implémenter (tâche A2)
- prix_affiche / prix_vente DVF : décote estimation
- date_premiere_obs / date_mutation DVF : délai vente
- code_insee : clé de jointure DVF ✅

### Terrain
Communes traitées : Givors (69700), Rive-de-Gier (42800)
Communes contrôle : à définir
Période T0 : mai 2026 (premier run)
Période T1 : juillet 2026 (objectif)

### Croisements futurs
- DVF DGFIP : délais et taux de vente réels
- ADEME DPE : enrichir GES manquant SeLoger
- SIRENE : qualifier les entités (taille, ancienneté)
