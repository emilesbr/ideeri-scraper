"""
process_from_storage.py — Récupère les HTML depuis Supabase Storage,
les parse et les insère dans stg_lbc / stg_seloger sans re-scraper.
Puis lance le transform pour chaque CP ayant de nouvelles données.
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()

from supabase import create_client
from datetime import datetime, timezone
from collections import defaultdict
from pipeline import _parse_lbc, _parse_seloger, cmd_run

SUPA_URL = os.environ["SUPA_URL"]
SUPA_KEY = os.environ["SUPA_KEY"]
SVC_KEY  = os.environ.get("SUPA_SERVICE_KEY", SUPA_KEY)

sb_anon = create_client(SUPA_URL, SUPA_KEY)
sb_svc  = create_client(SUPA_URL, SVC_KEY)

COMMUNE_MAP = {
    "69001": ("Lyon 1er",                "Lyon 1er"),
    "69002": ("Lyon 2eme",               "Lyon 2ème"),
    "69003": ("Lyon 3eme",               "Lyon 3ème"),
    "69130": ("Écully",                  "Écully"),
    "69260": ("Charbonnières-les-Bains", "Charbonnières-les-Bains"),
    "69290": ("Craponne",                "Craponne"),
    "69570": ("Dardilly",                "Dardilly"),
    "69700": ("Givors",                  "Givors"),
    "42800": ("Rive-de-Gier",            "Rive-de-Gier"),
    "42410": ("Pélussin",                "Pélussin"),
}

def list_bucket():
    files = sb_svc.storage.from_("debug-html").list(options={"limit": 1000})
    grouped = defaultdict(dict)  # (src, cp) → {page: (name, updated_at)}
    for f in files:
        m = re.match(r'(seloger|lbc)_(\d+)_p(\d+)(_retry)?\.html', f["name"])
        if not m:
            continue
        src, cp, page = m.group(1), m.group(2), int(m.group(3))
        updated = f.get("updated_at", "")
        existing = grouped[(src, cp)].get(page)
        if not existing or updated > existing[1]:
            grouped[(src, cp)][page] = (f["name"], updated)
    return grouped

def download(filename: str) -> str:
    data = sb_svc.storage.from_("debug-html").download(filename)
    return data.decode("utf-8", errors="replace")

def insert_stg(table: str, commune: str, cp: str, page: int, raw: dict, nb: int):
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "scraped_at": now,
        "code_postal": cp,
        "page": page,
        "url_source": f"storage:{table}/{cp}_p{page}",
        "nb_annonces": nb,
        "processed": False,
        "data_brute": {**raw, "_meta": {
            "code_postal": cp,
            "commune": commune,
            "page": page,
            "source": "storage",
            "scraped_at": now,
        }},
    }
    sb_anon.table(table).insert(row).execute()

def main():
    grouped = list_bucket()
    print(f"Fichiers trouvés : {sum(len(v) for v in grouped.values())} pages uniques\n")

    processed_cps = set()

    for (src, cp), pages in sorted(grouped.items()):
        commune, _ = COMMUNE_MAP.get(cp, (cp, cp))
        table = "stg_seloger" if src == "seloger" else "stg_lbc"
        parser = _parse_seloger if src == "seloger" else _parse_lbc

        print(f"\n{src:8s} {cp} {commune} — {len(pages)} pages")
        ok, skip, err = 0, 0, 0

        # Pages déjà processed=True → skip (déjà traitées par le transform)
        existing_today = {
            r["page"]
            for r in sb_anon.table(table).select("page").eq("code_postal", cp).eq("processed", True).execute().data
        }

        for page in sorted(pages):
            filename, updated = pages[page]
            if page in existing_today:
                print(f"  p{page:2d} : déjà processed — skip")
                skip += 1
                continue
            try:
                html = download(filename)
                items, raw, total = parser(html)
                if not items:
                    print(f"  p{page:2d} : 0 annonces — skip")
                    skip += 1
                    continue
                insert_stg(table, commune, cp, page, raw, len(items))
                print(f"  p{page:2d} : {len(items):3d} ann. insérées ✅  ({updated[:10]})")
                ok += 1
                processed_cps.add(cp)
            except Exception as e:
                print(f"  p{page:2d} : ERREUR — {e}")
                err += 1

        print(f"  → {ok} OK / {skip} vides / {err} erreurs")

    print(f"\n{'='*50}")
    print(f"CPs à transformer : {sorted(processed_cps)}")
    print(f"{'='*50}")

    for cp in sorted(processed_cps):
        commune, _ = COMMUNE_MAP.get(cp, (cp, cp))
        print(f"\nTransform {cp} {commune}...")
        try:
            import subprocess
            subprocess.run(
                ["python3", "pipeline.py", "transform", cp, commune],
                cwd=os.path.dirname(__file__),
            )
        except Exception as e:
            print(f"  ERREUR transform {cp} : {e}")

    print("\n✓ Terminé.")

if __name__ == "__main__":
    main()
