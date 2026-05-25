"""
Scraping exhaustif Givors (69700) — SeLoger 4 pages + LBC 4 pages.
Insère dans stg_lbc / stg_seloger avec métadonnées embarquées dans data_brute
(compatible avec le schéma actuel sans migration).
"""

import os, re, json, math, lzstring, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from pathlib import Path
from supabase import create_client

load_dotenv()
KEY  = os.environ["SCRAPINGBEE_KEY"]
SB   = "https://app.scrapingbee.com/api/v1/"
sb   = create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])
DEBUG = Path(__file__).parent / "debug"
DEBUG.mkdir(exist_ok=True)

COMMUNE      = "Givors"
CODE_POSTAL  = "69700"
NB_PAGES_SL  = 4   # 119 annonces / 30 par page
NB_PAGES_LBC = 4   # 117 annonces / 35 par page

SL_BASE  = (
    "https://www.seloger.com/classified-search"
    "?classifiedBusiness=Professional&distributionTypes=Buy"
    "&estateTypes=House,Apartment&locations=AD08FR28776&projectTypes=Resale"
)
LBC_BASE = (
    "https://www.leboncoin.fr/cl/ventes_immobilieres"
    "/cp_givors_69700/real_estate_type:2"
)

# ---------------------------------------------------------------------------
# Construction des pages
# ---------------------------------------------------------------------------

def build_pages() -> list[dict]:
    pages = []
    for p in range(1, NB_PAGES_SL + 1):
        url = SL_BASE if p == 1 else f"{SL_BASE}&page={p}"
        pages.append({
            "id": f"seloger_givors_p{p}",
            "source": "seloger",
            "page": p,
            "url": url,
            "params": {"render_js": "false", "premium_proxy": "true", "country_code": "fr"},
            "timeout": 45,
        })
    for p in range(1, NB_PAGES_LBC + 1):
        pages.append({
            "id": f"lbc_givors_p{p}",
            "source": "lbc",
            "page": p,
            "url": f"{LBC_BASE}/p-{p}",
            "params": {
                "render_js": "true", "stealth_proxy": "true",
                "wait": "4000", "block_resources": "false",
            },
            "timeout": 120,
        })
    return pages

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(page: dict) -> dict:
    try:
        r = requests.get(SB, params={"api_key": KEY, "url": page["url"], **page["params"]},
                         timeout=page["timeout"])
        html = r.text
        (DEBUG / f"{page['id']}.html").write_text(html, encoding="utf-8")
        return {**page, "status": r.status_code, "html": html, "error": None}
    except Exception as e:
        return {**page, "status": None, "html": "", "error": str(e)}

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_lbc(html: str) -> tuple[list[dict], dict]:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.DOTALL)
    if not m:
        return [], {}
    d    = json.loads(m.group(1))
    sd   = d.get("props", {}).get("pageProps", {}).get("searchData", {})
    ads  = sd.get("ads", [])
    total = sd.get("total", len(ads))
    parsed = [
        {
            "id":        str(a.get("list_id", "")),
            "titre":     a.get("subject", ""),
            "prix":      a.get("price", [None])[0] if a.get("price") else None,
            "surface":   _lbc_attr(a, "square"),
            "nb_pieces": _lbc_attr(a, "rooms"),
            "cp":        a.get("location", {}).get("zipcode", ""),
            "ville":     a.get("location", {}).get("city", ""),
            "url":       a.get("url", ""),
        }
        for a in ads
    ]
    return parsed, {"ads": ads, "total_count": total}

def _lbc_attr(ad, key):
    return next((a.get("value") for a in ad.get("attributes", []) if a.get("key") == key), None)

