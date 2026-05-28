"""
api.py — API Flask pour le dashboard Ideeri
Usage : python3 api.py  → localhost:5000
"""

import hashlib, json, os, queue, re, secrets, sys, threading, unicodedata
from datetime import datetime, timezone, timedelta
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, Response, request, stream_with_context, send_file, redirect, make_response
from flask_cors import CORS
from supabase import create_client

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
CORS(app)
HERE = Path(__file__).parent

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"><title>Ideeri — Connexion</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#f0f4f8;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:12px;padding:40px;width:320px;
      box-shadow:0 4px 20px rgba(0,0,0,.08)}
h1{font-size:20px;font-weight:700;margin-bottom:24px;color:#2d3748}
input{width:100%;padding:10px 14px;border:1px solid #e2e8f0;border-radius:8px;
      font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#667eea}
button{width:100%;padding:10px;background:#667eea;color:#fff;border:none;
       border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#5a67d8}
.err{color:#e53e3e;font-size:13px;margin-bottom:12px}
</style></head>
<body><div class="card">
<h1>Ideeri Dashboard</h1>
{error}
<form method="post" action="/login">
<input type="password" name="password" placeholder="Mot de passe" autofocus>
<button type="submit">Connexion</button>
</form></div></body></html>"""


def _auth_token() -> str:
    pwd = os.environ.get("DASHBOARD_PASSWORD", "")
    return hashlib.sha256(pwd.encode()).hexdigest()[:32] if pwd else ""


@app.before_request
def _check_auth():
    pwd = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pwd:
        return  # Auth désactivée en local
    if request.path in ("/login",):
        return
    token = _auth_token()
    if request.cookies.get("ideeri_auth") == token:
        return
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    pwd = os.environ.get("DASHBOARD_PASSWORD", "")
    if request.method == "POST":
        if request.form.get("password") == pwd:
            resp = make_response(redirect("/"))
            resp.set_cookie("ideeri_auth", _auth_token(), max_age=7*24*3600, httponly=True, samesite="Lax")
            return resp
        return _LOGIN_HTML.replace("{error}", '<p class="err">Mot de passe incorrect</p>')
    return _LOGIN_HTML.replace("{error}", "")


@app.route("/")
def index():
    return send_file(HERE / "dashboard.html")

_ANSI = re.compile(r"\033\[[0-9;]*m")

# Logs accumulés en mémoire par CP (survivent à la déconnexion SSE du browser)
_run_logs: dict[str, list[str]] = {}
_run_done: dict[str, bool | None] = {}  # None = en cours, True = ok, False = erreur


def _stream_proc(proc, cp: str, q: "queue.Queue[str | None]") -> None:
    """Thread dédié : lit stdout du subprocess et accumule dans _run_logs[cp]."""
    for line in proc.stdout:
        line = _strip_ansi(line.rstrip())
        if line:
            _run_logs[cp].append(line)
            q.put(line)
    proc.wait()
    _run_done[cp] = (proc.returncode == 0)
    q.put(None)


def _make_sse_generator(proc, cp: str):
    """Générateur SSE qui lit depuis la queue du thread _stream_proc."""
    _run_logs[cp] = []
    _run_done[cp] = None
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_stream_proc, args=(proc, cp, q), daemon=True).start()
    while True:
        try:
            item = q.get(timeout=25)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if item is None:
            yield f"data: {json.dumps('__DONE__')}\n\n"
            break
        yield f"data: {json.dumps(item)}\n\n"


def _sb():
    return create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])


def _norm(s: str) -> str:
    """Normalise commune : minuscules + supprime accents + tirets = espaces."""
    nfkd = unicodedata.normalize("NFKD", (s or "").lower())
    s2   = "".join(c for c in nfkd if not unicodedata.combining(c))
    s2   = re.sub(r"[-\s]+", " ", s2)
    return s2.strip()


def _norm_commune(s: str) -> str:
    """Normalise commune pour comparaison insensible aux ordinals et 'arrondissement'.
    'Lyon 1er arrondissement' → 'lyon 1'  |  'Lyon 1' → 'lyon 1'
    'Paris 18ème arrondissement' → 'paris 18'
    """
    s2 = _norm(s)
    s2 = re.sub(r"\b(\d+)(?:er|eme|ieme|e)\b", r"\1", s2)
    s2 = re.sub(r"\barrondissement\b", "", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _run_status(run: dict | None) -> dict:
    if not run:
        return {"status": "never", "date": None, "lbc_pages": 0, "sl_pages": 0,
                "err_lbc": [], "err_sl": [], "age_days": None}
    err_lbc   = run.get("pages_erreur_lbc") or []
    err_sl    = run.get("pages_erreur_sl")  or []
    lbc_pages = run.get("lbc_total_attendu") or 0
    sl_pages  = run.get("sl_total_attendu")  or 0
    statut    = run.get("statut", "ok")
    date_str  = run.get("scraped_at", "")
    try:
        dt       = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        age_days = 999
    if statut in ("error", "running"):
        status = "error"
    elif err_lbc or err_sl:
        status = "partial"
    elif age_days > 7:
        status = "stale"
    else:
        status = "fresh"
    return {"status": status, "date": date_str[:10] if date_str else None,
            "lbc_pages": lbc_pages, "sl_pages": sl_pages,
            "err_lbc": err_lbc, "err_sl": err_sl, "age_days": age_days}


# ---------------------------------------------------------------------------
# GET /api/zones
# ---------------------------------------------------------------------------

@app.route("/api/zones")
def zones():
    sb = _sb()
    runs_rows = sb.table("runs").select(
        "code_postal, commune, scraped_at, statut, "
        "pages_erreur_lbc, pages_erreur_sl, lbc_total_attendu, sl_total_attendu"
    ).order("scraped_at", desc=True).limit(500).execute().data

    # Grouper par (cp, commune normalisée) — fusionne "Pelussin" et "Pélussin"
    zones_map: dict[tuple, dict] = {}
    for r in runs_rows:
        cp   = r.get("code_postal")
        comm = (r.get("commune") or "").strip()
        if not cp:
            continue
        key = (cp, _norm_commune(comm))
        if key not in zones_map:
            zones_map[key] = {"cp": cp, "commune": comm, "nb_runs": 0, "latest": None}
        else:
            zones_map[key]["commune"] = comm   # garder le nom du run le plus récent
        zones_map[key]["nb_runs"] += 1
        if zones_map[key]["latest"] is None:
            zones_map[key]["latest"] = r

    # Pré-charger toutes les annonces actives par CP (évite N requêtes)
    all_ann_by_cp: dict[str, list] = {}
    for (cp, _), _ in zones_map.items():
        if cp not in all_ann_by_cp:
            rows_cp = sb.table("annonces").select("commune") \
                        .eq("code_postal", cp).eq("est_active", True).execute().data
            all_ann_by_cp[cp] = rows_cp

    result = []
    for (cp, norm_comm), v in sorted(zones_map.items()):
        nb = sum(1 for a in all_ann_by_cp.get(cp, [])
                 if _norm_commune(a.get("commune", "")) == norm_comm)
        result.append({
            "cp":          cp,
            "commune":     v["commune"],
            "nb_annonces": nb,
            "nb_runs":     v["nb_runs"],
            "run_status":  _run_status(v["latest"]),
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# Helper : nb_mandats total sur un CP (sum des biens distincts par entité)
# ---------------------------------------------------------------------------

def _zone_nb_mandats(cp: str, sb) -> int:
    """Retourne la somme des biens uniques par entité sur un CP (base PDM)."""
    rows = sb.table("annonces").select("nom_commercial, bien_id") \
             .eq("code_postal", cp).eq("est_active", True).execute().data
    seen: set[tuple] = set()
    for r in rows:
        if r.get("bien_id") and r.get("nom_commercial"):
            seen.add((r["nom_commercial"], r["bien_id"]))
    return len(seen)


# ---------------------------------------------------------------------------
# GET /api/zone/<cp>
# ---------------------------------------------------------------------------

@app.route("/api/zone/<cp>")
def zone(cp):
    sb = _sb()
    commune_filter = (request.args.get("commune") or "").strip() or None

    rows = sb.table("annonces").select("*").eq("code_postal", cp).eq("est_active", True).execute().data
    if commune_filter:
        norm_cf  = _norm_commune(commune_filter)
        filtered = [r for r in rows if _norm_commune(r.get("commune", "")) == norm_cf]
        if filtered:
            rows = filtered
        # sinon on garde toutes les annonces du CP

    # Vérifier que la zone existe au moins dans runs avant de renvoyer 404
    if not rows:
        runs_check = sb.table("runs").select("id").eq("code_postal", cp).limit(1).execute().data
        if not runs_check:
            return jsonify({"error": "zone inconnue"}), 404

    nb_annonces = len(rows)
    nb_dpe      = sum(1 for r in rows if r.get("dpe"))

    # Biens dédupliqués (pour nb_biens + portail breakdown + multi_mandats)
    biens: dict[str, dict] = {}
    for r in rows:
        bid = r.get("bien_id")
        if not bid:
            continue
        if bid not in biens:
            biens[bid] = {"lbc": False, "sl": False, "entites": set()}
        if r.get("sur_lbc"):     biens[bid]["lbc"] = True
        if r.get("sur_seloger"): biens[bid]["sl"]  = True
        if r.get("entite_id"):   biens[bid]["entites"].add(r["entite_id"])

    nb_biens         = len(biens)
    nb_mandats_lbc   = sum(1 for b in biens.values() if b["lbc"] and not b["sl"])
    nb_mandats_sl    = sum(1 for b in biens.values() if b["sl"]  and not b["lbc"])
    nb_mandats_both  = sum(1 for b in biens.values() if b["lbc"] and b["sl"])
    nb_multi_mandats = sum(1 for b in biens.values() if len(b["entites"]) > 1)

    # Entités : grouper par entite_id (canonique) — évite le split LBC/SeLoger par nom
    ent_map: dict[str, dict] = {}  # key = entite_id si dispo, sinon nom_commercial
    for r in rows:
        key = r.get("entite_id") or (r.get("nom_commercial") or "Inconnu")
        bid = r.get("bien_id")
        if key not in ent_map:
            ent_map[key] = {
                "nom":      r.get("nom_commercial") or "Inconnu",
                "lbc_biens": set(), "sl_biens": set(), "biens": set(),
                "type":     "agence",
            }
        if bid:
            ent_map[key]["biens"].add(bid)
            if r.get("sur_lbc"):     ent_map[key]["lbc_biens"].add(bid)
            if r.get("sur_seloger"): ent_map[key]["sl_biens"].add(bid)

    # Noms canoniques et types depuis la table entites
    eids = [k for k in ent_map if isinstance(k, str) and len(k) == 36 and "-" in k]
    if eids:
        try:
            et_rows = sb.table("entites").select("id, nom_commercial, type_entite") \
                        .in_("id", eids[:200]).execute().data
            for et in et_rows:
                if et["id"] in ent_map:
                    if et.get("nom_commercial"):
                        ent_map[et["id"]]["nom"] = et["nom_commercial"]
                    ent_map[et["id"]]["type"] = et.get("type_entite") or "agence"
        except Exception:
            pass

    entites = sorted(
        [{
            "nom":        v["nom"],
            "nb_mandats": len(v["biens"]),
            "nb_biens":   len(v["biens"]),
            "nb_lbc":     len(v["lbc_biens"]),
            "nb_seloger": len(v["sl_biens"]),
            "type":       v["type"],
        }
         for v in ent_map.values()],
        key=lambda x: x["nb_mandats"], reverse=True,
    )

    # nb_mandats total zone = somme des mandats par entité (base PDM, > nb_biens si multi-mandat)
    nb_mandats = sum(e["nb_mandats"] for e in entites)

    # Mouvements de prix — renommés en price_changes avec champ nom
    price_changes = []
    for r in rows:
        for evt in (r.get("historique_prix") or []):
            ancien = evt.get("ancien_prix")
            if ancien is not None:
                price_changes.append({
                    "nom":        r.get("nom_commercial"),
                    "titre":      r.get("titre"),
                    "ancien_prix": ancien,
                    "prix_actuel": r["prix_affiche"],
                    "delta":      r["prix_affiche"] - ancien,
                    "date":       (evt.get("date") or "")[:10],
                })
    price_changes.sort(key=lambda x: x["date"], reverse=True)

    # Dernier run (filtré par commune si fournie)
    all_runs = sb.table("runs").select("*").eq("code_postal", cp).order("scraped_at", desc=True).limit(50).execute().data
    if commune_filter:
        norm_cf  = _norm_commune(commune_filter)
        runs_filtered = [r for r in all_runs if _norm_commune(r.get("commune", "")) == norm_cf]
        runs_rows = (runs_filtered if runs_filtered else all_runs)[:5]
    else:
        runs_rows = all_runs[:5]
    last_run   = runs_rows[0]["scraped_at"][:16].replace("T", " ") if runs_rows else None
    run_status = _run_status(runs_rows[0] if runs_rows else None)

    return jsonify({
        # ── Identité ──
        "cp":         cp,
        "commune":    commune_filter or (rows[0].get("commune") if rows else None) or cp,
        "last_run":   last_run,
        "run_status": run_status,

        # ── Métriques globales (noms attendus par le dashboard) ──
        "nb_annonces":      nb_annonces,
        "nb_biens":         nb_biens,
        "nb_mandats":       nb_mandats,       # somme des mandats par entité
        "nb_mandats_lbc":   nb_mandats_lbc,   # biens exclusifs LBC (dedup)
        "nb_mandats_sl":    nb_mandats_sl,    # biens exclusifs SeLoger (dedup)
        "nb_mandats_both":  nb_mandats_both,  # biens sur les deux (dedup)
        "nb_entites":       len(entites),
        "nb_multi_mandats": nb_multi_mandats,
        "nb_dpe":           nb_dpe,           # count absolu avec DPE renseigné

        # ── Classement entités ──
        "entites": entites,

        # ── Mouvements de prix ──
        "price_changes": price_changes[:10],

        # ── Runs ──
        "derniers_runs": [
            {
                "id":            r["id"],
                "date":          r["scraped_at"][:16].replace("T", " "),
                "nouvelles":     r.get("nb_nouvelles", 0),
                "disparues":     r.get("nb_disparues", 0),
                "prix_modifies": r.get("nb_prix_modifies", 0),
                "duree":         r.get("duree_secondes"),
                "statut":        r.get("statut", "ok"),
            }
            for r in runs_rows
        ],
    })


# ---------------------------------------------------------------------------
# GET /api/map  — données agrégées par code INSEE pour la carte
# ---------------------------------------------------------------------------

@app.route("/api/map")
def map_data():
    sb = _sb()
    zones = sb.table("zones_ref").select("*").execute().data
    rows  = sb.table("annonces").select("code_insee, bien_id, entite_id, est_active") \
              .eq("est_active", True).execute().data

    by_insee: dict[str, dict] = {}
    for z in zones:
        insee = z["code_insee"]
        by_insee[insee] = {
            "code_insee":         insee,
            "cp":                 z["cp"],
            "commune":            z["commune_officielle"],
            "nb_annonces":        0,
            "nb_biens":           0,
            "nb_entites":         0,
        }

    for r in rows:
        insee = r.get("code_insee")
        if not insee or insee not in by_insee:
            continue
        by_insee[insee]["nb_annonces"] += 1

    # nb_biens et nb_entites : agrégation déduplication
    from collections import defaultdict
    biens_by_insee:   dict[str, set] = defaultdict(set)
    entites_by_insee: dict[str, set] = defaultdict(set)
    for r in rows:
        insee = r.get("code_insee")
        if not insee or insee not in by_insee:
            continue
        if r.get("bien_id"):
            biens_by_insee[insee].add(r["bien_id"])
        if r.get("entite_id"):
            entites_by_insee[insee].add(r["entite_id"])

    for insee, d in by_insee.items():
        d["nb_biens"]   = len(biens_by_insee[insee])
        d["nb_entites"] = len(entites_by_insee[insee])

    return jsonify(list(by_insee.values()))


# ---------------------------------------------------------------------------
# GET /api/entites/search?q=xxx
# ---------------------------------------------------------------------------

@app.route("/api/entites/search")
def entites_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    sb = _sb()
    rows = sb.table("entites") \
             .select("id, nom_commercial, type_entite") \
             .ilike("nom_commercial", f"%{q}%") \
             .order("nom_commercial") \
             .limit(20).execute().data
    return jsonify([
        {"id": r["id"], "nom": r["nom_commercial"], "type": r.get("type_entite")}
        for r in rows
    ])


# ---------------------------------------------------------------------------
# GET /api/entite/<nom>
# ---------------------------------------------------------------------------

@app.route("/api/entite/<path:nom>")
def entite(nom):
    sb = _sb()
    rows = sb.table("annonces") \
             .select("code_postal, commune, code_insee, source, bien_id, prix_affiche, surface, titre, dpe") \
             .eq("nom_commercial", nom).eq("est_active", True).execute().data

    # Agréger par CP
    cp_map: dict[str, dict] = {}
    for r in rows:
        cp = r["code_postal"]
        if cp not in cp_map:
            cp_map[cp] = {"commune": r.get("commune"), "code_insee": r.get("code_insee"), "nb_lbc": 0, "nb_sl": 0, "biens": set()}
        if not cp_map[cp].get("code_insee") and r.get("code_insee"):
            cp_map[cp]["code_insee"] = r["code_insee"]
        if r["source"] == "lbc":
            cp_map[cp]["nb_lbc"] += 1
        else:
            cp_map[cp]["nb_sl"] += 1
        if r.get("bien_id"):
            cp_map[cp]["biens"].add(r["bien_id"])

    # Pour chaque zone, récupérer le nb_mandats total de la zone (pour calcul PDM)
    zones_out = []
    for cp, v in cp_map.items():
        nb_mandats_zone = _zone_nb_mandats(cp, sb)
        zones_out.append({
            "cp":                cp,
            "commune":           v["commune"],
            "code_insee":        v.get("code_insee"),
            "nb_mandats":        len(v["biens"]),
            "nb_biens":          len(v["biens"]),
            "nb_lbc":            v["nb_lbc"],
            "nb_seloger":        v["nb_sl"],
            "total_mandats_zone": nb_mandats_zone,
        })
    zones_out.sort(key=lambda x: x["nb_mandats"], reverse=True)

    ent_rows = sb.table("entites").select("*").eq("nom_commercial", nom).execute().data
    ent_data = ent_rows[0] if ent_rows else {}

    historique   = ent_data.get("historique_activite") or []
    hist_sorted  = sorted(historique, key=lambda h: h.get("date", ""))
    month_ago    = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    last_entry   = hist_sorted[-1] if hist_sorted else None
    old_entry    = next((h for h in reversed(hist_sorted) if h.get("date", "") <= month_ago), None)
    trend_30j    = None
    if last_entry and old_entry and last_entry.get("date") != old_entry.get("date"):
        trend_30j = (last_entry.get("nb_total") or 0) - (old_entry.get("nb_total") or 0)
    activite = {
        "date_premiere_obs": ent_data.get("date_premiere_obs"),
        "date_derniere_obs": last_entry.get("date") if last_entry else None,
        "nb_obs":            len(historique),
        "trend_30j":         trend_30j,
    }

    annonces_out = [
        {
            "id":      r.get("id_annonce"),
            "titre":   r.get("titre"),
            "prix":    r.get("prix_affiche"),
            "surface": r.get("surface"),
            "cp":      r.get("code_postal"),
            "commune": r.get("commune"),
            "dpe":     r.get("dpe"),
            "source":  r.get("source"),
        }
        for r in sorted(rows, key=lambda x: x.get("prix_affiche") or 0, reverse=True)[:20]
    ]

    return jsonify({
        "nom":        nom,
        "type":       ent_data.get("type_entite"),
        "siren":      ent_data.get("siren"),
        "sur_lbc":    ent_data.get("sur_lbc"),
        "sur_seloger": ent_data.get("sur_seloger"),
        "activite":   activite,
        "zones":      zones_out,
        "annonces":   annonces_out,
    })


# ---------------------------------------------------------------------------
# GET /api/runs
# ---------------------------------------------------------------------------

@app.route("/api/runs")
def runs():
    sb = _sb()
    rows = sb.table("runs").select("*").order("scraped_at", desc=True).limit(50).execute().data
    return jsonify([
        {
            "id":            r["id"],
            "cp":            r["code_postal"],
            "commune":       r.get("commune"),
            "date":          r["scraped_at"][:16].replace("T", " "),
            "source":        r.get("source"),
            "trouvees":      r.get("nb_annonces_trouvees", 0),
            "nouvelles":     r.get("nb_nouvelles", 0),
            "disparues":     r.get("nb_disparues", 0),
            "prix_modifies": r.get("nb_prix_modifies", 0),
            "duree":         r.get("duree_secondes"),
            "statut":        r.get("statut", "ok"),
        }
        for r in rows
    ])


# ---------------------------------------------------------------------------
# POST /api/run  — SSE streaming
# ---------------------------------------------------------------------------

@app.route("/api/run", methods=["POST"])
def run_pipeline():
    data    = request.json or {}
    cp      = (data.get("cp") or "").strip()
    commune = (data.get("commune") or "").strip()
    sl_code = (data.get("sl_code") or "").strip() or None
    lbc_loc = (data.get("lbc_loc") or "").strip() or None
    source  = data.get("source") or None

    if not cp or not commune:
        return jsonify({"error": "cp et commune requis"}), 400

    cmd = [sys.executable, "-u", "pipeline.py", "run", cp, commune]
    if sl_code:
        cmd += ["--sl-code", sl_code]
    if lbc_loc:
        cmd += ["--lbc-loc", lbc_loc]
    if source:
        cmd += ["--source", source]

    def generate():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(HERE),
        )

        def _feed_stdin():
            try:
                proc.stdin.write("o\no\no\no\n")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass

        threading.Thread(target=_feed_stdin, daemon=True).start()
        yield from _make_sse_generator(proc, cp)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# GET /api/run/<cp>/logs  — logs accumulés (pour reconnexion après reload)
# ---------------------------------------------------------------------------

@app.route("/api/run/<cp>/logs")
def run_logs_endpoint(cp):
    from_idx = max(0, int(request.args.get("from", 0)))
    if cp not in _run_logs:
        return jsonify({"lines": [], "total": 0, "done": None, "tracked": False})
    lines = _run_logs[cp]
    done  = _run_done.get(cp)
    return jsonify({
        "lines":   lines[from_idx:],
        "total":   len(lines),
        "done":    done,
        "tracked": True,
    })


# ---------------------------------------------------------------------------
# GET /api/incomplete  — zones avec pages manquantes ou runs en erreur
# ---------------------------------------------------------------------------

@app.route("/api/incomplete")
def incomplete():
    result = []
    seen_cps: set[str] = set()

    debug_dir = HERE / "debug"
    if debug_dir.exists():
        for sf in sorted(debug_dir.glob("scrape_state_*.json")):
            try:
                state   = json.loads(sf.read_text(encoding="utf-8"))
                cp      = state.get("cp") or ""
                commune = state.get("commune") or cp
                err_lbc = state.get("pages_erreur_lbc", [])
                err_sl  = state.get("pages_erreur_sl",  [])
                if cp and (err_lbc or err_sl):
                    result.append({
                        "cp":              cp,
                        "commune":         commune,
                        "pages_erreur_lbc": err_lbc,
                        "pages_erreur_sl":  err_sl,
                        "source":          "scrape_incomplet",
                    })
                    seen_cps.add(cp)
            except Exception:
                pass

    try:
        sb = _sb()
        # Dernier run par CP (tous statuts)
        all_runs = sb.table("runs").select(
            "code_postal, commune, scraped_at, statut, pages_erreur_lbc, pages_erreur_sl"
        ).order("scraped_at", desc=True).limit(100).execute().data

        # Pour chaque CP, garder le dernier run — s'il est en erreur/running, alerter
        latest_per_cp: dict[str, dict] = {}
        for r in all_runs:
            cp = r.get("code_postal")
            if cp and cp not in latest_per_cp:
                latest_per_cp[cp] = r

        for cp, r in latest_per_cp.items():
            if cp in seen_cps:
                continue
            statut    = r.get("statut")
            err_lbc   = r.get("pages_erreur_lbc") or []
            err_sl    = r.get("pages_erreur_sl")  or []
            if statut in ("error", "running"):
                result.append({
                    "cp":               cp,
                    "commune":          r.get("commune") or cp,
                    "pages_erreur_lbc": [],
                    "pages_erreur_sl":  [],
                    "statut":           statut,
                    "date":             r["scraped_at"][:16].replace("T", " "),
                    "source":           "run_erreur",
                })
                seen_cps.add(cp)
            elif err_lbc or err_sl:
                result.append({
                    "cp":               cp,
                    "commune":          r.get("commune") or cp,
                    "pages_erreur_lbc": err_lbc,
                    "pages_erreur_sl":  err_sl,
                    "statut":           statut,
                    "date":             r["scraped_at"][:16].replace("T", " "),
                    "source":           "run_incomplet",
                })
                seen_cps.add(cp)
    except Exception:
        pass

    return jsonify(result)


# ---------------------------------------------------------------------------
# GET /api/zone/<cp>/state?commune=  — retourne le scrape_state (URLs sauvegardées)
# ---------------------------------------------------------------------------

@app.route("/api/zone/<cp>/state")
def zone_state(cp):
    commune = (request.args.get("commune") or "").strip()
    debug_dir = HERE / "debug"
    slug = re.sub(r"[^a-z0-9]+", "_",
                  "".join(c for c in unicodedata.normalize("NFKD", commune.lower())
                          if not unicodedata.combining(c))).strip("_") if commune else ""

    sf = debug_dir / f"scrape_state_{cp}_{slug}.json" if slug else None
    if sf and sf.exists():
        state = json.loads(sf.read_text(encoding="utf-8"))
    elif debug_dir.exists():
        matches = [f for f in debug_dir.glob(f"scrape_state_{cp}_*.json")
                   if not commune or _norm(json.loads(f.read_text()).get("commune", "")) == _norm(commune)]
        state = json.loads(matches[0].read_text(encoding="utf-8")) if matches else {}
    else:
        state = {}

    # Extraire le sl_code depuis sl_base (ex: locations=AD08FR28766)
    sl_base = state.get("sl_base") or ""
    sl_code = ""
    m = re.search(r"locations=([A-Z0-9]+)", sl_base)
    if m:
        sl_code = m.group(1)

    lbc_base = state.get("lbc_base", "")

    # Pages scrapées : priorité au scrape_state (source de vérité), fallback stg_*
    lbc_scraped: list[int] = state.get("lbc_scraped") or []
    sl_scraped:  list[int] = state.get("sl_scraped")  or []
    last_run: dict | None = None
    try:
        sb = _sb()
        if not lbc_scraped:
            lbc_rows = sb.table("stg_lbc").select("page").eq("code_postal", cp).execute().data
            lbc_scraped = sorted(set(r["page"] for r in lbc_rows if r.get("page")))
        if not sl_scraped:
            sl_rows = sb.table("stg_seloger").select("page").eq("code_postal", cp).execute().data
            sl_scraped = sorted(set(r["page"] for r in sl_rows if r.get("page")))
        runs_data = sb.table("runs").select("scraped_at,statut,nb_annonces_trouvees") \
                      .eq("code_postal", cp).order("scraped_at", desc=True).limit(1).execute().data
        if runs_data:
            last_run = runs_data[0]
    except Exception:
        pass

    return jsonify({
        "sl_code":  sl_code,
        "lbc_base": lbc_base,
        "sl_base":  sl_base,
        "lbc_pages": state.get("lbc_pages", 0),
        "sl_pages":  state.get("sl_pages", 0),
        "pages_erreur_lbc": state.get("pages_erreur_lbc") or [],
        "pages_erreur_sl":  state.get("pages_erreur_sl") or [],
        "lbc_scraped": lbc_scraped,
        "sl_scraped":  sl_scraped,
        "last_run":    last_run,
    })


# ---------------------------------------------------------------------------
# POST /api/zone/<cp>/accept-incomplete  — supprime les erreurs de pages du state
# ---------------------------------------------------------------------------

@app.route("/api/zone/<cp>/accept-incomplete", methods=["POST"])
def accept_incomplete(cp):
    commune = (request.json or {}).get("commune", "")
    if not commune:
        return jsonify({"error": "commune manquante"}), 400

    debug_dir = HERE / "debug"
    slug = re.sub(r"[^a-z0-9]+", "_",
                  "".join(c for c in unicodedata.normalize("NFKD", commune.lower())
                          if not unicodedata.combining(c))).strip("_")
    sf = debug_dir / f"scrape_state_{cp}_{slug}.json"

    if not sf.exists():
        # Chercher parmi tous les state files du CP
        matches = [f for f in debug_dir.glob(f"scrape_state_{cp}_*.json")
                   if _norm(json.loads(f.read_text()).get("commune", "")) == _norm(commune)]
        if not matches:
            return jsonify({"ok": True, "msg": "Pas de state file trouvé — rien à nettoyer"})
        sf = matches[0]

    state = json.loads(sf.read_text(encoding="utf-8"))
    cleared_lbc = list(state.get("pages_erreur_lbc") or [])
    cleared_sl  = list(state.get("pages_erreur_sl")  or [])
    state["pages_erreur_lbc"] = []
    state["pages_erreur_sl"]  = []
    sf.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    return jsonify({"ok": True, "cleared_lbc": cleared_lbc, "cleared_sl": cleared_sl})


# ---------------------------------------------------------------------------
# POST /api/retry/<cp>?wait=8000  — SSE streaming du retry
# ---------------------------------------------------------------------------

@app.route("/api/retry/<cp>", methods=["POST"])
def retry_zone(cp):
    try:
        wait = int(request.args.get("wait", "8000"))
        wait = max(4000, min(20000, wait))
    except ValueError:
        wait = 8000
    body = request.json or {}
    commune   = body.get("commune", "").strip() or None
    lbc_pages = body.get("lbc_pages") or []
    sl_pages  = body.get("sl_pages")  or []

    cmd = [sys.executable, "-u", "pipeline.py", "retry", cp]
    if commune:
        cmd.append(commune)
    cmd += ["--wait", str(wait)]
    if lbc_pages:
        cmd += ["--lbc-pages", ",".join(str(p) for p in lbc_pages)]
    if sl_pages:
        cmd += ["--sl-pages", ",".join(str(p) for p in sl_pages)]

    def generate():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(HERE),
        )

        def _feed_stdin():
            try:
                proc.stdin.write("o\n")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass

        threading.Thread(target=_feed_stdin, daemon=True).start()
        yield from _make_sse_generator(proc, cp)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
