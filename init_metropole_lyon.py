"""
init_metropole_lyon.py — Initialise zones_ref avec les 59 communes de la Métropole de Lyon.

Pour chaque commune :
  1. Géocode via api-adresse.data.gouv.fr → code_insee + commune_officielle + lat/lon
  2. Upsert dans zones_ref (actif=true, priorite par taille de marché estimée)

Usage :
  python3 init_metropole_lyon.py            # Dry-run : affiche sans écrire
  python3 init_metropole_lyon.py --apply    # Écrit en base
  python3 init_metropole_lyon.py --apply --reset-priorite  # Recalcule les priorités
"""

import os, sys, time, requests
from dotenv import load_dotenv
load_dotenv(override=True)

G   = "\033[92m"
R   = "\033[91m"
Y   = "\033[93m"
B   = "\033[1m"
RST = "\033[0m"

# ---------------------------------------------------------------------------
# Liste officielle des 59 communes de la Métropole de Lyon
# Priorité : 1 = scraper en premier (marché le plus actif)
# Format : (cp, commune, priorite_estimee)
# ---------------------------------------------------------------------------

COMMUNES_METROPOLE = [
    # --- Lyon (9 arrondissements) — déjà dans zones_ref via migration_batch.sql ---
    # Inclus ici pour s'assurer qu'ils sont bien actifs
    ("69001", "Lyon 1er Arrondissement",  1),
    ("69002", "Lyon 2e Arrondissement",   2),
    ("69003", "Lyon 3e Arrondissement",   3),
    ("69004", "Lyon 4e Arrondissement",   4),
    ("69005", "Lyon 5e Arrondissement",   5),
    ("69006", "Lyon 6e Arrondissement",   6),
    ("69007", "Lyon 7e Arrondissement",   7),
    ("69008", "Lyon 8e Arrondissement",   8),
    ("69009", "Lyon 9e Arrondissement",   9),

    # --- Grandes communes (marché actif) ---
    ("69100", "Villeurbanne",             10),
    ("69200", "Vénissieux",               11),
    ("69500", "Bron",                     12),
    ("69120", "Vaulx-en-Velin",           13),
    ("69800", "Saint-Priest",             14),
    ("69140", "Rillieux-la-Pape",         15),
    ("69150", "Décines-Charpieu",         16),
    ("69300", "Caluire-et-Cuire",         17),
    ("69160", "Tassin-la-Demi-Lune",      18),
    ("69190", "Saint-Fons",               19),

    # --- Communes moyennes ---
    ("69130", "Écully",                   20),
    ("69230", "Saint-Genis-Laval",        21),
    ("69340", "Francheville",             22),
    ("69600", "Oullins",                  23),
    ("69780", "Mions",                    24),
    ("69310", "Pierre-Bénite",            25),
    ("69330", "Meyzieu",                  26),
    ("69680", "Chassieu",                 27),
    ("69290", "Craponne",                 28),
    ("69260", "Charbonnières-les-Bains",  29),
    ("69570", "Dardilly",                 30),
    ("69410", "Champagne-au-Mont-d'Or",   31),
    ("69960", "Corbas",                   32),
    ("69320", "Feyzin",                   33),
    ("69540", "Irigny",                   34),
    ("69760", "Limonest",                 35),

    # --- Petites communes ---
    ("69250", "Albigny-sur-Saône",        40),
    ("69390", "Charly",                   41),
    ("69660", "Collonges-au-Mont-d'Or",   42),
    ("69270", "Couzon-au-Mont-d'Or",      43),
    ("69250", "Curis-au-Mont-d'Or",       44),
    ("69250", "Fleurieu-sur-Saône",       45),
    ("69270", "Fontaines-Saint-Martin",   46),
    ("69270", "Fontaines-sur-Saône",      47),
    ("69730", "Genay",                    48),
    ("69700", "Givors",                   36),   # déjà en base
    ("69520", "Grigny",                   37),
    ("69330", "Jonage",                   49),
    ("69330", "Jons",                     50),
    ("69350", "La Mulatière",             51),
    ("69890", "La Tour-de-Salvagny",      52),
    ("69380", "Lissieu",                  53),
    ("69280", "Marcy-l'Étoile",           54),
    ("69390", "Montanay",                 55),
    ("69250", "Neuville-sur-Saône",       56),
    ("69650", "Quincieux",                57),
    ("69270", "Rochetaillée-sur-Saône",   58),
    ("69450", "Saint-Cyr-au-Mont-d'Or",   59),
    ("69370", "Saint-Didier-au-Mont-d'Or", 60),
    ("69290", "Saint-Genis-les-Ollières", 61),
    ("69650", "Saint-Germain-au-Mont-d'Or", 62),
    ("69780", "Saint-Pierre-de-Chandieu", 63),
    ("69270", "Saint-Romain-au-Mont-d'Or", 64),
    ("69280", "Sainte-Consorce",          65),
    ("69580", "Sathonay-Camp",            66),
    ("69580", "Sathonay-Village",         67),
    ("69360", "Solaize",                  68),
    ("69360", "Ternay",                   69),
    ("69780", "Toussieu",                 70),
    ("69390", "Vernaison",                71),
    ("69250", "Poleymieux-au-Mont-d'Or",  72),
]


