"""
api.py — API Flask pour le dashboard Ideeri
Usage : python3 api.py  → localhost:5000
"""

import json, os, re, sys, threading
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, Response, request, stream_with_context
from flask_cors import CORS
from supabase import create_client

load_dotenv()
app = Flask(__name__)
CORS(app)
HERE = Path(__file__).parent

_ANSI = re.compile(r"\033\[[0-9;]*m")


def _sb():
    return create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


# ---------------------------------------------------------------------------
# GET /api/zones
# ---------------------------------------------------------------------------

@app.route("/api/zones")
def zones():
    sb = _sb()
    runs_rows = sb.table("runs").select("code_postal, commune").execute().data

    # CP → commune + nb_runs
    cp_commune: dict[str, str] = {}
    cp_runs:    dict[str, int] = {}
    for r in runs_rows:
        cp = r.get("code_postal")
        if not cp:
            continue
        cp_commune.setdefault(cp, r.get("commune") or cp)
        cp_runs[cp] = cp_runs.get(cp, 0) + 1

    result = []
    for cp, commune in sorted(cp_commune.items()):
        nb = sb.table("annonces").select("id_annonce", count="exact") \
               .eq("code_postal", cp).eq("est_active", True).execute().count or 0
        result.append({
            "cp":         cp,
            "commune":    commune,
            "nb_annonces": nb,
            "nb_runs":    cp_runs.get(cp, 0),
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
    rows = sb.table("annonces").select("*").eq("code_postal", cp) \
             .eq("est_active", True).execute().data

    if not rows:
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

    # Entités : nb_mandats = biens distincts par entité
    ent_map: dict[str, dict] = {}
    for r in rows:
        nom = r.get("nom_commercial") or "Inconnu"
        if nom not in ent_map:
            ent_map[nom] = {"nb_lbc": 0, "nb_sl": 0, "biens": set()}
        if r["source"] == "lbc":
            ent_map[nom]["nb_lbc"] += 1
        else:
            ent_map[nom]["nb_sl"] += 1
        if r.get("bien_id"):
            ent_map[nom]["biens"].add(r["bien_id"])

    # Type des entités : un seul SELECT groupé
    noms = list(ent_map.keys())
    ent_type_map: dict[str, str] = {}
    if noms:
        try:
            et_rows = sb.table("entites").select("nom_commercial, type_entite") \
                        .in_("nom_commercial", noms[:200]).execute().data
            for et in et_rows:
                ent_type_map[et["nom_commercial"]] = et.get("type_entite") or "agence"
        except Exception:
            pass

    entites = sorted(
        [{
            "nom":        n,
            "nb_mandats": len(v["biens"]),   # nb_mandats = biens uniques de l'entité
            "nb_biens":   len(v["biens"]),   # alias pour compatibilité tri côté client
            "nb_lbc":     v["nb_lbc"],
            "nb_seloger": v["nb_sl"],
            "type":       ent_type_map.get(n, "agence"),
        }
         for n, v in ent_map.items()],
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

    # Dernier run
    runs_rows = sb.table("runs").select("*").eq("code_postal", cp) \
                  .order("scraped_at", desc=True).limit(5).execute().data
    last_run = runs_rows[0]["scraped_at"][:16].replace("T", " ") if runs_rows else None

    return jsonify({
        # ── Identité ──
        "cp":      cp,
        "commune": rows[0].get("commune") or cp,
        "last_run": last_run,

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
# GET /api/entite/<nom>
# ---------------------------------------------------------------------------

@app.route("/api/entite/<path:nom>")
def entite(nom):
    sb = _sb()
    rows = sb.table("annonces") \
             .select("code_postal, commune, source, bien_id, prix_affiche, surface, titre, dpe") \
             .eq("nom_commercial", nom).eq("est_active", True).execute().data

    # Agréger par CP
    cp_map: dict[str, dict] = {}
    for r in rows:
        cp = r["code_postal"]
        if cp not in cp_map:
            cp_map[cp] = {"commune": r.get("commune"), "nb_lbc": 0, "nb_sl": 0, "biens": set()}
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
            "nb_mandats":        len(v["biens"]),   # mandats de cette entité sur cette zone
            "nb_biens":          len(v["biens"]),
            "nb_lbc":            v["nb_lbc"],
            "nb_seloger":        v["nb_sl"],
            "total_mandats_zone": nb_mandats_zone,  # total tous opérateurs sur cette zone
        })
    zones_out.sort(key=lambda x: x["nb_mandats"], reverse=True)

    ent_rows = sb.table("entites").select("*").eq("nom_commercial", nom).execute().data
    ent_data = ent_rows[0] if ent_rows else {}

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
    source  = data.get("source") or None

    if not cp or not commune:
        return jsonify({"error": "cp et commune requis"}), 400

    cmd = [sys.executable, "pipeline.py", "run", cp, commune]
    if sl_code:
        cmd += ["--sl-code", sl_code]
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

        # Répond "o" aux deux confirmations interactives de pipeline.py
        def _feed_stdin():
            try:
                proc.stdin.write("o\no\n")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass

        threading.Thread(target=_feed_stdin, daemon=True).start()

        for line in proc.stdout:
            line = _strip_ansi(line.rstrip())
            if line:
                yield f"data: {json.dumps(line)}\n\n"

        proc.wait()
        yield f"data: {json.dumps('__DONE__')}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, port=5000, threaded=True)
