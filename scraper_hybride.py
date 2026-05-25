"""
scraper_hybride.py
- LBC : requête HTTP directe (pas besoin de ScrapingBee)
- SeLoger : ScrapingBee avec render_js=True

Économie : ~140 crédits/CP sur LBC (0 au lieu de ~140)

Usage :
    python scraper_hybride.py 69005
    python scraper_hybride.py 69001 69002 69003

Dépendances :
    pip install requests lzstring supabase python-dotenv

.env :
    SUPA_URL=https://ton-projet.supabase.co
    SUPA_KEY=ta-cle-anon
    SCRAPINGBEE_KEY=ta-cle-scrapingbee   # utilisé uniquement pour SeLoger
"""

import json, re, sys, uuid, time, os
from datetime import date
import requests

try:
    import lzstring
except ImportError:
    sys.exit("❌  pip install lzstring")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from supabase import create_client
except ImportError:
    sys.exit("❌  pip install supabase")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPA_URL        = os.environ.get("SUPA_URL")
SUPA_KEY        = os.environ.get("SUPA_KEY")
SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_KEY")

for var, val in [("SUPA_URL", SUPA_URL), ("SUPA_KEY", SUPA_KEY), ("SCRAPINGBEE_KEY", SCRAPINGBEE_KEY)]:
    if not val:
        sys.exit(f"❌  Variable manquante : {var}")

supabase = create_client(SUPA_URL, SUPA_KEY)

DELAI = 2  # secondes entre requêtes

# Headers navigateur pour LBC (requête directe)
HEADERS_LBC = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.leboncoin.fr/",
}

SELOGER_SLUGS = {
    "69001": "lyon-1er-69",       "69002": "lyon-2eme-69",
    "69003": "lyon-3eme-69",      "69004": "lyon-4eme-69",
    "69005": "lyon-5eme-69",      "69006": "lyon-6eme-69",
    "69007": "lyon-7eme-69",      "69008": "lyon-8eme-69",
    "69009": "lyon-9eme-69",      "69100": "villeurbanne-69",
    "69110": "sainte-foy-les-lyon-69", "69130": "ecully-69",
    "69150": "decines-charpieu-69","69160": "tassin-la-demi-lune-69",
    "69200": "venissieux-69",      "69230": "saint-genis-laval-69",
    "69300": "caluire-et-cuire-69","69340": "francheville-69",
    "69370": "saint-didier-au-mont-dor-69","69400": "villefranche-sur-saone-69",
    "69450": "saint-cyr-au-mont-d-or-69","69500": "bron-69",
    "69570": "dardilly-69",        "69600": "oullins-69",
    "69800": "saint-priest-69",
}

# ---------------------------------------------------------------------------
# Utilitaires communs
# ---------------------------------------------------------------------------

def nettoyer(nom: str) -> str:
    if not nom: return "PARTICULIER"
    nom = nom.upper()
    return re.sub(r'[^A-Z0-9]', '', nom).strip() or "PARTICULIER"

def extraire_quartier_url(url: str) -> str | None:
    if not url: return None
    skip = {'achat','vente','appartement','maison','lyon','seloger',
            'bellesdemeures','www','com','fr','annonces'}
    for p in reversed(url.rstrip('/').split('/')):
        if re.match(r'^\d+\.htm', p) or re.match(r'^\d+$', p): continue
        if p.endswith('.htm'): p = p.rsplit('.',1)[0]
        if p.lower() in skip or len(p) < 4: continue
        if 'lyon' in p.lower() and re.search(r'\d', p): continue
        return p
    return None

def upsert(payload: dict, source: str) -> None:
    try:
        supabase.table("annonces").upsert(
            payload, on_conflict="signature_entite_bien"
        ).execute()
        print(f"    ✓ {source:<8} {payload['nom_commercial'][:30]:<30} {payload['prix_affiche']:>8} € {payload.get('surface') or '?':>4}m²")
    except Exception as e:
        print(f"    ✗ upsert : {e}")

# ---------------------------------------------------------------------------
# LBC — requête directe (sans ScrapingBee)
# ---------------------------------------------------------------------------