def parse_seloger(html: str) -> tuple[list[dict], dict]:
    m = re.search(r'<script id="__UFRN_FETCHER__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return [], {}
    inner = re.search(r'JSON\.parse\("(.+)"\)', m.group(1), re.DOTALL)
    if not inner:
        return [], {}
    decoded = inner.group(1).encode().decode("unicode_escape")
    b64 = json.loads(decoded).get("data", {}).get("classified-serp-init-data", "")
    if not b64:
        return [], {}
    raw = lzstring.LZString().decompressFromBase64(b64)
    if not raw:
        return [], {}
    pp    = json.loads(raw).get("pageProps", {})
    ids   = pp.get("classifieds", [])
    cd    = pp.get("classifiedsData", {})
    total = pp.get("totalCount", len(ids))
    parsed = []
    for cid in ids:
        item = cd.get(cid)
        if not isinstance(item, dict):
            continue
        hf   = item.get("hardFacts", {})
        loc  = item.get("location", {}).get("address", {})
        facts = {f["type"]: f.get("splitValue") for f in hf.get("facts", [])}
        pr   = hf.get("price", {}).get("ariaLabel", "")
        prix = int(re.sub(r"[^\d]", "", pr)) if pr else None
        lid  = item.get("metadata", {}).get("legacyId", cid)
        ville = loc.get("city", "")
        parsed.append({
            "id":        lid,
            "titre":     hf.get("title", ""),
            "prix":      prix,
            "surface":   facts.get("livingSpace"),
            "nb_pieces": facts.get("numberOfRooms"),
            "cp":        loc.get("zipCode", ""),
            "ville":     ville,
            "quartier":  loc.get("district", ""),
            "url":       f"https://www.seloger.com/annonces/achat/{ville.lower().replace(' ','-')}/{lid}.htm",
        })
    return parsed, {"classifieds_ids": ids, "classifieds_data": cd, "total_count": total}

# ---------------------------------------------------------------------------
# Insertion Supabase
# Métadonnées embarquées dans data_brute (compatible schéma sans migration)
# ---------------------------------------------------------------------------

def insert(table: str, page_meta: dict, raw: dict, nb: int) -> bool:
    payload = {
        "date_scrap": datetime.now(timezone.utc).isoformat(),
        "data_brute": {
            "_meta": {
                "commune":      COMMUNE,
                "code_postal":  CODE_POSTAL,
                "page":         page_meta["page"],
                "url_source":   page_meta["url"],
                "nb_annonces":  nb,
                "scraped_at":   datetime.now(timezone.utc).isoformat(),
            },
            **raw,
        },
    }
    try:
        sb.table(table).insert(payload).execute()
        return True
    except Exception as e:
        print(f"    [SUPA ERR] {e}")
        return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    pages = build_pages()
    print(f"=== Scraping {COMMUNE} {CODE_POSTAL} ===")
    print(f"  SeLoger : {NB_PAGES_SL} pages  |  LBC : {NB_PAGES_LBC} pages")
    print(f"  Total   : {len(pages)} requêtes ScrapingBee\n")

    results = {}
    print("Lancement en parallèle (max 5 concurrent)...")
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch, p): p["id"] for p in pages}
        for fut in as_completed(futures):
            r = fut.result()
            results[r["id"]] = r
            ok = "OK" if r["status"] == 200 else (r["error"] or f"HTTP {r['status']}")
            print(f"  {r['id']:25s} | HTTP {r['status'] or 'ERR'} | {len(r['html'])//1024:4d} Ko | {ok}")

    print("\n--- Extraction + Supabase ---\n")
    total_lbc = total_sl = 0
    ok_lbc = ok_sl = 0

    for page in pages:
        r = results[page["id"]]
        if r["status"] != 200:
            print(f"  {page['id']}: ignoré")
            continue

        if page["source"] == "lbc":
            ads, raw = parse_lbc(r["html"])
            total_lbc += len(ads)
            ins = insert("stg_lbc", page, raw, len(ads))
            ok_lbc += ins
            print(f"  {page['id']}: {len(ads):2d} annonces | supa:{'OK' if ins else 'ERR'}")
            for a in ads[:2]:
                print(f"    #{a['id']} | {a['prix']}€ | {a['surface']}m² | {a['nb_pieces']}p | {a['ville']}")
        else:
            ads, raw = parse_seloger(r["html"])
            total_sl += len(ads)
            ins = insert("stg_seloger", page, raw, len(ads))
            ok_sl += ins
            print(f"  {page['id']}: {len(ads):2d} annonces | supa:{'OK' if ins else 'ERR'}")
            for a in ads[:2]:
                print(f"    #{a['id']} | {a['prix']}€ | {a['surface']}m² | {a['nb_pieces']}p | {a['ville']}")

    print(f"\n{'='*50}")
    print(f"LBC     : {total_lbc} annonces — {ok_lbc}/{NB_PAGES_LBC} pages insérées")
    print(f"SeLoger : {total_sl} annonces — {ok_sl}/{NB_PAGES_SL} pages insérées")
    print(f"Total   : {total_lbc + total_sl} annonces dans Supabase")

if __name__ == "__main__":
    main()
