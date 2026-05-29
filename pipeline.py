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

import argparse, json, math, os, re, sys, threading, time, unicodedata
from urllib.parse import quote as _urlquote
import lzstring, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

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


def _state_slug(commune: str) -> str:
    """Normalise le nom de commune pour le nom de fichier : 'La Chapelle-Villars' → 'la_chapelle_villars'."""
    nfkd = unicodedata.normalize("NFKD", (commune or "").lower())
    s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _state_file(cp: str, commune: str) -> Path:
    return DEBUG / f"scrape_state_{cp}_{_state_slug(commune)}.json"

# ---------------------------------------------------------------------------
# ScrapingBee
# ---------------------------------------------------------------------------

SB_URL     = "https://app.scrapingbee.com/api/v1/"
SL_PARAMS  = {"render_js": "false", "premium_proxy": "true", "country_code": "fr"}
LBC_PARAMS = {"render_js": "true", "stealth_proxy": "true", "wait": "6000", "block_resources": "false", "country_code": "fr"}
DEBUG      = Path(__file__).parent / "debug"
DEBUG.mkdir(exist_ok=True)


def _sb():
    from supabase import create_client
    return create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])


def _upload_html_direct(pid: str, content: bytes) -> None:
    """Upload le contenu HTML brut vers Supabase Storage en arrière-plan, sans écriture disque."""
    svc_key = os.environ.get("SUPA_SERVICE_KEY")
    if not svc_key:
        return
    def _do():
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPA_URL"], svc_key)
            sb.storage.from_("debug-html").upload(
                path=f"{pid}.html",
                file=content,
                file_options={"content-type": "text/html", "upsert": "true"},
            )
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _upload_html(filepath: Path) -> None:
    """Upload un fichier HTML local vers Supabase Storage en arrière-plan (legacy)."""
    svc_key = os.environ.get("SUPA_SERVICE_KEY")
    if not svc_key:
        return
    def _do():
        try:
            from supabase import create_client
            sb = create_client(os.environ["SUPA_URL"], svc_key)
            with open(filepath, "rb") as fh:
                content = fh.read()
            sb.storage.from_("debug-html").upload(
                path=filepath.name,
                file=content,
                file_options={"content-type": "text/html", "upsert": "true"},
            )
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


_LBC_PREMIUM_PARAMS = {"render_js": "true", "premium_proxy": "true", "wait": "6000",
                       "block_resources": "true", "country_code": "fr"}

# Params pour zones urbaines denses (arrondissements Lyon, Paris…) où Datadome est agressif.
# premium_proxy + wait=10000ms + block_resources=False (recommandé par ScrapingBee sur erreur 613).
# 1 seul worker (moins détectable). Crédits 613 non facturés par ScrapingBee.
_LBC_URBAN_PARAMS = {"render_js": "true", "premium_proxy": "true", "wait": "10000",
                     "block_resources": "false", "country_code": "fr"}

_GHOST_MAX_BYTES = 2000  # Datadome wrapper vide ~586o — toute réponse HTTP 200 sous ce seuil est rejetée


def _fetch(pid: str, url: str, params: dict, timeout: int) -> dict:
    """Fetche une URL via ScrapingBee. Backoff automatique sur HTTP 500 (wait +2000ms, 4 retries max).
    Démarre en stealth_proxy pour LBC ; si 613, bascule sur premium_proxy.
    Un HTTP 200 < _GHOST_MAX_BYTES est traité comme un fantôme Datadome (retry + status None final)."""
    t0 = time.time()
    current_params = dict(params)
    max_attempts = 5 if "wait" in params else 1
    premium_fallback = False

    for attempt in range(max_attempts):
        if attempt > 0:
            extra = attempt * 2000
            base_wait = _LBC_PREMIUM_PARAMS["wait"] if premium_fallback else params["wait"]
            current_params["wait"] = str(int(base_wait) + extra)
            pause = attempt * 15
            print(f"    ↩ retry {attempt} dans {pause}s (wait={current_params['wait']}ms)...", end=" ", flush=True)
            time.sleep(pause)
        try:
            r = requests.get(SB_URL,
                             params={"api_key": os.environ["SCRAPINGBEE_KEY"], "url": url, **current_params},
                             timeout=timeout)
            html = r.content.decode("utf-8", errors="replace")
            is_ghost = r.status_code == 200 and len(r.content) < _GHOST_MAX_BYTES
            if r.status_code == 200 and not is_ghost:
                _upload_html_direct(pid, r.content)
            else:
                (DEBUG / f"{pid}.html").write_text(html, encoding="utf-8")
            if is_ghost:
                print(f"    ⚠ fantôme {len(r.content)}o (Datadome)", end=" ", flush=True)
                if attempt < max_attempts - 1:
                    continue
                return {"id": pid, "url": url, "status": None, "html": html,
                        "error": f"datadome_ghost_{len(r.content)}o", "elapsed": round(time.time() - t0, 1)}
            if r.status_code == 200:
                return {"id": pid, "url": url, "status": 200, "html": html,
                        "error": None, "elapsed": round(time.time() - t0, 1)}
            # 613 = Datadome block sur stealth proxy → bascule premium pour les tentatives suivantes
            if r.status_code == 500 and "Server responded with 613" in html and not premium_fallback:
                premium_fallback = True
                current_params = dict(_LBC_PREMIUM_PARAMS)
                print(f"    ⚡ 613 → premium", end=" ", flush=True)
            if attempt == max_attempts - 1:
                return {"id": pid, "url": url, "status": r.status_code, "html": html,
                        "error": None, "elapsed": round(time.time() - t0, 1)}
        except Exception as e:
            if attempt == max_attempts - 1:
                return {"id": pid, "url": url, "status": None, "html": "",
                        "error": str(e), "elapsed": round(time.time() - t0, 1)}

    return {"id": pid, "url": url, "status": None, "html": "", "error": "unreachable",
            "elapsed": round(time.time() - t0, 1)}


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

