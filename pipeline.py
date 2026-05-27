"""
pipeline.py — Orchestrateur du pipeline Ideeri

Usage :
  python3 pipeline.py check
  python3 pipeline.py status <cp>
  python3 pipeline.py scrape <cp> <commune> [--sl-code AD08FRXXXXX]
  python3 pipeline.py transform <cp> <commune>
  python3 pipeline.py run <cp> <commune> [--sl-code AD08FRXXXXX]
  python3 pipeline.py reset <cp>
  python3 pipeline.py enrich [--all] [--dry-run]
"""

import argparse, json, math, os, re, sys, time, unicodedata
import lzstring, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# ANSI
# ---------------------------------------------------------------------------

G   = "\033[92m"
R   = "\033[91m"
Y   = "\033[93m"
C   = "\033[96m"
B   = "\033[1m"
RST = "\033[0m"

def _ok(msg):   print(f"  {G}✅{RST} {msg}")
def _err(msg):  print(f"  {R}❌{RST} {msg}")
def _warn(msg): print(f"  {Y}⚠️ {RST} {msg}")

# ---------------------------------------------------------------------------
# ScrapingBee
# ---------------------------------------------------------------------------

SB_URL     = "https://app.scrapingbee.com/api/v1/"
SL_PARAMS  = {"render_js": "false", "premium_proxy": "true", "country_code": "fr"}
LBC_PARAMS = {"render_js": "true",  "premium_proxy": "true", "wait": "4000", "block_resources": "true"}
DEBUG      = Path(__file__).parent / "debug"
DEBUG.mkdir(exist_ok=True)


def _sb():
    from supabase import create_client
    return create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])


def _fetch(pid: str, url: str, params: dict, timeout: int) -> dict:
    t0 = time.time()
    try:
        r = requests.get(SB_URL,
                         params={"api_key": os.environ["SCRAPINGBEE_KEY"], "url": url, **params},
                         timeout=timeout)
        html = r.text
        (DEBUG / f"{pid}.html").write_text(html, encoding="utf-8")
        return {"id": pid, "url": url, "status": r.status_code, "html": html,
                "error": None, "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"id": pid, "url": url, "status": None, "html": "",
                "error": str(e), "elapsed": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------
# Parsers HTML → (items, raw_for_storage, total_count)
# ---------------------------------------------------------------------------

def _parse_lbc(html: str) -> tuple[list, dict, int]:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.DOTALL)
    if not m:
        return [], {}, 0
    d  = json.loads(m.group(1))
    sd = d.get("props", {}).get("pageProps", {}).get("searchData", {})
    ads = sd.get("ads", [])
    return ads, {"ads": ads}, sd.get("total", len(ads))


def _parse_seloger(html: str) -> tuple[list, dict, int]:
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
    pp  = json.loads(raw_json).get("pageProps", {})
    ids = pp.get("classifieds", [])
    cd  = pp.get("classifiedsData", {})
    return ids, {"classifieds_ids": ids, "classifieds_data": cd}, pp.get("totalCount", len(ids))


# ---------------------------------------------------------------------------
# Helpers URL + insertion stg_*
# ---------------------------------------------------------------------------

def _lbc_base(commune: str, cp: str) -> str:
    # On nettoie le nom de la commune pour le paramètre d'URL (ex: "Rive-de-Gier" -> "Rive-de-Gier")
    slug = unicodedata.normalize("NFKD", commune) # On garde les majuscules et tirets d'origine pour leur moteur
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    
    # On utilise le nouveau format d'URL globale de recherche par Code Postal
    return (
        "https://www.leboncoin.fr/recherche"
        "?category=9"
        f"&locations={slug}_{cp}"
        "&real_estate_type=1,2"  # 1=Maison, 2=Appartement (type réel géré par le parser)
    )


def _sl_base(code: str) -> str:
    return (
        "https://www.seloger.com/classified-search"
        "?classifiedBusiness=Professional&distributionTypes=Buy"
        f"&estateTypes=House,Apartment&locations={code}&projectTypes=Resale"
    )