LBC_URL = "https://www.leboncoin.fr/cl/ventes_immobilieres/cp_lyon_{cp}/real_estate_type:2/p-{page}"

def fetch_lbc_direct(url: str) -> str | None:
    """Requête HTTP directe vers LBC — pas de ScrapingBee nécessaire."""
    try:
        r = requests.get(url, headers=HEADERS_LBC, timeout=30)
        if r.status_code == 200:
            return r.text
        print(f"  ⚠️  LBC HTTP {r.status_code}")
        return None
    except Exception as e:
        print(f"  ❌  {e}")
        return None

def parser_lbc(html: str, cp: str) -> tuple[list[dict], int]:
    """
    Extrait les annonces depuis le HTML LBC.
    Tente d'abord __NEXT_DATA__ (données JSON complètes),
    puis fallback sur parsing HTML/regex.
    """
    # Méthode 1 : __NEXT_DATA__ (données structurées côté serveur)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            search = (data.get('props',{}).get('pageProps',{}).get('searchData',{}))
            ads   = search.get('ads', [])
            total = search.get('total', 0)
            if ads:
                return ads, total
        except:
            pass

    # Méthode 2 : parsing regex du HTML (fallback si JS a modifié la structure)
    total_m = re.search(r'(\d+)\s+annonces', html)
    total = int(total_m.group(1)) if total_m else 0

    annonces = []
    blocs = re.split(r'/ad/ventes_immobilieres/(\d+)', html)
    for i in range(1, len(blocs), 2):
        list_id = blocs[i]
        bloc    = blocs[i+1][:600] if i+1 < len(blocs) else ""

        prix_m = re.search(r'([\d\u202f\xa0\s]{3,12})(?:\s*€|\s*euros)', bloc)
        if not prix_m: continue
        try:
            prix = int(re.sub(r'\D', '', prix_m.group(1)))
        except: continue
        if prix < 10_000: continue

        surf_m  = re.search(r'(\d+)\s*m²', bloc)
        surf    = int(surf_m.group(1)) if surf_m else None
        pieces_m= re.search(r'(\d+)\s*pièces?', bloc)
        pieces  = int(pieces_m.group(1)) if pieces_m else None
        dpe_m   = re.search(r'énergie ([A-G])', bloc)
        dpe     = dpe_m.group(1) if dpe_m else None
        agence_m= re.search(r'\*\*([^*\n]+)\*\*', bloc)
        nom_brut= agence_m.group(1).strip() if agence_m else "PARTICULIER"
        nom_brut= re.sub(r"aujourd'hui.*|hier.*", "", nom_brut).strip() or "PARTICULIER"

        quartier_m = re.search(r'Lyon\s+69005\s+([\w\s-]+?)(?:\n|Située)', bloc)
        quartier = quartier_m.group(1).strip() if quartier_m else None
        if quartier and ('rrondissement' in quartier or len(quartier) > 30):
            quartier = None

        annonces.append({
            '_list_id': list_id,
            '_prix': prix, '_surf': surf, '_pieces': pieces,
            '_dpe': dpe, '_nom': nom_brut, '_quartier': quartier,
            '_baisse': 'Baisse de prix' in bloc,
        })
    return annonces, total

def traiter_lbc_nextdata(ads: list[dict], cp: str) -> int:
    """Traitement depuis __NEXT_DATA__ (format structuré)."""
    nb = 0
    for ad in ads:
        try:
            nom_brut   = ad.get('owner', {}).get('name', 'PARTICULIER')
            prix_list  = ad.get('price', [])
            prix       = prix_list[0] if prix_list else 0
            if not prix or prix < 10_000: continue

            titre   = ad.get('subject', '')
            surf_m  = re.search(r'(\d+)\s*m²', titre)
            surface = int(surf_m.group(1)) if surf_m else None
            if not surface:
                for attr in ad.get('attributes', []):
                    if attr.get('key') == 'square':
                        try: surface = int(attr['value'])
                        except: pass
                        break

            nom_clean = nettoyer(nom_brut)
            sig = f"{nom_clean}_{cp}_{surface or 0}_{prix}"
            today = str(date.today())

            upsert({
                "id_annonce": str(uuid.uuid4()),
                "siret_emetteur": None, "source": "LBC",
                "type_bien": "Appartement",
                "prix_affiche": prix, "surface": surface,
                "code_postal": cp,
                "date_publication": today, "date_derniere_obs": today,
                "est_active": True, "date_premiere_obs": today,
                "prix_m2_affiche": round(prix/surface) if surface else None,
                "quartier_calcule": None,
                "id_unique_bien": str(ad.get('list_id','')),
                "id_annonce_parente": None,
                "sur_lbc": True, "sur_seloger": False,
                "signature_entite_bien": sig,
                "nom_commercial": nom_brut,
            }, "LBC")
            nb += 1
        except Exception as e:
            print(f"    ✗ annonce LBC ignorée : {e}")
    return nb

