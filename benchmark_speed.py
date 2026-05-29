"""
benchmark_speed.py — Optimisation automatique des paramètres LBC

Teste différentes combinaisons de wait + workers sur une petite commune,
mesure temps et taux de succès, et recommande les meilleurs paramètres.

Usage :
  python3 benchmark_speed.py                  # Lyon 1er par défaut
  python3 benchmark_speed.py 69005 "Lyon 5eme"
"""

import os, sys, time, json, re, statistics
from pathlib import Path
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

load_dotenv(override=True)

SB_URL = "https://app.scrapingbee.com/api/v1/"
DEBUG  = Path(__file__).parent / "debug"
DEBUG.mkdir(exist_ok=True)

CP      = sys.argv[1] if len(sys.argv) > 1 else "69001"
COMMUNE = sys.argv[2] if len(sys.argv) > 2 else "Lyon 1er"

# Paramètres fixes
BASE_LBC_PARAMS = {
    "render_js":       "true",
    "premium_proxy":   "true",
    "block_resources": "true",
    "country_code":    "fr",
}

# Combinaisons à tester : (wait_ms, max_workers, label)
COMBOS = [
    (3000, 2, "wait=3s  / 2 workers"),
    (4000, 2, "wait=4s  / 2 workers"),
    (5000, 2, "wait=5s  / 2 workers"),
    (6000, 2, "wait=6s  / 2 workers  ← actuel"),
    (4000, 3, "wait=4s  / 3 workers"),
    (5000, 3, "wait=5s  / 3 workers"),
]

G   = "\033[92m"
R   = "\033[91m"
Y   = "\033[93m"
C   = "\033[96m"
B   = "\033[1m"
RST = "\033[0m"


def _geocode(commune: str, cp: str) -> dict | None:
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": commune, "postcode": cp, "type": "municipality", "limit": 1},
            timeout=5,
        )
        features = r.json().get("features", [])
        if not features:
            return None
        lon, lat = features[0]["geometry"]["coordinates"]
        return {"lat": lat, "lon": lon}
    except Exception:
        return None


def _lbc_url(commune: str, cp: str, page: int = 1) -> str:
    from urllib.parse import quote as q
    geo = _geocode(commune, cp)
    slug = q(commune, safe="-")
    base = (
        "https://www.leboncoin.fr/recherche"
        "?category=9"
        f"&locations={slug}_{cp}__{geo['lat']}_{geo['lon']}_5000"
        "&immo_sell_type=old"
        "&real_estate_type=2,1"
        "&owner_type=all"
    ) if geo else (
        "https://www.leboncoin.fr/recherche"
        f"?category=9&locations={slug}_{cp}&immo_sell_type=old&real_estate_type=2,1&owner_type=all"
    )
    return f"{base}&page={page}" if page > 1 else base


def _fetch_lbc(url: str, wait_ms: int, pid: str) -> dict:
    params = {**BASE_LBC_PARAMS, "wait": str(wait_ms)}
    t0 = time.time()
    try:
        r = requests.get(
            SB_URL,
            params={"api_key": os.environ["SCRAPINGBEE_KEY"], "url": url, **params},
            timeout=120,
        )
        html = r.content.decode("utf-8", errors="replace")
        elapsed = round(time.time() - t0, 1)
        ok = r.status_code == 200 and "__NEXT_DATA__" in html
        ads = 0
        if ok:
            m = re.search(r'"total"\s*:\s*(\d+)', html)
            if m:
                ads = int(m.group(1))
        return {"pid": pid, "status": r.status_code, "ok": ok, "elapsed": elapsed, "ads": ads}
    except Exception as e:
        return {"pid": pid, "status": None, "ok": False, "elapsed": round(time.time() - t0, 1), "ads": 0, "error": str(e)}


def _probe_pages() -> int:
    """Détecte le nombre de pages LBC sur la commune cible."""
    print(f"  Sonde LBC p1 ({COMMUNE} {CP})...", end=" ", flush=True)
    r = _fetch_lbc(_lbc_url(COMMUNE, CP, 1), wait_ms=6000, pid="probe_p1")
    if not r["ok"]:
        detail = f"HTTP {r['status']}" if r.get('status') else r.get('error', 'erreur inconnue')
        print(f"{R}ECHEC{RST} — {detail}")
        return 0
    import math
    pages = max(1, math.ceil(r["ads"] / 35)) if r["ads"] else 1
    print(f"{G}OK{RST} — {r['ads']} annonces → {pages} pages ({r['elapsed']}s)")
    return pages