def geocode(commune: str, cp: str) -> dict | None:
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": commune, "postcode": cp, "type": "municipality", "limit": 1},
            timeout=5,
        )
        features = r.json().get("features", [])
        if not features:
            return None
        props = features[0].get("properties", {})
        lon, lat = features[0]["geometry"]["coordinates"]
        return {
            "code_insee":         props.get("citycode", ""),
            "commune_officielle": props.get("city", commune),
            "lat": lat, "lon": lon,
        }
    except Exception:
        return None


def main():
    apply  = "--apply" in sys.argv
    dry    = not apply

    if dry:
        print(f"\n{B}=== Dry-run — Métropole de Lyon ({len(COMMUNES_METROPOLE)} communes) ==={RST}")
        print(f"  Passe {Y}--apply{RST} pour écrire en base.\n")
    else:
        print(f"\n{B}=== Init zones_ref — Métropole de Lyon ==={RST}\n")
        from supabase import create_client
        sb = create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])

    ok = err = skip = 0
    rows = []

    for cp, commune, priorite in COMMUNES_METROPOLE:
        print(f"  Géocodage {commune} ({cp})...", end=" ", flush=True)
        geo = geocode(commune, cp)

        if not geo or not geo["code_insee"]:
            print(f"{R}ECHEC — introuvable{RST}")
            err += 1
            continue

        print(f"{G}OK{RST} → {geo['commune_officielle']} ({geo['code_insee']})")
        rows.append({
            "code_insee":         geo["code_insee"],
            "cp":                 cp,
            "commune":            geo["commune_officielle"],
            "commune_officielle": geo["commune_officielle"],
            "actif":              True,
            "priorite":           priorite,
        })
        ok += 1
        time.sleep(0.05)  # respecter le rate limit de l'API

    print(f"\n  {ok} géocodées | {err} échecs\n")

    if dry:
        print(f"  {B}Aperçu (10 premières) :{RST}")
        for r in rows[:10]:
            print(f"    {r['code_insee']} | {r['cp']} | {r['commune_officielle']:35s} | priorité {r['priorite']}")
        print(f"\n  → Relance avec {Y}--apply{RST} pour insérer les {len(rows)} lignes en base.")
        return

    # Upsert en base
    print(f"  Upsert {len(rows)} communes dans zones_ref...")
    inserted = updated = 0
    for row in rows:
        try:
            existing = sb.table("zones_ref").select("code_insee, actif, priorite") \
                         .eq("code_insee", row["code_insee"]).execute().data
            if existing:
                sb.table("zones_ref").update({
                    "cp":                 row["cp"],
                    "commune":            row["commune"],
                    "commune_officielle": row["commune_officielle"],
                    "actif":              row["actif"],
                    "priorite":           row["priorite"],
                }).eq("code_insee", row["code_insee"]).execute()
                updated += 1
            else:
                sb.table("zones_ref").insert(row).execute()
                inserted += 1
        except Exception as e:
            print(f"  {R}ERR{RST} {row['commune']} : {e}")
            err += 1

    print(f"\n  {G}✅ {inserted} insérées | {updated} mises à jour | {err} erreurs{RST}")
    print(f"\n  Vérifie avec :")
    print(f"    SELECT cp, commune, actif, priorite FROM zones_ref ORDER BY priorite;")
    print(f"\n  Lance ensuite le batch :")
    print(f"    python3 pipeline.py batch --min-credits 500")


if __name__ == "__main__":
    main()