def traiter_lbc_regex(annonces: list[dict], cp: str) -> int:
    """Traitement depuis parsing regex (fallback)."""
    nb = 0
    today = str(date.today())
    for a in annonces:
        try:
            nom_clean = nettoyer(a['_nom'])
            sig = f"{nom_clean}_{cp}_{a['_surf'] or 0}_{a['_prix']}"
            upsert({
                "id_annonce": str(uuid.uuid4()),
                "siret_emetteur": None, "source": "LBC",
                "type_bien": "Appartement",
                "prix_affiche": a['_prix'], "surface": a['_surf'],
                "code_postal": cp,
                "date_publication": today, "date_derniere_obs": today,
                "est_active": True, "date_premiere_obs": today,
                "prix_m2_affiche": round(a['_prix']/a['_surf']) if a['_surf'] else None,
                "quartier_calcule": a['_quartier'],
                "id_unique_bien": a['_list_id'],
                "id_annonce_parente": None,
                "sur_lbc": True, "sur_seloger": False,
                "signature_entite_bien": sig,
                "nom_commercial": a['_nom'],
            }, "LBC")
            nb += 1
        except Exception as e:
            print(f"    ✗ annonce regex ignorée : {e}")
    return nb

def scraper_lbc(cp: str) -> int:
    total_insere, page, total_connu = 0, 1, None
    while True:
        url = LBC_URL.format(cp=cp, page=page)
        print(f"  📄  LBC p.{page} — {url}")
        html = fetch_lbc_direct(url)
        if not html:
            print("  ⚠️  HTML vide, arrêt")
            break

        data, total = parser_lbc(html, cp)
        if total_connu is None:
            total_connu = total
            print(f"  ℹ️  {total} annonces → ~{-(-total//35)} pages")

        if not data:
            print(f"  ℹ️  Page {page} vide, fin pagination")
            break

        # Détecter le format (list de dicts __NEXT_DATA__ ou liste regex)
        if data and isinstance(data[0], dict) and 'owner' in data[0]:
            nb = traiter_lbc_nextdata(data, cp)
        else:
            nb = traiter_lbc_regex(data, cp)

        total_insere += nb
        print(f"  → {nb} traitées (page {page})")

        if total_connu and page * 35 >= total_connu:
            break
        page += 1
        time.sleep(DELAI)
    return total_insere

# ---------------------------------------------------------------------------
# SeLoger — ScrapingBee (render_js requis)
# ---------------------------------------------------------------------------

