"""
scraper.py — Scraper paramétrique SeLoger + LeBonCoin
Usage : python3 scraper.py <url_seloger_p1> <url_lbc_p1>

Exemples d'URL :
  SeLoger : https://www.seloger.com/classified-search?...&locations=AD08FR28776&...
  LBC     : https://www.leboncoin.fr/cl/ventes_immobilieres/cp_givors_69700/real_estate_type:2

Détecte automatiquement code_postal, commune et nombre de pages depuis la p1.
Scrape toutes les pages en parallèle, insère en stg_*, puis lance transform.
"""

import os, re, sys, json, math, lzstring, requests, subprocess
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

# ---------------------------------------------------------------------------
# Extraction code_postal / commune depuis les URLs
# ---------------------------------------------------------------------------

def _extract_from_lbc_url(url: str) -> tuple[str, str]:
    """cp_saint-priest_69800 → ('69800', 'Saint-Priest')"""
    m = re.search(r'/cp_([^/]+)_(\d{5})/', url)
    if not m:
        raise ValueError(f"Impossible d'extraire CP/commune depuis URL LBC : {url}")
    commune_slug = m.group(1).replace("-", " ").title()
    return m.group(2), commune_slug

def _extract_cp_from_seloger_url(url: str) -> str | None:
    """Extrait un code postal si présent dans les paramètres SeLoger."""
    m = re.search(r'locations=(\d{5})', url)
    return m.group(1) if m else None

# ---------------------------------------------------------------------------
# Fetch ScrapingBee
# ---------------------------------------------------------------------------

def _fetch(pid: str, url: str, params: dict, timeout: int) -> dict:
    try:
        r = requests.get(SB, params={"api_key": KEY, "url": url, **params}, timeout=timeout)
        html = r.text
        (DEBUG / f"{pid}.html").write_text(html, encoding="utf-8")
        return {"id": pid, "url": url, "status": r.status_code, "html": html, "error": None}
    except Exception as e:
        return {"id": pid, "url": url, "status": None, "html": "", "error": str(e)}

# ---------------------------------------------------------------------------
# Parsers + sonde page 1
# ---------------------------------------------------------------------------

def _lbc_attr(ad, key):
    return next((a.get("value") for a in ad.get("attributes", []) if a.get("key") == key), None)

