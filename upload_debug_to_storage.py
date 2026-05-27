"""
upload_debug_to_storage.py — Upload les HTML de debug/ vers Supabase Storage.

Prérequis :
  1. Créer le bucket 'debug-html' dans le dashboard Supabase (Storage → New bucket → Private)
  2. Si erreur 403 avec la clé anon : exporter SUPA_SERVICE_KEY=<service_role_key>
     et relancer — le script utilisera cette clé en priorité.

Usage :
  python3 upload_debug_to_storage.py
"""

from dotenv import load_dotenv
load_dotenv()

import os
from pathlib import Path
from supabase import create_client

HERE  = Path(__file__).parent
DEBUG = HERE / "debug"
BUCKET = "debug-html"

key = os.environ.get("SUPA_SERVICE_KEY") or os.environ["SUPA_KEY"]
sb  = create_client(os.environ["SUPA_URL"], key)

files = sorted(DEBUG.glob("*.html"))
if not files:
    print("Aucun fichier HTML trouvé dans debug/")
    raise SystemExit(0)

print(f"Bucket : {BUCKET}")
print(f"Fichiers à uploader : {len(files)}\n")

ok = err = 0
for f in files:
    with open(f, "rb") as fh:
        content = fh.read()
    try:
        sb.storage.from_(BUCKET).upload(
            path=f.name,
            file=content,
            file_options={"content-type": "text/html", "upsert": "true"},
        )
        print(f"  OK   {f.name}  ({len(content)//1024} Ko)")
        ok += 1
    except Exception as e:
        print(f"  ERR  {f.name} : {e}")
        err += 1

print(f"\n{'─'*50}")
print(f"  {ok} fichiers uploadés")
if err:
    print(f"  {err} erreurs — vérifier les droits du bucket ou la clé API")
else:
    print(f"  Aucune erreur — tu peux supprimer debug/*.html")
