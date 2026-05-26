import json
import re
import lzstring

lz = lzstring.LZString()

print("🔍 Lecture du fichier capture_seloger.html...")
with open("capture_seloger.html", "r", encoding="utf-8") as f:
    html = f.read()

match = re.search(r'window\["__UFRN_FETCHER__"\]\s*=\s*JSON\.parse\("(.*?)"\)', html)
if not match:
    print("❌ Impossible de trouver __UFRN_FETCHER__")
    sys.exit(1)

raw_text = match.group(1).replace('\\"', '"').replace('\\\\', '\\')
ufrn_json = json.loads(raw_text)

compressed_data = ufrn_json.get("data", {}).get("classified-serp-init-data")
if compressed_data:
    print("📦 Clé 'classified-serp-init-data' trouvée.")
    decompressed = lz.decompressFromBase64(compressed_data)
    data = json.loads(decompressed)
    
    print("\n--- ANALYSE DES CLÉS DISPONIBLES DANS LE BLOC DECOMPRESSE ---")
    print(list(data.keys()))
    
    # On cherche s'il y a une clé qui ressemble à des annonces (results, listings, items, etc.)
    for k in data.keys():
        if isinstance(data[k], list):
            print(f"🔹 Clé '{k}' contient une LISTE de {len(data[k])} éléments (Piste très sérieuse !)")
        elif isinstance(data[k], dict):
            print(f"🔸 Clé '{k}' contient un DICTIONNAIRE avec les sous-clés : {list(data[k].keys())}")
else:
    print("❌ 'classified-serp-init-data' est vide. Voici les clés réelles du bloc 'data' global :")
    print(list(ufrn_json.get("data", {}).keys()))