def parse_lbc_page(html: str) -> tuple[list, dict, int]:
    """Retourne (ads_raw, raw_for_storage, total_count)."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not m:
        return [], {}, 0
    d  = json.loads(m.group(1))
    sd = d.get("props", {}).get("pageProps", {}).get("searchData", {})
    ads = sd.get("ads", [])
    total = sd.get("total", len(ads))
    return ads, {"ads": ads}, total

def parse_seloger_page(html: str) -> tuple[list, dict, int]:
    """Retourne (classifieds_ids, raw_for_storage, total_count)."""
    m = re.search(r'<script id="__UFRN_FETCHER__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return [], {}, 0
    inner = re.search(r'JSON\.parse\("(.+)"\)', m.group(1), re.DOTALL)
    if not inner:
        return [], {}, 0
    decoded = inner.group(1).encode().decode("unicode_escape")
    b64 = json.loads(decoded).get("data", {}).get("classified-serp-init-data", "")
    if not b64:
        return [], {}, 0
    raw_json = lzstring.LZString().decompressFromBase64(b64)
    if not raw_json:
        return [], {}, 0
    pp    = json.loads(raw_json).get("pageProps", {})
    ids   = pp.get("classifieds", [])
    cd    = pp.get("classifiedsData", {})
    total = pp.get("totalCount", len(ids))
    return ids, {"classifieds_ids": ids, "classifieds_data": cd}, total

def _detect_cp_commune_from_seloger(ads_ids: list, cd: dict) -> tuple[str, str]:
    """Extrait CP et commune du premier item SeLoger parsé."""
    for cid in ads_ids:
        item = cd.get(cid)
        if not isinstance(item, dict):
            continue
        loc = item.get("location", {}).get("address", {})
        cp, city = loc.get("zipCode", ""), loc.get("city", "")
        if cp and city:
            return cp, city
    return "", ""

def _detect_cp_commune_from_lbc(ads: list) -> tuple[str, str]:
    """Extrait CP et commune du premier item LBC."""
    for ad in ads:
        cp   = ad.get("location", {}).get("zipcode", "")
        city = ad.get("location", {}).get("city", "")
        if cp and city:
            return cp, city
    return "", ""

# ---------------------------------------------------------------------------
# Insertion Supabase
# ---------------------------------------------------------------------------

def _insert(table: str, commune: str, code_postal: str, page: int,
            url: str, raw: dict, nb: int) -> bool:
    payload = {
        "date_scrap": datetime.now(timezone.utc).isoformat(),
        "data_brute": {
            "_meta": {
                "commune":     commune,
                "code_postal": code_postal,
                "page":        page,
                "url_source":  url,
                "nb_annonces": nb,
                "scraped_at":  datetime.now(timezone.utc).isoformat(),
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
# Scrape phase
# ---------------------------------------------------------------------------

SL_PARAMS  = {"render_js": "false", "premium_proxy": "true", "country_code": "fr"}
LBC_PARAMS = {"render_js": "true", "stealth_proxy": "true", "wait": "4000", "block_resources": "false"}

def scrape(sl_base: str, lbc_base: str) -> tuple[str, str]:
    """Scrape toutes les pages, insère en stg_*. Retourne (code_postal, commune)."""

    print("\n=== Phase 1 — Sonde page 1 ===\n")

    # --- SeLoger p1 ---
    print("  SeLoger p1...", end=" ", flush=True)
    r_sl = _fetch("seloger_p1", sl_base, SL_PARAMS, timeout=45)
    if r_sl["status"] != 200:
        raise RuntimeError(f"SeLoger p1 échoué : HTTP {r_sl['status']} / {r_sl['error']}")
    sl_ids, sl_raw, sl_total = parse_seloger_page(r_sl["html"])
    sl_pages = max(1, math.ceil(sl_total / 30))
    cp_sl, commune_sl = _detect_cp_commune_from_seloger(sl_ids, sl_raw.get("classifieds_data", {}))
    print(f"OK — {sl_total} annonces → {sl_pages} pages  ({cp_sl} {commune_sl})")

    # --- LBC p1 ---
    print("  LBC p1...", end=" ", flush=True)
    r_lbc = _fetch("lbc_p1", lbc_base, LBC_PARAMS, timeout=120)
    if r_lbc["status"] != 200:
        raise RuntimeError(f"LBC p1 échoué : HTTP {r_lbc['status']} / {r_lbc['error']}")
    lbc_ads, lbc_raw, lbc_total = parse_lbc_page(r_lbc["html"])
    lbc_pages = max(1, math.ceil(lbc_total / 35))
    cp_lbc, commune_lbc = _detect_cp_commune_from_lbc(lbc_ads)
    print(f"OK — {lbc_total} annonces → {lbc_pages} pages  ({cp_lbc} {commune_lbc})")

    # Résolution code_postal / commune
    code_postal = cp_sl or cp_lbc or _extract_cp_from_seloger_url(sl_base) or ""
    commune     = commune_sl or commune_lbc or ""
    if not code_postal:
        # Dernier recours : extraire depuis l'URL LBC
        code_postal, commune_fallback = _extract_from_lbc_url(lbc_base)
        commune = commune or commune_fallback

    print(f"\n  → Commune : {commune} ({code_postal})")
    print(f"  → SeLoger : {sl_pages} pages  |  LBC : {lbc_pages} pages")
    print(f"  → Total   : {sl_pages + lbc_pages} requêtes (p1 déjà faites)")

    # Insérer p1
    _insert("stg_seloger", commune, code_postal, 1, sl_base, sl_raw, len(sl_ids))
    _insert("stg_lbc",     commune, code_postal, 1, lbc_base, lbc_raw, len(lbc_ads))

    # --- Pages 2..N ---
    tasks = []
    for p in range(2, sl_pages + 1):
        url = f"{sl_base}&page={p}"
        tasks.append(("seloger", p, url, SL_PARAMS, 45))
    for p in range(2, lbc_pages + 1):
        url = f"{lbc_base}/p-{p}"
        tasks.append(("lbc", p, url, LBC_PARAMS, 120))

    if tasks:
        print(f"\n=== Phase 2 — Pages suivantes ({len(tasks)} requêtes, max 5 concurrent) ===\n")
        results = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            fmap = {
                ex.submit(_fetch, f"{src}_p{p}", url, params, timeout): (src, p, url)
                for src, p, url, params, timeout in tasks
            }
            for fut in as_completed(fmap):
                src, p, url = fmap[fut]
                r = fut.result()
                results[(src, p)] = r
                status_str = f"HTTP {r['status']}" if r["status"] else r["error"]
                print(f"  {src:8s} p{p} | {status_str} | {len(r['html'])//1024:4d} Ko")

        for src, p, url, _, _ in tasks:
            r = results.get((src, p), {})
            if r.get("status") != 200:
                print(f"  ⚠  {src} p{p} ignorée ({r.get('error') or r.get('status')})")
                continue
            if src == "seloger":
                ids, raw, _ = parse_seloger_page(r["html"])
                _insert("stg_seloger", commune, code_postal, p, url, raw, len(ids))
                print(f"  {src} p{p} → {len(ids)} annonces insérées")
            else:
                ads, raw, _ = parse_lbc_page(r["html"])
                _insert("stg_lbc", commune, code_postal, p, url, raw, len(ads))
                print(f"  {src} p{p} → {len(ads)} annonces insérées")

    return code_postal, commune

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("Usage : python3 scraper.py <url_seloger_p1> <url_lbc_p1>")
        sys.exit(1)

    sl_url  = sys.argv[1].rstrip("/")
    lbc_url = sys.argv[2].rstrip("/")

    # Retirer &page=N si l'utilisateur a copié une page N
    sl_url = re.sub(r"&page=\d+", "", sl_url)
    lbc_url = re.sub(r"/p-\d+$", "", lbc_url)

    print(f"SeLoger : {sl_url[:80]}...")
    print(f"LBC     : {lbc_url}")

    code_postal, commune = scrape(sl_url, lbc_url)

    if not code_postal:
        print("\n⚠  Code postal non détecté — transform impossible")
        sys.exit(1)

    print(f"\n=== Phase 3 — Transform {code_postal} {commune} ===\n")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "transform.py"), code_postal, commune],
        cwd=Path(__file__).parent,
    )
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