def _insert_stg(table: str, commune: str, cp: str, page: int,
                url: str, raw: dict, nb: int, sb) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "scraped_at":  now,
        "code_postal": cp,
        "page":        page,
        "url_source":  url,
        "nb_annonces": nb,
        "data_brute": {
            "_meta": {
                "commune": commune, "code_postal": cp, "page": page,
                "url_source": url, "nb_annonces": nb, "scraped_at": now,
            },
            **raw,
        },
    }
    try:
        sb.table(table).insert(payload).execute()
        return True
    except Exception as e:
        print(f"    {R}[SUPA ERR]{RST} {e}")
        return False


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check():
    print(f"\n{B}=== Vérification de l'environnement ==={RST}\n")
    all_ok = True

    # 1. Variables .env
    missing = [v for v in ("SCRAPINGBEE_KEY", "SUPA_URL", "SUPA_KEY") if not os.environ.get(v)]
    if missing:
        _err(f".env incomplet — manquant : {', '.join(missing)}")
        print(f"\n  {R}Corrige .env avant de continuer.{RST}")
        return
    _ok(".env complet")

    # 2. Connexion Supabase
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])
        sb.table("runs").select("id").limit(0).execute()
        _ok("Supabase connecté")
    except Exception as e:
        _err(f"Supabase — échec connexion : {e}")
        print(f"\n  {R}Vérifie SUPA_URL et SUPA_KEY dans .env{RST}")
        return

    # 3. Tables présentes
    tables_missing = []
    for t in ("stg_lbc", "stg_seloger", "annonces", "entites", "runs"):
        try:
            sb.table(t).select("*").limit(0).execute()
        except Exception:
            tables_missing.append(t)
    if tables_missing:
        _err(f"Tables manquantes : {', '.join(tables_missing)}")
        print(f"  → Lance migration_v2.sql dans le SQL Editor Supabase")
        all_ok = False
    else:
        _ok("Tables présentes")

    # 4. Migration v2 (colonne bien_id)
    try:
        sb.table("annonces").select("bien_id").limit(0).execute()
        _ok("Migration v2 appliquée")
    except Exception:
        _err("Colonne bien_id absente")
        print(f"  → Lance migration_v2.sql dans le SQL Editor Supabase")
        all_ok = False

    # 5. Crédits ScrapingBee
    try:
        r = requests.get(
            "https://app.scrapingbee.com/api/v1/usage",
            params={"api_key": os.environ["SCRAPINGBEE_KEY"]},
            timeout=10,
        )
        if r.status_code == 200:
            d         = r.json()
            used      = d.get("used_api_credits", 0)
            maxi      = d.get("max_api_credits")
            remaining = (maxi - used) if isinstance(maxi, int) else "?"
            reset_ts  = d.get("billing_cycle_end") or d.get("next_reset") or "?"
            suffix    = f"(reset {reset_ts})" if reset_ts != "?" else ""
            _ok(f"ScrapingBee : {remaining} crédits disponibles {suffix}".strip())
        else:
            _err(f"ScrapingBee — clé invalide (HTTP {r.status_code})")
            all_ok = False
    except Exception as e:
        _err(f"ScrapingBee — {e}")
        all_ok = False

    print()
    if all_ok:
        print(f"  {G}{B}→ Prêt à scraper{RST}")
    else:
        print(f"  {R}→ Corrige les erreurs ci-dessus avant de continuer{RST}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(cp: str):
    sb = _sb()
    print(f"\n{B}=== Statut {cp} ==={RST}\n")

    # Staging
    for label, table in (("stg_lbc", "stg_lbc"), ("stg_seloger", "stg_seloger")):
        rows = sb.table(table).select("data_brute").eq(
            "data_brute->_meta->>code_postal", cp).execute().data
        nb_ann = sum(r.get("data_brute", {}).get("_meta", {}).get("nb_annonces", 0) for r in rows)
        print(f"  📦 {label:<12}: {len(rows)} pages | {nb_ann} annonces brutes")

    # Annonces
    na = sb.table("annonces").select("id_annonce", count="exact").eq(
        "code_postal", cp).eq("est_active", True).execute().count or 0
    ni = sb.table("annonces").select("id_annonce", count="exact").eq(
        "code_postal", cp).eq("est_active", False).execute().count or 0
    print(f"  ✅ annonces      : {na} actives | {ni} inactives")

    # Entités
    ent_rows = sb.table("annonces").select("entite_id").eq("code_postal", cp).eq(
        "est_active", True).not_.is_("entite_id", "null").execute().data
    nb_ent = len({r["entite_id"] for r in ent_rows if r.get("entite_id")})
    print(f"  ✅ entites       : {nb_ent}")

    # Biens uniques + mandats
    b_rows = sb.table("annonces").select("bien_id, sur_lbc, sur_seloger").eq(
        "code_postal", cp).eq("est_active", True).not_.is_("bien_id", "null").execute().data
    biens: dict = {}
    for a in b_rows:
        bid = a["bien_id"]
        if bid not in biens:
            biens[bid] = {"lbc": False, "sl": False}
        if a.get("sur_lbc"):     biens[bid]["lbc"] = True
        if a.get("sur_seloger"): biens[bid]["sl"]  = True
    nb_b = len(biens)
    pct  = lambda n: f"{round(100 * n / nb_b)}%" if nb_b else "0%"
    nb_lbc = sum(1 for b in biens.values() if b["lbc"] and not b["sl"])
    nb_sl  = sum(1 for b in biens.values() if b["sl"]  and not b["lbc"])
    nb_2   = sum(1 for b in biens.values() if b["lbc"] and b["sl"])
    ratio  = round(len(b_rows) / nb_b, 2) if nb_b else 0
    print(f"  ✅ biens uniques : {nb_b}")
    print(f"     ├── LBC seul        : {nb_lbc} ({pct(nb_lbc)})")
    print(f"     ├── SeLoger seul    : {nb_sl} ({pct(nb_sl)})")
    print(f"     └── Les deux        : {nb_2} ({pct(nb_2)})")
    print(f"  ✅ mandats réels : {len(b_rows)} (ratio {ratio}/bien)")

    # Dernier run
    runs = sb.table("runs").select("id, scraped_at").eq("code_postal", cp).order(
        "scraped_at", desc=True).limit(1).execute().data
    if runs:
        dt = runs[0]["scraped_at"][:16].replace("T", " ")
        print(f"  📅 dernier run   : {dt} (run #{runs[0]['id']})")
    else:
        print(f"  📅 dernier run   : aucun")

    # Matching
    n_intra = sb.table("annonces").select("id_annonce", count="exact").eq(
        "code_postal", cp).eq("source", "lbc").eq("sur_seloger", True).not_.is_(
        "match_confidence", "null").execute().count or 0
    cl_rows = sb.table("annonces").select("cluster_bien_id").eq(
        "code_postal", cp).not_.is_("cluster_bien_id", "null").execute().data
    n_clusters = len({r["cluster_bien_id"] for r in cl_rows})
    print(f"  🔄 matching      : {n_intra} paires intra + {n_clusters} clusters cross")

    # DPE
    dpe_tot = sb.table("annonces").select("id_annonce", count="exact").eq(
        "code_postal", cp).eq("est_active", True).execute().count or 0
    dpe_ok  = sb.table("annonces").select("id_annonce", count="exact").eq(
        "code_postal", cp).eq("est_active", True).not_.is_("dpe", "null").execute().count or 0
    pct_dpe = f"{round(100 * dpe_ok / dpe_tot)}%" if dpe_tot else "0%"
    print(f"  🌡️  DPE couverture : {pct_dpe} ({dpe_ok}/{dpe_tot})")

    if na == 0:
        print(f"\n  {Y}Aucune donnée pour {cp} — lance :{RST}")
        print(f"    python3 pipeline.py run {cp} <commune>")


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------

def cmd_scrape(cp: str, commune: str, sl_code: str | None = None,
               source: str | None = None) -> dict | None:
    do_sl  = source in (None, "seloger")
    do_lbc = source in (None, "lbc")
    sb       = _sb()
    lbc_base = _lbc_base(commune, cp)
    sl_base  = _sl_base(sl_code if sl_code else cp)

    label = f" [{source.upper()}]" if source else ""
    print(f"\n{B}=== Scrape {commune} ({cp}){label} ==={RST}\n")
    print(f"  Sondage page 1 (détection du volume)...\n")

    # Sonde SeLoger p1
    sl_ids, sl_raw, sl_total, sl_pages = [], {}, 0, 0
    r_sl = {"status": None, "html": ""}
    if do_sl:
        print(f"  SeLoger p1...", end=" ", flush=True)
        r_sl = _fetch(f"seloger_{cp}_p1", sl_base, SL_PARAMS, timeout=45)
        sl_ids, sl_raw, sl_total = _parse_seloger(r_sl["html"]) if r_sl["status"] == 200 else ([], {}, 0)
        sl_pages = max(1, math.ceil(sl_total / 30)) if sl_total else 0
        print(f"{'OK — ' + str(sl_total) + ' ann. → ' + str(sl_pages) + ' pages' if r_sl['status'] == 200 else 'ECHEC HTTP ' + str(r_sl['status'])}")

    # Sonde LBC p1
    lbc_ads, lbc_raw, lbc_total, lbc_pages = [], {}, 0, 0
    r_lbc = {"status": None, "html": ""}
    if do_lbc:
        print(f"  LBC p1...", end=" ", flush=True)
        r_lbc = _fetch(f"lbc_{cp}_p1", f"{lbc_base}/p-1", LBC_PARAMS, timeout=120)
        lbc_ads, lbc_raw, lbc_total = _parse_lbc(r_lbc["html"]) if r_lbc["status"] == 200 else ([], {}, 0)
        lbc_pages = max(1, math.ceil(lbc_total / 35)) if lbc_total else 0
        print(f"{'OK — ' + str(lbc_total) + ' ann. → ' + str(lbc_pages) + ' pages' if r_lbc['status'] == 200 else 'ECHEC HTTP ' + str(r_lbc['status'])}")

    if (do_sl and sl_total == 0 and not do_lbc) or \
       (do_lbc and lbc_total == 0 and not do_sl) or \
       (do_sl and do_lbc and sl_total == 0 and lbc_total == 0):
        print(f"\n  {R}Aucune annonce détectée. Vérifie les URLs ou les crédits.{RST}")
        return None

    credits_sl  = sl_pages  * 10
    credits_lbc = lbc_pages * 75

    if do_sl:
        print(f"\n  SeLoger : {sl_total} annonces → {sl_pages} pages (~{credits_sl} crédits)")
    if do_lbc:
        print(f"  LBC     : {lbc_total} annonces → {lbc_pages} pages (~{credits_lbc} crédits)")
    print(f"  Total estimé : ~{credits_sl + credits_lbc} crédits\n")

    # Guard : données déjà présentes aujourd'hui pour la source concernée
    today = datetime.now(timezone.utc).date().isoformat()
    guard_tables = []
    if do_lbc:  guard_tables.append("stg_lbc")
    if do_sl:   guard_tables.append("stg_seloger")
    for gtable in guard_tables:
        existing = sb.table(gtable).select("scraped_at").eq("code_postal", cp).execute().data
        if any((r.get("scraped_at") or "")[:10] == today for r in existing):
            _warn(f"Des données {gtable} existent déjà pour {cp} aujourd'hui.")
            rep = input("  Écraser et re-scraper ? [o/n] : ").strip().lower()
            if rep != "o":
                print("  Annulé.")
                return None
            break

    rep = input("  Continuer ? [o/n] : ").strip().lower()
    if rep != "o":
        print("  Annulé.")
        return None

    # Insérer page 1 (déjà fetchée)
    if do_sl  and r_sl["status"]  == 200:
        _insert_stg("stg_seloger", commune, cp, 1, sl_base,          sl_raw,  len(sl_ids),  sb)
    if do_lbc and r_lbc["status"] == 200:
        _insert_stg("stg_lbc",     commune, cp, 1, f"{lbc_base}/p-1", lbc_raw, len(lbc_ads), sb)

    # Construire les tâches pages 2..N
    tasks = []
    if do_sl:
        for p in range(2, sl_pages  + 1):
            tasks.append(("seloger", p, f"{sl_base}&page={p}",   SL_PARAMS,  45))
    if do_lbc:
        for p in range(2, lbc_pages + 1):
            tasks.append(("lbc",     p, f"{lbc_base}/p-{p}",     LBC_PARAMS, 120))

    total_sl  = len(sl_ids)
    total_lbc = len(lbc_ads)
    ok_sl     = 1 if r_sl["status"]  == 200 else 0
    ok_lbc    = 1 if r_lbc["status"] == 200 else 0

    if tasks:
        print(f"\n  Scraping {len(tasks)} pages restantes (max 5 concurrent)...\n")
        with ThreadPoolExecutor(max_workers=5) as ex:
            fmap = {
                ex.submit(_fetch, f"{src}_{cp}_p{p}", url, params, timeout): (src, p, url)
                for src, p, url, params, timeout in tasks
            }
            results = {}
            for fut in as_completed(fmap):
                src, p, url = fmap[fut]
                r = fut.result()
                results[(src, p)] = r
                icon = f"{G}✓{RST}" if r["status"] == 200 else f"{R}✗{RST}"
                print(f"  {icon} {src:8s} p{p} | HTTP {r['status'] or 'ERR'} | {r['elapsed']}s")

        for src, p, url, _, _ in tasks:
            r = results.get((src, p), {})
            if r.get("status") != 200:
                continue
            if src == "seloger":
                ids, raw, _ = _parse_seloger(r["html"])
                _insert_stg("stg_seloger", commune, cp, p, url, raw, len(ids), sb)
                total_sl += len(ids)
                ok_sl    += 1
            else:
                ads, raw, _ = _parse_lbc(r["html"])
                _insert_stg("stg_lbc", commune, cp, p, url, raw, len(ads), sb)
                total_lbc += len(ads)
                ok_lbc    += 1

    print(f"\n  {B}Résultat scraping :{RST}")
    if do_lbc:
        print(f"  LBC     : {total_lbc} ann. sur {lbc_pages} pages ({ok_lbc} OK / {lbc_pages - ok_lbc} ERR)")
    if do_sl:
        print(f"  SeLoger : {total_sl} ann. sur {sl_pages} pages ({ok_sl} OK / {sl_pages - ok_sl} ERR)")
    print(f"  Crédits consommés : ~{credits_sl + credits_lbc}")
    print(f"\n  → Lance transform : {C}python3 pipeline.py transform {cp} {commune}{RST}")

    return {"total_sl": total_sl, "total_lbc": total_lbc,
            "sl_pages": sl_pages, "lbc_pages": lbc_pages,
            "credits": credits_sl + credits_lbc}


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------

def cmd_transform(cp: str, commune: str):
    sb = _sb()
    has_lbc = bool(sb.table("stg_lbc").select("id").eq(
        "data_brute->_meta->>code_postal", cp).limit(1).execute().data)
    has_sl  = bool(sb.table("stg_seloger").select("id").eq(
        "data_brute->_meta->>code_postal", cp).limit(1).execute().data)
    if not has_lbc and not has_sl:
        _warn(f"Aucune donnée en staging pour {cp}")
        print(f"  Lance d'abord : {C}python3 pipeline.py scrape {cp} {commune}{RST}")
        return

    print(f"\n{B}=== Transform {commune} ({cp}) ==={RST}\n")
    from transform import run_transform
    run_transform(cp, commune)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def cmd_run(cp: str, commune: str, sl_code: str | None = None,
            source: str | None = None):
    t0 = time.time()
    result = cmd_scrape(cp, commune, sl_code, source)
    if result is None:
        return
    print()
    cmd_transform(cp, commune)

    print(f"\n  Enrichissement SIRENE des nouvelles entités...")
    try:
        from enrich_sirene import enrich
        n = enrich(quiet=True)
        if n:
            _ok(f"{n} nouvelles entités enrichies via SIRENE")
        else:
            print(f"  Aucune nouvelle entité à enrichir")
    except Exception as e:
        _warn(f"Enrichissement SIRENE non bloquant : {e}")

    print(f"\n  {B}Durée totale : {int(time.time() - t0)}s{RST}")


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def cmd_reset(cp: str):
    print(f"\n{Y}⚠️  Reset matching pour {cp}{RST}")
    print(f"  Efface sur_lbc/sur_seloger/match_confidence/cluster_bien_id")
    print(f"  pour toutes les annonces actives de ce code postal.")
    confirm = input(f"  Taper le code postal pour confirmer : ").strip()
    if confirm != cp:
        print("  Annulé.")
        return

    sb = _sb()
    # LBC : réinitialise le flag seloger et les métadonnées de matching
    sb.table("annonces").update({
        "sur_seloger":        False,
        "match_confidence":   None,
        "cluster_bien_id":    None,
        "cluster_confidence": None,
    }).eq("code_postal", cp).eq("est_active", True).eq("source", "lbc").execute()

    # SeLoger : réinitialise le flag lbc et les métadonnées de matching
    sb.table("annonces").update({
        "sur_lbc":            False,
        "match_confidence":   None,
        "cluster_bien_id":    None,
        "cluster_confidence": None,
    }).eq("code_postal", cp).eq("est_active", True).eq("source", "seloger").execute()

    print(f"  {G}Reset effectué.{RST} Relance matching...")
    from transform import intra_entity_match, cross_entity_match
    n_intra = intra_entity_match(cp)
    cl      = cross_entity_match(cp)
    print(f"  {G}✅{RST} {n_intra} paires intra | {cl['n_clusters']} clusters cross")


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------

def cmd_enrich(refresh: bool = False, dry_run: bool = False):
    print(f"\n{B}=== Enrichissement SIRENE ==={RST}\n")
    from enrich_sirene import enrich
    enrich(refresh=refresh, dry_run=dry_run)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Orchestrateur Ideeri — scraping + transform",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Vérifie l'environnement")

    p = sub.add_parser("status", help="État d'un code postal")
    p.add_argument("cp")

    p = sub.add_parser("scrape", help="Scrape un code postal")
    p.add_argument("cp")
    p.add_argument("commune")
    p.add_argument("--sl-code", dest="sl_code", default=None,
                   help="Code SeLoger (ex: AD08FR28776) — optionnel, par défaut = CP")
    p.add_argument("--source", choices=["seloger", "lbc"], default=None,
                   help="Scraper une seule source (par défaut : les deux)")

    p = sub.add_parser("transform", help="Transforme les données staging")
    p.add_argument("cp")
    p.add_argument("commune")

    p = sub.add_parser("run", help="Scrape + transform enchaînés")
    p.add_argument("cp")
    p.add_argument("commune")
    p.add_argument("--sl-code", dest="sl_code", default=None)
    p.add_argument("--source", choices=["seloger", "lbc"], default=None,
                   help="Scraper une seule source (par défaut : les deux)")

    p = sub.add_parser("reset", help="Réinitialise le matching d'un code postal")
    p.add_argument("cp")

    p = sub.add_parser("enrich", help="Enrichit les entités via l'API SIRENE")
    p.add_argument("--all",     action="store_true", help="Ré-enrichit même les déjà traités")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Affiche sans écrire")

    args = parser.parse_args()

    {
        "check":     lambda: cmd_check(),
        "status":    lambda: cmd_status(args.cp),
        "scrape":    lambda: cmd_scrape(args.cp, args.commune, getattr(args, "sl_code", None), getattr(args, "source", None)),
        "transform": lambda: cmd_transform(args.cp, args.commune),
        "run":       lambda: cmd_run(args.cp, args.commune, getattr(args, "sl_code", None), getattr(args, "source", None)),
        "reset":     lambda: cmd_reset(args.cp),
        "enrich":    lambda: cmd_enrich(getattr(args, "all", False), getattr(args, "dry_run", False)),
    }[args.cmd]()


if __name__ == "__main__":
    main()