def _test_combo(wait_ms: int, workers: int, pages: int, label: str) -> dict:
    urls = [_lbc_url(COMMUNE, CP, p) for p in range(1, pages + 1)]
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(_fetch_lbc, url, wait_ms, f"p{i+1}"): i for i, url in enumerate(urls)}
        for fut in as_completed(fmap):
            results.append(fut.result())
    total = round(time.time() - t0, 1)
    n_ok  = sum(1 for r in results if r["ok"])
    times = [r["elapsed"] for r in results]
    return {
        "label":       label,
        "wait_ms":     wait_ms,
        "workers":     workers,
        "pages":       pages,
        "n_ok":        n_ok,
        "success_rate": round(100 * n_ok / pages) if pages else 0,
        "total_s":     total,
        "avg_s":       round(statistics.mean(times), 1) if times else 0,
        "min_s":       round(min(times), 1) if times else 0,
        "max_s":       round(max(times), 1) if times else 0,
    }


def _score(r: dict) -> float:
    """Score composite : taux de succès pondéré × inverse du temps total."""
    if r["success_rate"] < 80:
        return 0.0
    return r["success_rate"] / r["total_s"]


def main():
    print(f"\n{B}=== Benchmark LBC — {COMMUNE} ({CP}) ==={RST}\n")
    key = os.environ.get("SCRAPINGBEE_KEY", "")
    print(f"  Clé ScrapingBee : {'*'*8 + key[-6:] if key else R+'NON CHARGÉE'+RST}")

    credits = None
    try:
        resp = requests.get(
            "https://app.scrapingbee.com/api/v1/usage",
            params={"api_key": os.environ["SCRAPINGBEE_KEY"]},
            timeout=10,
        )
        if resp.status_code == 200:
            d = resp.json()
            used = d.get("used_api_credits", 0)
            maxi = d.get("max_api_credits")
            credits = (maxi - used) if isinstance(maxi, int) else None
    except Exception:
        pass

    cost_per_combo = 75  # crédits LBC premium_proxy par page
    total_cost     = len(COMBOS) * 3 * cost_per_combo  # estimation max
    print(f"  Crédits disponibles : {credits if credits is not None else '?'}")
    print(f"  Coût estimé benchmark : ~{total_cost} crédits ({len(COMBOS)} combos × ~3p × 75)")

    if credits is not None and credits < total_cost:
        print(f"\n  {Y}⚠ Quota potentiellement insuffisant. Continuer quand même ? [o/n]{RST} ", end="")
        if input().strip().lower() != "o":
            print("  Annulé.")
            return

    pages = _probe_pages()
    if pages == 0:
        print(f"\n  {R}Impossible de détecter les pages — vérifie la clé ScrapingBee.{RST}")
        return

    print(f"\n  {B}Test de {len(COMBOS)} combinaisons sur {pages} page(s)...{RST}\n")
    print(f"  {'Combinaison':<30} {'Succès':>7} {'Total':>7} {'Moy/p':>7} {'Score':>7}")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")

    all_results = []
    for wait_ms, workers, label in COMBOS:
        print(f"  {label:<30}", end=" ", flush=True)
        r = _test_combo(wait_ms, workers, pages, label)
        all_results.append(r)
        score = _score(r)
        ok_icon = G if r["success_rate"] == 100 else (Y if r["success_rate"] >= 80 else R)
        print(f"{ok_icon}{r['success_rate']:>6}%{RST} {r['total_s']:>6}s {r['avg_s']:>6}s {score:>7.2f}")
        # Petite pause entre combos pour ne pas stresser Datadome
        time.sleep(5)

    # Recommandation
    best = max(all_results, key=_score)
    print(f"\n  {B}{'='*58}{RST}")
    print(f"  {G}{B}Meilleur combo : {best['label']}{RST}")
    print(f"  Taux succès : {best['success_rate']}% | Temps total : {best['total_s']}s | Score : {round(_score(best), 2)}")

    current = next((r for r in all_results if r["wait_ms"] == 6000 and r["workers"] == 2), None)
    if current and best["total_s"] < current["total_s"]:
        gain = round(100 * (1 - best["total_s"] / current["total_s"]))
        print(f"  Gain vs actuel (6s/2w) : {G}−{gain}%{RST} plus rapide")

    print(f"\n  {B}Pour appliquer ces paramètres dans pipeline.py :{RST}")
    print(f'  LBC_PARAMS["wait"]    = "{best["wait_ms"]}"')
    print(f"  max_workers           = {best['workers']}")

    # Sauvegarder les résultats
    out = DEBUG / f"benchmark_lbc_{CP}.json"
    out.write_text(json.dumps({"commune": COMMUNE, "cp": CP, "pages": pages,
                               "results": all_results, "best": best}, indent=2, ensure_ascii=False))
    print(f"\n  Résultats sauvegardés : {out}")


if __name__ == "__main__":
    main()