def _geocode(commune: str, cp: str) -> dict | None:
    """Retourne {lat, lon, commune_officielle, code_insee} depuis api-adresse.data.gouv.fr."""
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": commune, "postcode": cp, "type": "municipality", "limit": 1},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        features = r.json().get("features", [])
        if not features:
            return None
        feat = features[0]
        lon, lat = feat["geometry"]["coordinates"]
        props = feat.get("properties", {})
        return {
            "lat": lat, "lon": lon,
            "commune_officielle": props.get("city", commune),
            "code_insee": props.get("citycode", ""),
        }
    except Exception:
        return None


def _lbc_base(commune: str, cp: str, lbc_loc: str | None = None) -> str:
    """Construit l'URL LBC de base.
    lbc_loc : valeur du paramètre locations= (ou URL complète) fournie manuellement.
    """
    if lbc_loc:
        # Accepte une URL complète ou juste la valeur locations=
        if lbc_loc.startswith("http"):
            m = re.search(r"[?&]locations=([^&]+)", lbc_loc)
            loc = m.group(1) if m else _urlquote(lbc_loc, safe="%-._~")
        else:
            loc = lbc_loc
        return (
            "https://www.leboncoin.fr/recherche"
            "?category=9"
            f"&locations={loc}"
            "&immo_sell_type=old"
            "&real_estate_type=2,1"
            "&owner_type=all"
        )
    # Garder les accents — LBC en a besoin pour identifier la commune (Vérin ≠ Verin)
    slug = _urlquote(commune, safe="-")
    geo = _geocode(commune, cp)
    if geo:
        lat, lon = geo["lat"], geo["lon"]
        return (
            "https://www.leboncoin.fr/recherche"
            "?category=9"
            f"&locations={slug}_{cp}__{lat}_{lon}_5000"
            "&immo_sell_type=old"
            "&real_estate_type=2,1"
            "&owner_type=all"
        )
    _warn(f"Géocodage échoué pour {commune} {cp} — fallback sans coordonnées")
    return (
        "https://www.leboncoin.fr/recherche"
        "?category=9"
        f"&locations={slug}_{cp}"
        "&immo_sell_type=old"
        "&real_estate_type=2,1"
        "&owner_type=all"
    )


def _lbc_page(base_url: str, page: int) -> str:
    """Ajoute &page=N pour la pagination LBC (page 1 = pas de paramètre)."""
    if page <= 1:
        return base_url
    return f"{base_url}&page={page}"


def _sl_base(code: str) -> str:
    return (
        "https://www.seloger.com/classified-search"
        "?classifiedBusiness=Professional&distributionTypes=Buy"
        f"&estateTypes=House,Apartment&locations={code}&projectTypes=Resale"
    )


