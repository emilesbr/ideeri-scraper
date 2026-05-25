import os
import sys
import json
import re
import uuid
import time
import requests
import lzstring
from dotenv import load_dotenv
from supabase import create_client

# Chargement de l'environnement
load_dotenv()
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
SCRAPINGBEE_KEY = os.getenv("SCRAPINGBEE_KEY")

if not SUPA_URL or not SUPA_KEY or not SCRAPINGBEE_KEY:
    print("❌ Configuration manquante dans le fichier .env")
    sys.exit(1)

supabase = create_client(SUPA_URL, SUPA_KEY)
lz = lzstring.LZString()

def nettoyer_nom_agence(nom):
    if not nom: return "PARTICULIER"
    nom = nom.upper()
    parasites = ["AGENCE", "IMMOBILIER", "IMMO", "LYON", "CONSEIL", "GROUPE", "PROPRIETES", "SQUARE", "HABITAT", "PLAZA"]
    for p in parasites: nom = nom.replace(p, "")
    return re.sub(r'[^A-Z0-9]', '', nom).strip()

def fetch_page(page_num):
    base_url = "https://www.seloger.com/classified-search?classifiedBusiness=Professional&distributionTypes=Buy&estateTypes=House,Apartment&locations=AD08FR28889&projectTypes=Resale"
    url_cible = base_url if page_num == 1 else f"{base_url}&page={page_num}"
    
    print(f"\n🚀 [Page {page_num}] Extraction via ScrapingBee...")
    
    sb_url = "https://app.scrapingbee.com/api/v1/"
    params = {
        'api_key': SCRAPINGBEE_KEY,
        'url': url_cible,
        'stealth_proxy': 'true',
        'country_code': 'fr',
        'render_js': 'true',
        'wait': '5000',
        'premium_proxy': 'true'
    }
    
    try:
        res = requests.get(sb_url, params=params, timeout=120)
        if res.status_code == 200:
            return res.text
        print(f"  ⚠️  Erreur HTTP ScrapingBee : {res.status_code}")
        return None
    except Exception as e:
        print(f"  ❌  Erreur de connexion : {e}")
        return None

def extraire_et_sauvegarder(html, cp="69005"):
    match = re.search(r'window\["__UFRN_FETCHER__"\]\s*=\s*JSON\.parse\("(.*?)"\)', html)
    if not match:
        print("  ⚠️  Impossible de localiser la variable window.__UFRN_FETCHER__")
        return 0, False
        
    try:
        raw_text = match.group(1).replace('\\"', '"').replace('\\\\', '\\')
        ufrn_json = json.loads(raw_text)
        
        compressed_data = ufrn_json.get("data", {}).get("classified-serp-init-data")
        if not compressed_data:
            print("  ⚠️  Clé 'classified-serp-init-data' introuvable.")
            return 0, False
            
        decompressed = lz.decompressFromBase64(compressed_data)
        data = json.loads(decompressed)
        
        # AJUSTEMENT MAJEUR : On cible 'classifieds' à l'intérieur de 'pageProps'
        items = data.get("pageProps", {}).get("classifieds", [])
    except Exception as e:
        print(f"  ❌  Erreur lors du décodage/décompression des données : {e}")
        return 0, False

    if not items:
        print("  ℹ️  Plus aucune annonce disponible sur cette page.")
        return 0, False

    count = 0
    for ad in items:
        try:
            # Extraction des informations selon la structure de l'annonce
            nom_brut = ad.get('professional', {}).get('name') or ad.get('owner', {}).get('name', 'PARTICULIER')
            prix = int(ad.get('price', 0))
            surf = int(ad.get('surface', 0))
            if prix == 0 or surf == 0: continue
            
            nom_clean = nettoyer_nom_agence(nom_brut)
            signature = f"{nom_clean}_{cp}_{surf}_{prix}"
            
            payload = {
                "id_annonce": str(uuid.uuid4()),
                "signature_entite_bien": signature,
                "nom_commercial": nom_brut,
                "prix_affiche": prix,
                "surface": surf,
                "source": "SeLoger",
                "sur_lbc": False,
                "sur_seloger": True,
                "code_postal": str(cp),
                "est_active": True,
                "date_derniere_obs": "now()"
            }
            
            supabase.table("annonces").upsert(payload, on_conflict="signature_entite_bien").execute()
            print(f"  ✓  Enregistré : {nom_clean[:15]}... | {prix} € | {surf} m²")
            count += 1
        except Exception as ex:
            continue
            
    return count, True

if __name__ == "__main__":
    print("============================================================")
    print("  Scraper Ideeri - Moteur d'Extraction Exhaustif UFRN")
    print("============================================================")
    
    page = 1
    total_general = 0
    
    while True:
        html_source = fetch_page(page)
        if not html_source:
            print("❌ Échec de la récupération de la page. Fin du processus.")
            break
            
        nb_insere, continuer = extraire_et_sauvegarder(html_source)
        print(f"📊 Résultat Page {page} : {nb_insere} annonces synchronisées.")
        
        if not continuer or nb_insere == 0:
            print("\n🏁 Fin de la pagination atteinte avec succès !")
            break
            
        total_general += nb_insere
        page += 1
        
        print("😴 Pause de sécurité de 4 secondes...")
        time.sleep(4)

    print("\n============================================================")
    print(f" 🎉 Mission terminée ! Total général : {total_general} annonces.")
    print("============================================================")

