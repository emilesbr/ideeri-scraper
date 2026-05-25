"""
Scraping Écully — 2 pages LBC + 2 pages SeLoger.
Paramètres validés : stealth_proxy pour LBC, premium_proxy pour SeLoger.
Résultats sauvegardés dans debug/ ET dans Supabase (stg_lbc / stg_seloger).
"""

import os
import re
import json
import lzstring
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from pathlib import Path
from supabase import create_client

load_dotenv()
SCRAPINGBEE_KEY = os.environ["SCRAPINGBEE_KEY"]
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"
DEBUG_DIR = Path(__file__).parent / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

sb = create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])


# ---------------------------------------------------------------------------
# Pages à scraper
# ---------------------------------------------------------------------------

PAGES = [
    {
        "id": "lbc_ecully_p1",
        "source": "lbc",
        "code_postal": "69130",
        "page": 1,
        # /cl/ format — le double underscore dans /recherche?locations=... cause une 520
        "url": "https://www.leboncoin.fr/cl/ventes_immobilieres/cp_ecully_69130/real_estate_type:2/p-1",
        "params": {
            "render_js": "true",
            "stealth_proxy": "true",
            "wait": "4000",
            "block_resources": "false",
        },
        "timeout": 120,
    },
    {
        "id": "lbc_ecully_p2",
        "source": "lbc",
        "code_postal": "69130",
        "page": 2,
        "url": "https://www.leboncoin.fr/cl/ventes_immobilieres/cp_ecully_69130/real_estate_type:2/p-2",
        "params": {
            "render_js": "true",
            "stealth_proxy": "true",
            "wait": "4000",
            "block_resources": "false",
        },
        "timeout": 120,
    },
    {
        "id": "seloger_ecully_p1",
        "source": "seloger",
        "code_postal": "69130",
        "page": 1,
        "url": (
            "https://www.seloger.com/classified-search"
            "?classifiedBusiness=Professional"
            "&distributionTypes=Buy"
            "&estateTypes=House,Apartment"
            "&locations=AD08FR28766"
            "&projectTypes=Resale"
        ),
        "params": {
            "render_js": "false",
            "premium_proxy": "true",
            "country_code": "fr",
        },
        "timeout": 45,
    },
    {
        "id": "seloger_ecully_p2",
        "source": "seloger",
        "code_postal": "69130",
        "page": 2,
        "url": (
            "https://www.seloger.com/classified-search"
            "?classifiedBusiness=Professional"
            "&distributionTypes=Buy"
            "&estateTypes=House,Apartment"
            "&locations=AD08FR28766"
            "&projectTypes=Resale"
            "&page=2"
        ),
        "params": {
            "render_js": "false",
            "premium_proxy": "true",
            "country_code": "fr",
        },
        "timeout": 45,
    },
]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_page(page: dict) -> dict:
    payload = {"api_key": SCRAPINGBEE_KEY, "url": page["url"], **page["params"]}
    try:
        resp = requests.get(SCRAPINGBEE_URL, params=payload, timeout=page["timeout"])
        html = resp.text
        (DEBUG_DIR / f"{page['id']}.html").write_text(html, encoding="utf-8")
        return {
            **page,
            "status": resp.status_code,
            "html": html,
            "error": None,
        }
    except Exception as exc:
        return {**page, "status": None, "html": "", "error": str(exc)}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_lbc(html: str) -> tuple[list[dict], dict]:
    """Retourne (annonces_parsées, raw_json_pour_supabase)."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    if not m:
        return [], {}
    try:
        next_data = json.loads(m.group(1))
        search_data = (
            next_data.get("props", {})
                     .get("pageProps", {})
                     .get("searchData", {})
        )
        ads_raw = search_data.get("ads", [])
        total = search_data.get("total", len(ads_raw))

        parsed = []
        for ad in ads_raw:
            parsed.append({
                "id": str(ad.get("list_id", "")),
                "titre": ad.get("subject", ""),
                "prix": ad.get("price", [None])[0] if ad.get("price") else None,
                "surface": _lbc_attr(ad, "square"),
                "nb_pieces": _lbc_attr(ad, "rooms"),
                "cp": ad.get("location", {}).get("zipcode", ""),
                "ville": ad.get("location", {}).get("city", ""),
                "url": ad.get("url", ""),
            })

        raw = {"ads": ads_raw, "total_count": total}
        return parsed, raw
    except Exception:
        return [], {}


def _lbc_attr(ad: dict, key: str):
    for a in ad.get("attributes", []):
        if a.get("key") == key:
            return a.get("value")
    return None


def parse_seloger(html: str) -> tuple[list[dict], dict]:
    """Retourne (annonces_parsées, raw_json_pour_supabase)."""
    m = re.search(r'<script id="__UFRN_FETCHER__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return [], {}
    try:
        inner = re.search(r'JSON\.parse\("(.+)"\)', m.group(1), re.DOTALL)
        if not inner:
            return [], {}
        decoded = inner.group(1).encode().decode("unicode_escape")
        ufrn = json.loads(decoded)
        b64 = ufrn.get("data", {}).get("classified-serp-init-data", "")
        if not b64:
            return [], {}
        raw_json = lzstring.LZString().decompressFromBase64(b64)
        if not raw_json:
            return [], {}
        pp = json.loads(raw_json).get("pageProps", {})
        ids: list[str] = pp.get("classifieds", [])
        cd: dict = pp.get("classifiedsData", {})
        total = pp.get("totalCount", len(ids))

        parsed = []
        for cid in ids:
            item = cd.get(cid)
            if not isinstance(item, dict):
                continue
            hf = item.get("hardFacts", {})
            loc = item.get("location", {}).get("address", {})
            facts = {f["type"]: f.get("splitValue") for f in hf.get("facts", [])}
            price_raw = hf.get("price", {}).get("ariaLabel", "")
            prix = int(re.sub(r"[^\d]", "", price_raw)) if price_raw else None
            legacy_id = item.get("metadata", {}).get("legacyId", cid)
            ville = loc.get("city", "")
            parsed.append({
                "id": legacy_id,
                "titre": hf.get("title", ""),
                "prix": prix,
                "surface": facts.get("livingSpace"),
                "nb_pieces": facts.get("numberOfRooms"),
                "cp": loc.get("zipCode", ""),
                "ville": ville,
                "quartier": loc.get("district", ""),
                "url": f"https://www.seloger.com/annonces/achat/{ville.lower().replace(' ', '-')}/{legacy_id}.htm",
            })

        raw = {"classifieds_ids": ids, "classifieds_data": cd, "total_count": total}
        return parsed, raw
    except Exception:
        return [], {}


# ---------------------------------------------------------------------------
# Insertion Supabase
# ---------------------------------------------------------------------------

def insert_stg(table: str, page_meta: dict, raw: dict, nb_annonces: int) -> bool:
    row = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "code_postal": page_meta["code_postal"],
        "page": page_meta["page"],
        "url_source": page_meta["url"],
        "nb_annonces": nb_annonces,
        "data_brute": raw,
    }
    try:
        sb.table(table).insert(row).execute()
        return True
    except Exception as exc:
        print(f"    [SUPA ERR] {table}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== Scraping Écully — 2×LBC + 2×SeLoger ===\n")

    print("Lancement des 4 requêtes en parallèle...")
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_page, p): p["id"] for p in PAGES}
        for future in as_completed(futures):
            r = future.result()
            results[r["id"]] = r
            status = r["status"] or "ERR"
            size = len(r["html"]) // 1024
            ok = "OK" if r["status"] == 200 else (r["error"] or "ERR")
            print(f"  {r['id']:25s} | HTTP {status} | {size} Ko | {ok}")

    print("\n--- Extraction + insertion Supabase ---\n")

    total_lbc = 0
    total_sl = 0

    for page in PAGES:
        pid = page["id"]
        r = results[pid]
        if r["status"] != 200:
            print(f"  {pid}: ignoré (status {r['status']})")
            continue

        if page["source"] == "lbc":
            ads, raw = parse_lbc(r["html"])
            total_lbc += len(ads)
            inserted = insert_stg("stg_lbc", page, raw, len(ads))
            print(f"  {pid}: {len(ads)} annonces | Supabase: {'OK' if inserted else 'ERR'}")
            for ad in ads[:3]:
                print(f"    #{ad['id']} | {ad['prix']}€ | {ad['surface']}m² | {ad['nb_pieces']} pièces | {ad['ville']} {ad['cp']}")
        else:
            ads, raw = parse_seloger(r["html"])
            total_sl += len(ads)
            inserted = insert_stg("stg_seloger", page, raw, len(ads))
            print(f"  {pid}: {len(ads)} annonces | Supabase: {'OK' if inserted else 'ERR'}")
            for ad in ads[:3]:
                print(f"    #{ad['id']} | {ad['prix']}€ | {ad['surface']}m² | {ad['nb_pieces']} pièces | {ad['ville']} {ad['cp']}")

    print(f"\nTotal : {total_lbc} annonces LBC + {total_sl} annonces SeLoger")
    print(f"HTML bruts dans : {DEBUG_DIR}/")


if __name__ == "__main__":
    main()