def _insert_stg(table: str, commune: str, cp: str, page: int,
                url: str, raw: dict, nb: int, sb) -> bool:
    """Insère en stg_*. Retente 2 fois sur erreur réseau/Supabase transitoire.
    Retourne False si l'insert échoue définitivement → page ajoutée à err_* par l'appelant."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "scraped_at":  now,
        "code_postal": cp,
        "commune":     commune,
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
    for attempt in range(3):
        try:
            sb.table(table).insert(payload).execute()
            return True
        except Exception as e:
            if attempt == 2:
                print(f"    {R}[SUPA ERR définitif p{page}]{RST} {e}")
                return False
            print(f"    {Y}[SUPA retry {attempt+1}/2]{RST} {e}")
            time.sleep(2 * (attempt + 1))


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

    # Runs récents
    runs = sb.table("runs").select(
        "id, scraped_at, statut, nb_annonces_trouvees, pages_erreur_lbc, pages_erreur_sl"
    ).eq("code_postal", cp).order("scraped_at", desc=True).limit(5).execute().data

    if runs:
        last = runs[0]
        dt     = last["scraped_at"][:16].replace("T", " ")
        statut = last["statut"] or "?"
        icon   = G if statut == "ok" else (Y if statut == "running" else R)
        print(f"  📅 dernier run   : {dt} (run #{last['id']}) — {icon}{statut}{RST}")
        if statut == "running":
            _warn(f"Run #{last['id']} toujours 'running' — transform crashé ? Relance : python3 pipeline.py transform {cp} <commune>")
        if statut == "error":
            _warn(f"Run #{last['id']} en erreur — relance : python3 pipeline.py transform {cp} <commune>")
        err_lbc = last.get("pages_erreur_lbc") or []
        err_sl  = last.get("pages_erreur_sl")  or []
        if err_lbc or err_sl:
            _warn(f"Pages manquantes — LBC: {err_lbc} | SeLoger: {err_sl} → python3 pipeline.py retry {cp}")
    else:
        print(f"  📅 dernier run   : aucun")

    # Scrape incomplet (state files)
    for sf in sorted(DEBUG.glob(f"scrape_state_{cp}_*.json")):
        state = json.loads(sf.read_text(encoding="utf-8"))
        err_lbc = state.get("pages_erreur_lbc", [])
        err_sl  = state.get("pages_erreur_sl",  [])
        if err_lbc or err_sl:
            comm = state.get("commune", cp)
            _warn(f"Scrape incomplet ({comm}) — LBC: {err_lbc} | SeLoger: {err_sl}")
            print(f"    → {C}python3 pipeline.py retry {cp} {comm}{RST}")

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

def _save_scrape_state(cp: str, commune: str, lbc_base: str | None, sl_base: str | None,
                       lbc_pages: int, sl_pages: int,
                       err_lbc: list[int], err_sl: list[int],
                       commune_officielle: str | None = None,
                       code_insee: str | None = None) -> None:
    err_lbc_set = set(err_lbc)
    err_sl_set  = set(err_sl)
    state = {
        "commune": commune, "cp": cp,
        "commune_officielle": commune_officielle or commune,
        "code_insee": code_insee or "",
        "lbc_base": lbc_base, "sl_base": sl_base,
        "lbc_pages": lbc_pages, "sl_pages": sl_pages,
        "lbc_scraped": [p for p in range(1, lbc_pages + 1) if p not in err_lbc_set],
        "sl_scraped":  [p for p in range(1, sl_pages  + 1) if p not in err_sl_set],
        "pages_erreur_lbc": sorted(err_lbc),
        "pages_erreur_sl":  sorted(err_sl),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    _state_file(cp, commune).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _confirm(prompt: str, auto: bool) -> bool:
    if auto:
        print(f"  {prompt} → o (auto)")
        return True
    return input(prompt).strip().lower() == "o"


def _get_credits_remaining() -> int | None:
    try:
        r = requests.get(
            "https://app.scrapingbee.com/api/v1/usage",
            params={"api_key": os.environ["SCRAPINGBEE_KEY"]},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            used = d.get("used_api_credits", 0)
            maxi = d.get("max_api_credits")
            return (maxi - used) if isinstance(maxi, int) else None
    except Exception:
        pass
    return None


def cmd_scrape(cp: str, commune: str, sl_code: str | None = None,
               source: str | None = None, lbc_loc: str | None = None,
               auto_yes: bool = False, lbc_premium: bool = False) -> dict | None:
    do_sl  = source in (None, "seloger")
    do_lbc = source in (None, "lbc")
    sb       = _sb()
    # Géocodage : lat/lon + nom officiel INSEE pour normalisation commune LBC
    geo = _geocode(commune, cp) if not lbc_loc else None
    commune_officielle = geo["commune_officielle"] if geo else commune
    code_insee         = geo["code_insee"]         if geo else ""
    lbc_base = _lbc_base(commune, cp, lbc_loc)
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

    # Sonde LBC p1 — utilise _lbc_params résolu plus bas, mais il faut le définir ici aussi
    _lbc_params_sondage = _LBC_URBAN_PARAMS if lbc_premium else LBC_PARAMS
    lbc_ads, lbc_raw, lbc_total, lbc_pages = [], {}, 0, 0
    r_lbc = {"status": None, "html": ""}
    if do_lbc:
        print(f"  LBC p1...", end=" ", flush=True)
        r_lbc = _fetch(f"lbc_{cp}_p1", _lbc_page(lbc_base, 1), _lbc_params_sondage, timeout=180)
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
            if not _confirm("  Écraser et re-scraper ? [o/n] : ", auto_yes):
                print("  Annulé.")
                return None
            break

    if not _confirm("  Continuer ? [o/n] : ", auto_yes):
        print("  Annulé.")
        return None

    # Tracking pages en erreur.
    # Les fantômes Datadome retournent status None depuis _fetch() — ils tombent dans le else.
    # Ce cas residuel (status 200, 0 annonces) couvre : commune vide ou échec de parsing.
    err_lbc: list[int] = ([] if (r_lbc["status"] == 200 and lbc_total > 0)
                          else ([1] if do_lbc else []))
    err_sl:  list[int] = ([] if (r_sl["status"]  == 200 and sl_total  > 0)
                          else ([1] if do_sl  else []))

    if do_lbc and lbc_total == 0 and r_lbc["status"] == 200:
        _warn("Sondage LBC échoué — HTTP 200 mais 0 annonces parsées (commune vide ou parsing raté)")

    # Insérer page 1 uniquement si contenu valide — HTTP 200 avec 0 annonces = fantôme Datadome
    if do_sl  and r_sl["status"]  == 200 and sl_ids:
        if not _insert_stg("stg_seloger", commune, cp, 1, sl_base, sl_raw, len(sl_ids), sb):
            err_sl.append(1)
    if do_lbc and r_lbc["status"] == 200 and lbc_ads:
        if not _insert_stg("stg_lbc", commune, cp, 1, _lbc_page(lbc_base, 1), lbc_raw, len(lbc_ads), sb):
            err_lbc.append(1)

    # Construire les tâches pages 2..N
    tasks = []
    if do_sl:
        for p in range(2, sl_pages  + 1):
            tasks.append(("seloger", p, f"{sl_base}&page={p}",   SL_PARAMS,  45))
    _lbc_params = _LBC_URBAN_PARAMS if lbc_premium else LBC_PARAMS
    if do_lbc:
        for p in range(2, lbc_pages + 1):
            tasks.append(("lbc",     p, _lbc_page(lbc_base, p),  _lbc_params, 180))

    total_sl  = len(sl_ids)
    total_lbc = len(lbc_ads)
    ok_sl     = 1 if r_sl["status"]  == 200 else 0
    ok_lbc    = 1 if r_lbc["status"] == 200 else 0

    if tasks:
        # lbc_premium : 1 seul worker (moins détectable par Datadome en zone urbaine dense)
        has_lbc = any(s == "lbc" for s, *_ in tasks)
        max_w = 1 if (has_lbc and lbc_premium) else (2 if has_lbc else 5)
        print(f"\n  Scraping {len(tasks)} pages restantes (max {max_w} concurrent)...\n")
        with ThreadPoolExecutor(max_workers=max_w) as ex:
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
                (err_lbc if src == "lbc" else err_sl).append(p)
                continue
            if src == "seloger":
                ids, raw, _ = _parse_seloger(r["html"])
                if not ids:
                    err_sl.append(p)
                    print(f"  {Y}⚠  seloger p{p} | HTTP 200 mais 0 annonces (parsing raté ?){RST}")
                    continue
                if not _insert_stg("stg_seloger", commune, cp, p, url, raw, len(ids), sb):
                    err_sl.append(p)
                    continue
                total_sl += len(ids)
                ok_sl    += 1
            else:
                ads, raw, _ = _parse_lbc(r["html"])
                if not ads:
                    err_lbc.append(p)
                    print(f"  {Y}⚠  lbc     p{p} | HTTP 200 mais 0 annonces (parsing raté ?){RST}")
                    continue
                if not _insert_stg("stg_lbc", commune, cp, p, url, raw, len(ads), sb):
                    err_lbc.append(p)
                    continue
                total_lbc += len(ads)
                ok_lbc    += 1

    # Sauvegarder l'état pour permettre un retry ciblé
    _save_scrape_state(cp, commune,
                       lbc_base if do_lbc else None,
                       sl_base  if do_sl  else None,
                       lbc_pages, sl_pages, err_lbc, err_sl,
                       commune_officielle, code_insee)

    print(f"\n  {B}Résultat scraping :{RST}")
    if do_lbc:
        err_str = f"  {Y}⚠️  pages LBC en erreur : {err_lbc}{RST}" if err_lbc else ""
        print(f"  LBC     : {total_lbc} ann. sur {lbc_pages} pages ({ok_lbc} OK / {lbc_pages - ok_lbc} ERR){err_str}")
    if do_sl:
        err_str = f"  {Y}⚠️  pages SeLoger en erreur : {err_sl}{RST}" if err_sl else ""
        print(f"  SeLoger : {total_sl} ann. sur {sl_pages} pages ({ok_sl} OK / {sl_pages - ok_sl} ERR){err_str}")
    print(f"  Crédits consommés : ~{credits_sl + credits_lbc}")

    has_errors = bool(err_lbc or err_sl)
    if has_errors:
        print(f"\n  {Y}Run incomplet — pour compléter les pages manquantes :{RST}")
        print(f"    {C}python3 pipeline.py retry {cp} {commune}{RST}")
    else:
        print(f"\n  → Lance transform : {C}python3 pipeline.py transform {cp} {commune}{RST}")

    return {"total_sl": total_sl, "total_lbc": total_lbc,
            "sl_pages": sl_pages, "lbc_pages": lbc_pages,
            "credits": credits_sl + credits_lbc,
            "err_lbc": err_lbc, "err_sl": err_sl}


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

def cmd_batch(min_credits: int = 300, source: str | None = None, force: bool = False):
    """Lance le pipeline sur toutes les zones actives dans zones_ref (actif=true)."""
    sb = _sb()

    zones = sb.table("zones_ref").select("cp, commune, sl_code, lbc_loc, priorite") \
               .eq("actif", True).order("priorite").execute().data
    if not zones:
        _err("Aucune zone active dans zones_ref (actif=true)")
        return

    credits = _get_credits_remaining()
    print(f"\n{B}=== Batch {len(zones)} zones ==={RST}")
    print(f"  Crédits disponibles : {credits if credits is not None else '?'}\n")

    today = datetime.now(timezone.utc).date().isoformat()
    results = []

    for z in zones:
        cp      = z["cp"]
        commune = z["commune"]
        sl_code = z.get("sl_code") or None
        lbc_loc = z.get("lbc_loc") or None

        print(f"\n{B}--- {commune} ({cp}) ---{RST}")

        if not force:
            already = sb.table("runs").select("scraped_at").eq("code_postal", cp).eq(
                "statut", "ok").order("scraped_at", desc=True).limit(1).execute().data
            if already and (already[0].get("scraped_at") or "")[:10] == today:
                _ok(f"Déjà scrapé aujourd'hui — skip")
                results.append({"cp": cp, "commune": commune, "status": "skipped"})
                continue

        credits = _get_credits_remaining()
        if credits is not None and credits < min_credits:
            _warn(f"Quota insuffisant ({credits} crédits < seuil {min_credits}) — arrêt batch")
            break

        try:
            cmd_run(cp, commune, sl_code=sl_code, lbc_loc=lbc_loc,
                    source=source, auto_yes=True)
            results.append({"cp": cp, "commune": commune, "status": "ok"})
        except Exception as e:
            _err(f"{commune} — {e}")
            results.append({"cp": cp, "commune": commune, "status": "error", "error": str(e)})

    print(f"\n{B}=== Résumé batch ==={RST}")
    for r in results:
        icon = f"{G}✅{RST}" if r["status"] == "ok" else (f"{C}⏭ {RST}" if r["status"] == "skipped" else f"{R}❌{RST}")
        print(f"  {icon} {r['commune']} ({r['cp']}) — {r['status']}")


def cmd_retry(cp: str, commune: str | None = None, wait_override: int | None = None,
              lbc_pages_override: str | None = None, sl_pages_override: str | None = None):
    has_override = lbc_pages_override is not None or sl_pages_override is not None

    if commune:
        candidates = [_state_file(cp, commune)]
    else:
        candidates = sorted(DEBUG.glob(f"scrape_state_{cp}_*.json"))

    # Charger tous les state files valides
    valid_states: list[tuple[Path, dict]] = []
    for sf in candidates:
        if not sf.exists():
            continue
        try:
            raw = sf.read_text(encoding="utf-8").strip()
            if not raw:
                _warn(f"State file vide ignoré : {sf.name}")
                continue
            st = json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:
            _warn(f"State file illisible ({sf.name}) : {e}")
            continue
        valid_states.append((sf, st))

    if has_override:
        # Avec override : utiliser n'importe quel state (sans exiger des erreurs)
        if not valid_states:
            _err(f"Pas d'état de scrape trouvé pour {cp}")
            print(f"  Lance d'abord : {C}python3 pipeline.py run <cp> <commune>{RST}")
            return
        if len(valid_states) > 1:
            _warn(f"Plusieurs communes pour {cp} :")
            for sf, st in valid_states:
                print(f"    → python3 pipeline.py retry {cp} {st.get('commune','?')}")
            return
        state_file, state = valid_states[0]
    else:
        # Sans override : seulement les states avec erreurs
        states_with_errors = [(sf, st) for sf, st in valid_states
                               if st.get("pages_erreur_lbc") or st.get("pages_erreur_sl")]
        if not states_with_errors:
            if valid_states:
                _ok(f"Run {cp} complet — aucune page manquante")
            else:
                _err(f"Pas d'état de scrape trouvé pour {cp}")
                print(f"  Lance d'abord : {C}python3 pipeline.py run <cp> <commune>{RST}")
            return

        if len(states_with_errors) > 1:
            _warn(f"Plusieurs communes incomplètes pour {cp} :")
            for sf, st in states_with_errors:
                comm = st.get("commune", "?")
                print(f"    → python3 pipeline.py retry {cp} {comm}")
            return
        state_file, state = states_with_errors[0]

    commune  = state["commune"]
    err_lbc  = state.get("pages_erreur_lbc", [])
    err_sl   = state.get("pages_erreur_sl",  [])
    lbc_base = state.get("lbc_base")
    sl_base  = state.get("sl_base")

    # Appliquer les overrides de pages si fournis
    if lbc_pages_override is not None:
        err_lbc = [int(x.strip()) for x in lbc_pages_override.split(",") if x.strip().isdigit()]
    if sl_pages_override is not None:
        err_sl  = [int(x.strip()) for x in sl_pages_override.split(",") if x.strip().isdigit()]

    if not err_lbc and not err_sl:
        _ok(f"Run {cp} ({commune}) — aucune page à re-scraper")
        return

    print(f"\n{B}=== Retry {commune} ({cp}) ==={RST}\n")
    if err_lbc: print(f"  {Y}Pages LBC manquantes   : {err_lbc}{RST}")
    if err_sl:  print(f"  {Y}Pages SeLoger manquantes: {err_sl}{RST}")
    if wait_override:
        print(f"  Wait LBC : {wait_override}ms")
    print()

    lbc_params = {**LBC_PARAMS, "wait": str(wait_override)} if wait_override else LBC_PARAMS

    tasks = []
    if lbc_base:
        for p in err_lbc:
            tasks.append(("lbc",     p, _lbc_page(lbc_base, p),      lbc_params, 180))
    if sl_base:
        for p in err_sl:
            url = sl_base if p == 1 else f"{sl_base}&page={p}"
            tasks.append(("seloger", p, url,                          SL_PARAMS,  45))

    rep = input(f"  Re-scraper {len(tasks)} page(s) manquante(s) ? [o/n] : ").strip().lower()
    if rep != "o":
        print("  Annulé.")
        return

    sb = _sb()
    still_err_lbc = list(err_lbc)
    still_err_sl  = list(err_sl)

    print(f"\n  Scraping {len(tasks)} page(s)...\n")

    def _fetch_or_cache(src: str, p: int, url: str, params: dict, to: int) -> dict:
        """Priorité : 1) cache local  2) storage Supabase  3) ScrapingBee (crédits)."""
        pid = f"{src}_{cp}_p{p}"
        marker = "__NEXT_DATA__" if src == "lbc" else "__UFRN_FETCHER__"

        # 1. Cache local (debug/*.html)
        for suffix in ("_retry.html", "_diag.html", ".html"):
            cached = DEBUG / f"{pid}{suffix}"
            if cached.exists():
                html = cached.read_text(encoding="utf-8")
                if marker in html:
                    print(f"  {C}↩ {src:8s} p{p} | cache local{RST}")
                    return {"id": pid, "url": url, "status": 200, "html": html, "error": None, "elapsed": 0}

        # 2. Storage Supabase (gratuit — HTML déjà payé)
        try:
            svc_key = os.environ.get("SUPA_SERVICE_KEY") or os.environ.get("SUPA_KEY")
            if svc_key:
                from supabase import create_client as _sc
                _sb2 = _sc(os.environ["SUPA_URL"], svc_key)
                for fname in (f"{pid}.html", f"{pid}_retry.html"):
                    content = _sb2.storage.from_("debug-html").download(fname)
                    html = content.decode("utf-8", errors="replace")
                    if marker in html:
                        print(f"  {C}↩ {src:8s} p{p} | storage{RST}")
                        return {"id": pid, "url": url, "status": 200, "html": html, "error": None, "elapsed": 0}
        except Exception:
            pass

        # 3. ScrapingBee (coûte des crédits — dernier recours)
        print(f"  {Y}↗ {src:8s} p{p} | ScrapingBee (non trouvé en cache){RST}")
        return _fetch(pid + "_retry", url, params, to)

    has_lbc = any(s == "lbc" for s, *_ in tasks)
    with ThreadPoolExecutor(max_workers=2 if has_lbc else 5) as ex:
        fmap = {
            ex.submit(_fetch_or_cache, src, p, url, params, to): (src, p, url)
            for src, p, url, params, to in tasks
        }
        for fut in as_completed(fmap):
            src, p, url = fmap[fut]
            r = fut.result()
            if r["elapsed"] > 0:  # pas depuis cache
                icon = f"{G}✓{RST}" if r["status"] == 200 else f"{R}✗{RST}"
                print(f"  {icon} {src:8s} p{p} | HTTP {r['status'] or 'ERR'} | {r['elapsed']}s")

            if r.get("status") != 200:
                continue

            if src == "lbc":
                ads, raw, _ = _parse_lbc(r["html"])
                if not ads:
                    print(f"  {Y}⚠  lbc     p{p} | HTTP 200 mais 0 annonces (parsing raté ?){RST}")
                    continue
                _insert_stg("stg_lbc",     commune, cp, p, url, raw, len(ads), sb)
                still_err_lbc = [x for x in still_err_lbc if x != p]
            else:
                ids, raw, _ = _parse_seloger(r["html"])
                if not ids:
                    print(f"  {Y}⚠  seloger p{p} | HTTP 200 mais 0 annonces (parsing raté ?){RST}")
                    continue
                _insert_stg("stg_seloger", commune, cp, p, url, raw, len(ids), sb)
                still_err_sl = [x for x in still_err_sl if x != p]

    # Mettre à jour l'état
    state["pages_erreur_lbc"] = still_err_lbc
    state["pages_erreur_sl"]  = still_err_sl
    lbc_pages = state.get("lbc_pages", 0)
    sl_pages  = state.get("sl_pages",  0)
    state["lbc_scraped"] = [p for p in range(1, lbc_pages + 1) if p not in set(still_err_lbc)]
    state["sl_scraped"]  = [p for p in range(1, sl_pages  + 1) if p not in set(still_err_sl)]
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    recovered = len(err_lbc) - len(still_err_lbc) + len(err_sl) - len(still_err_sl)

    if still_err_lbc or still_err_sl:
        _warn(f"Pages encore en erreur — LBC: {still_err_lbc} | SeLoger: {still_err_sl}")
        _warn(f"Relance : python3 pipeline.py retry {cp} {commune}")
    else:
        _ok("Toutes les pages récupérées")

    if recovered == 0:
        _warn("Aucune nouvelle page récupérée — transform ignoré")
        return

    print()
    cmd_transform(cp, commune)

    print(f"\n  Enrichissement SIRENE des nouvelles entités...")
    try:
        from enrich_sirene import enrich
        n = enrich(quiet=True)
        if n:
            _ok(f"{n} nouvelles entités enrichies via SIRENE")
    except Exception as e:
        _warn(f"Enrichissement SIRENE non bloquant : {e}")


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------

def cmd_transform(cp: str, commune: str,
                  pages_erreur_lbc: list[int] | None = None,
                  pages_erreur_sl:  list[int] | None = None,
                  lbc_total_attendu: int | None = None,
                  sl_total_attendu:  int | None = None):
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
    run_transform(cp, commune,
                  pages_erreur_lbc=pages_erreur_lbc,
                  pages_erreur_sl=pages_erreur_sl,
                  lbc_total_attendu=lbc_total_attendu,
                  sl_total_attendu=sl_total_attendu)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def cmd_run(cp: str, commune: str, sl_code: str | None = None,
            source: str | None = None, lbc_loc: str | None = None,
            auto_yes: bool = False, lbc_premium: bool = False):
    t0 = time.time()
    result = cmd_scrape(cp, commune, sl_code, source, lbc_loc, auto_yes=auto_yes, lbc_premium=lbc_premium)
    if result is None:
        return

    err_lbc = result.get("err_lbc", [])
    err_sl  = result.get("err_sl",  [])

    if err_lbc or err_sl:
        _warn(f"Run incomplet — pages LBC: {err_lbc} | SeLoger: {err_sl}")
        if not _confirm("  Lancer le transform quand même (données partielles) ? [o/n] : ", auto_yes):
            print(f"  Transform annulé. Complète d'abord : {C}python3 pipeline.py retry {cp} {commune}{RST}")
            return

    print()
    cmd_transform(cp, commune,
                  pages_erreur_lbc=err_lbc or None,
                  pages_erreur_sl=err_sl or None,
                  lbc_total_attendu=result.get("lbc_pages"),
                  sl_total_attendu=result.get("sl_pages"))

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

    if err_lbc or err_sl:
        _warn(f"Run partiel terminé. Pour compléter : {C}python3 pipeline.py retry {cp} {commune}{RST}")

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


def cmd_backfill(cp: str | None = None):
    """Peuple is_exclusive, ref_agence, lat/lng depuis les stg_* existants.
    Lancer après migration_new_fields.sql."""
    print(f"\n{B}=== Backfill nouveaux champs {'— ' + cp if cp else '(toutes zones)'} ==={RST}\n")
    from transform import backfill_new_fields, ref_match, _norm_commune
    backfill_new_fields(cp)
    # Rejouer ref_match sur toutes les zones concernées
    sb = _sb()
    q = sb.table("annonces").select("code_postal, commune").eq("est_active", True)
    if cp:
        q = q.eq("code_postal", cp)
    zones = {(r["code_postal"], r["commune"]) for r in q.execute().data}
    print(f"\n  Relance ref_match sur {len(zones)} zone(s)...")
    for zcp, zcom in sorted(zones):
        n = ref_match(zcp, zcom)
        if n:
            print(f"  {zcp} {zcom} : {n} nouvelles paires ref")


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
    p.add_argument("--lbc-loc", dest="lbc_loc", default=None,
                   help="Localisation LBC (URL complète ou valeur locations=, ex: Vérin_42410__45.45439_4.75312_2455)")
    p.add_argument("--source", choices=["seloger", "lbc"], default=None,
                   help="Scraper une seule source (par défaut : les deux)")

    p = sub.add_parser("transform", help="Transforme les données staging")
    p.add_argument("cp")
    p.add_argument("commune")

    p = sub.add_parser("run", help="Scrape + transform enchaînés")
    p.add_argument("cp")
    p.add_argument("commune")
    p.add_argument("--sl-code", dest="sl_code", default=None)
    p.add_argument("--lbc-loc", dest="lbc_loc", default=None,
                   help="Localisation LBC (URL complète ou valeur locations=)")
    p.add_argument("--source", choices=["seloger", "lbc"], default=None,
                   help="Scraper une seule source (par défaut : les deux)")

    p = sub.add_parser("retry", help="Re-scrape les pages manquantes d'un run incomplet")
    p.add_argument("cp")
    p.add_argument("commune", nargs="?", default=None, help="Commune (optionnel si CP unique)")
    p.add_argument("--wait", type=int, default=None, dest="wait_override",
                   help="Override wait LBC en ms (ex: 8000)")
    p.add_argument("--lbc-pages", dest="lbc_pages_override", default=None,
                   help="Pages LBC à re-scraper (ex: 2,3,4) — override le state")
    p.add_argument("--sl-pages", dest="sl_pages_override", default=None,
                   help="Pages SeLoger à re-scraper (ex: 5,6) — override le state")

    p = sub.add_parser("batch", help="Scrape toutes les zones actives (zones_ref actif=true)")
    p.add_argument("--min-credits", type=int, default=300, dest="min_credits",
                   help="Crédits minimum avant de s'arrêter (défaut 300)")
    p.add_argument("--source", choices=["seloger", "lbc"], default=None)
    p.add_argument("--force", action="store_true",
                   help="Re-scrape même si déjà fait aujourd'hui")

    p = sub.add_parser("reset", help="Réinitialise le matching d'un code postal")
    p.add_argument("cp")

    p = sub.add_parser("enrich", help="Enrichit les entités via l'API SIRENE")
    p.add_argument("--all",     action="store_true", help="Ré-enrichit même les déjà traités")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Affiche sans écrire")

    p = sub.add_parser("backfill", help="Peuple is_exclusive/ref_agence/lat/lng depuis les stg_*")
    p.add_argument("cp", nargs="?", default=None, help="Code postal (optionnel — toutes zones si absent)")

    args = parser.parse_args()

    {
        "check":     lambda: cmd_check(),
        "status":    lambda: cmd_status(args.cp),
        "scrape":    lambda: cmd_scrape(args.cp, args.commune, getattr(args, "sl_code", None), getattr(args, "source", None), getattr(args, "lbc_loc", None)),
        "transform": lambda: cmd_transform(args.cp, args.commune),
        "run":       lambda: cmd_run(args.cp, args.commune, getattr(args, "sl_code", None), getattr(args, "source", None), getattr(args, "lbc_loc", None)),
        "batch":     lambda: cmd_batch(getattr(args, "min_credits", 300), getattr(args, "source", None), getattr(args, "force", False)),
        "retry":     lambda: cmd_retry(args.cp, getattr(args, "commune", None), getattr(args, "wait_override", None), getattr(args, "lbc_pages_override", None), getattr(args, "sl_pages_override", None)),
        "reset":     lambda: cmd_reset(args.cp),
        "enrich":    lambda: cmd_enrich(getattr(args, "all", False), getattr(args, "dry_run", False)),
        "backfill":  lambda: cmd_backfill(getattr(args, "cp", None)),
    }[args.cmd]()


if __name__ == "__main__":
    main()