def fetch_seloger_scrapingbee(url: str) -> str | None:
    try:
        r = requests.get("https://app.scrapingbee.com/api/v1/", params={
            "api_key": SCRAPINGBEE_KEY, "url": url,
            "render_js": "true", "premium_proxy": "true",
            "country_code": "fr", "block_ads": "true", "wait": "3000",
        }, timeout=60)
        if r.status_code == 200:
            return r.text
        print(f"  ⚠️  ScrapingBee HTTP {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        print(f"  ❌  {e}")
        return None

def extraire_seloger(html: str) -> tuple[list, int]:
    m = re.search(r'window\["__UFRN_FETCHER__"\]=JSON\.parse\("(.*?)"\);', html, re.DOTALL)
    if not m: return [], 0
    try:
        raw     = m.group(1).encode().decode('unicode_escape')
        fetcher = json.loads(raw)
        serp_b64= fetcher['data']['classified-serp-init-data']
        lz      = lzstring.LZString()
        parsed  = json.loads(lz.decompressFromBase64(serp_b64))
        props   = parsed['pageProps']
        return list(props['classifiedsData'].values()), props.get('totalCount', 0)
    except Exception as e:
        print(f"  ⚠️  SeLoger parse : {e}")
        return [], 0

def traiter_seloger(annonces: list, cp: str) -> int:
    nb = 0
    for c in annonces:
        try:
            lt = c.get('legacyTracking', {}); t = c.get('tracking', {})
            rd = c.get('rawData', {}); meta = c.get('metadata', {})
            card = c.get('cardProvider', {}); provider = c.get('provider', {})

            prix = t.get('price') or rd.get('price', 0)
            if not prix or prix < 10_000: continue

            surf_raw = lt.get('space') or rd.get('surface', {}).get('area')
            surface  = int(float(surf_raw)) if surf_raw else None

            nom_brut = (card.get('title')
                or provider.get('intermediaryCard', {}).get('title')
                or provider.get('address', 'PARTICULIER'))

            nom_clean = nettoyer(nom_brut)
            sig = f"{nom_clean}_{cp}_{surface or 0}_{prix}"
            date_pub = meta.get('creationDate','')[:10] or None
            date_obs = meta.get('updateDate','')[:10] or None

            upsert({
                "id_annonce": str(uuid.uuid4()),
                "siret_emetteur": None, "source": "SeLoger",
                "type_bien": rd.get('propertyTypeLabel'),
                "prix_affiche": prix, "surface": surface,
                "code_postal": cp,
                "date_publication": date_pub, "date_derniere_obs": date_obs,
                "est_active": True, "date_premiere_obs": date_pub,
                "prix_m2_affiche": round(prix/surface) if surface else None,
                "quartier_calcule": extraire_quartier_url(c.get('url','')),
                "id_unique_bien": meta.get('legacyId'),
                "id_annonce_parente": None,
                "sur_lbc": False, "sur_seloger": True,
                "signature_entite_bien": sig, "nom_commercial": nom_brut,
            }, "SeLoger")
            nb += 1
        except Exception as e:
            print(f"    ✗ annonce SeLoger ignorée : {e}")
    return nb

def scraper_seloger(cp: str) -> int:
    slug = SELOGER_SLUGS.get(cp)
    if not slug:
        print(f"  ⚠️  Pas de slug pour {cp}")
        return 0
    total_insere, page, total_connu = 0, 1, None
    url_tpl = "https://www.seloger.com/achat/appartements-maisons/{slug}/?types=1,2&projects=2&enterprise=0&order=score&page={page}"
    while True:
        url = url_tpl.format(slug=slug, page=page)
        print(f"  📄  SeLoger p.{page} — {url}")
        html = fetch_seloger_scrapingbee(url)
        if not html: break
        annonces, total = extraire_seloger(html)
        if total_connu is None:
            total_connu = total
            print(f"  ℹ️  {total} annonces → ~{-(-total//30)} pages")
        if not annonces:
            print(f"  ℹ️  Page {page} vide, fin")
            break
        nb = traiter_seloger(annonces, cp)
        total_insere += nb
        print(f"  → {nb} traitées (page {page})")
        if total_connu and page * 30 >= total_connu: break
        page += 1
        time.sleep(DELAI)
    return total_insere

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    codes = sys.argv[1:] if len(sys.argv) > 1 else ["69005"]
    print(f"\n{'='*60}\n  Scraper Ideeri hybride\n  CPs : {', '.join(codes)}\n{'='*60}")
    grand_total = 0
    for cp in codes:
        print(f"\n{'─'*60}\n📍  {cp}\n{'─'*60}")
        print("\n🟠  LBC (requête directe)")
        n_lbc = scraper_lbc(cp)
        print(f"\n🔵  SeLoger (ScrapingBee)")
        n_sl  = scraper_seloger(cp)
        sous  = n_lbc + n_sl
        grand_total += sous
        print(f"\n  ✅  {cp} : {n_lbc} LBC + {n_sl} SeLoger = {sous}")
    print(f"\n{'='*60}\n  🎉  {grand_total} annonces upsertées\n{'='*60}\n")

if __name__ == "__main__":
    main()
