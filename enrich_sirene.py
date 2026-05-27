"""
enrich_sirene.py — Enrichissement SIRENE des entités agences

Appelle l'API recherche-entreprises.api.gouv.fr (gratuite, sans clé)
pour chaque SIREN connu et met à jour entites avec :
  denomination_sociale, libelle_forme_juridique, code_ape, libelle_ape,
  adresse_siege, effectif_salarie, date_creation_entreprise.

Note : le téléphone n'est PAS dans SIRENE. La colonne est prévue pour
un enrichissement ultérieur depuis les pages LBC boutique / SeLoger profil.

Usage :
  python3 enrich_sirene.py            # enrichit les non traités
  python3 enrich_sirene.py --all      # ré-enrichit tout (refresh)
  python3 enrich_sirene.py --dry-run  # affiche sans écrire
"""

import os, re, sys, time
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])

_API = "https://recherche-entreprises.api.gouv.fr/search"
_DELAY = 0.15  # secondes entre appels — API limite à ~7 req/s


def _fetch(siren: str) -> dict | None:
    try:
        r = requests.get(_API, params={"q": siren, "page": 1, "per_page": 1}, timeout=10)
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        if not results:
            return None
        company = results[0]
        if company.get("siren") != siren:
            return None
        return company
    except Exception as e:
        print(f"    Erreur réseau : {e}")
        return None


def _parse(company: dict) -> dict:
    siege = company.get("siege") or {}
    date_creation = company.get("date_creation")

    updates: dict = {}
    if company.get("nom_complet"):
        updates["denomination_sociale"] = company["nom_complet"]
    if company.get("libelle_nature_juridique"):
        updates["libelle_forme_juridique"] = company["libelle_nature_juridique"]
    if company.get("activite_principale"):
        updates["code_ape"] = company["activite_principale"]
    if company.get("libelle_activite_principale"):
        updates["libelle_ape"] = company["libelle_activite_principale"]
    if siege.get("adresse"):
        updates["adresse_siege"] = siege["adresse"]
    if company.get("tranche_effectif_salarie"):
        updates["effectif_salarie"] = company["tranche_effectif_salarie"]
    if date_creation and re.match(r"\d{4}-\d{2}-\d{2}", date_creation):
        updates["date_creation_entreprise"] = date_creation

    return updates


def enrich(refresh: bool = False, dry_run: bool = False, quiet: bool = False) -> int:
    """Enrichit les entités via SIRENE. Retourne le nombre d'entités enrichies."""
    query = sb.table("entites").select(
        "id, siren, nom_commercial, denomination_sociale"
    ).not_.is_("siren", "null")

    if not refresh:
        query = query.is_("sirene_enrichi_at", "null")

    rows = query.execute().data

    # Dédupliquer par SIREN (plusieurs entités peuvent partager un SIREN — groupe)
    seen: dict[str, list[str]] = {}
    for r in rows:
        siren = r["siren"]
        if re.fullmatch(r"\d{9}", siren or ""):
            seen.setdefault(siren, []).append(r["id"])

    if not quiet:
        print(f"{len(seen)} SIRENs uniques à enrichir ({len(rows)} entités concernées)")
    if dry_run and not quiet:
        print("=== DRY-RUN : aucune écriture ===\n")

    ok = skip = 0
    for siren, ids in seen.items():
        company = _fetch(siren)
        time.sleep(_DELAY)

        if not company:
            if not quiet:
                print(f"  NOT FOUND {siren} ({ids[0][:8]}...)")
            skip += 1
            if not dry_run:
                sb.table("entites").update(
                    {"sirene_enrichi_at": "now()"}
                ).in_("id", ids).execute()
            continue

        updates = _parse(company)
        updates["sirene_enrichi_at"] = "now()"

        if not quiet:
            nom_legal = updates.get("denomination_sociale", "?")
            ape = updates.get("code_ape", "?")
            libelle = updates.get("libelle_ape", "")
            print(f"  {siren} → {nom_legal} [{ape} {libelle}]")

        if not dry_run:
            sb.table("entites").update(updates).in_("id", ids).execute()

        ok += 1

    if not quiet:
        print(f"\nRésultat : {ok} enrichis, {skip} non trouvés")
    return ok


if __name__ == "__main__":
    refresh = "--all" in sys.argv
    dry_run = "--dry-run" in sys.argv
    enrich(refresh=refresh, dry_run=dry_run)
