import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()
SCRAPINGBEE_KEY = os.getenv("SCRAPINGBEE_KEY")

if not SCRAPINGBEE_KEY:
    print("❌ Clé ScrapingBee manquante dans le .env")
    sys.exit(1)

# L'URL exacte que tu m'as donnée pour la page 1
url_cible = "https://www.seloger.com/classified-search?classifiedBusiness=Professional&distributionTypes=Buy&estateTypes=House,Apartment&locations=AD08FR28889&projectTypes=Resale"

print("🚀 Lancement de la capture de la page SeLoger (Mode Ultra-Patient)...")

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
    # Changement crucial : timeout=120 pour laisser 2 minutes au chargement
    res = requests.get(sb_url, params=params, timeout=120)
    
    if res.status_code == 200:
        with open("capture_seloger.html", "w", encoding="utf-8") as f:
            f.write(res.text)
        print("✅ Code source capturé avec succès dans 'capture_seloger.html' !")
    else:
        print(f"❌ Échec de la capture. Code HTTP ScrapingBee : {res.status_code}")
        if res.text:
            print(f"Retour de l'API : {res.text[:200]}")
except Exception as e:
    print(f"❌ Erreur : {e}")

