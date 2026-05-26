"""
transform.py — Transformateur stg_* → annonces + entites + runs
Usage : python3 transform.py <code_postal> [commune]

Lit les pages non traitées (processed=false) dans stg_lbc et stg_seloger
pour le code postal donné, normalise chaque annonce, gère le suivi temporel,
et alimente annonces + entites. Journalise chaque run dans la table runs.
"""

import os, re, sys, json, time, hashlib, unicodedata, lzstring
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPA_URL"], os.environ["SUPA_KEY"])

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "agence", "immobilier", "immobiliere", "reseau", "france", "cabinet",
    "groupe", "group", "solutions", "transaction", "habitat", "conseils",
    "conseil", "immo", "services", "service", "proprietes", "propriete",
}

def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )

def normalize_name(name: str) -> str:
    s = _strip_accents(name.lower())
    words = re.findall(r"[a-z0-9]+", s)
    return "_".join(w for w in words if w not in _STOPWORDS and len(w) > 1)

def entity_signature(siren: str | None, type_entite: str, nom: str, cp: str) -> str:
    if siren and re.fullmatch(r"\d{9}", siren):
        return f"siren_{siren}"
    if type_entite == "particulier":
        return f"particulier_{cp}"
    normed = normalize_name(nom)
    return f"nom_{normed}" if normed else f"nom_{hashlib.md5(nom.encode()).hexdigest()[:8]}"

def compute_match_score(a: dict, b: dict) -> float:
    """Score unique 0.0 (rejet) ou [0.50, 1.00].
    Utilisé par intra_entity_match (seuil ≥0.70) et cross_entity_match (seuil ≥0.80).

    Conditions bloquantes (→ 0.0) :
      - type_bien ou code_postal différents
      - écart surface > 5% ou prix > 10%
      - DPE renseigné sur les deux, différents et aucun n'est N
      - l'un est N et l'autre est dans A–G

    Bonus/malus sur données présentes uniquement (l'absence est neutre).
    Score max théorique : 1.00 (plafonné).
    """
    if a.get("code_postal") != b.get("code_postal"):
        return 0.0
    if a.get("type_bien") and b.get("type_bien") and a["type_bien"] != b["type_bien"]:
        return 0.0

    s1, s2 = a.get("surface"), b.get("surface")
    p1, p2 = a.get("prix_affiche"), b.get("prix_affiche")
    if not (s1 and s2 and float(s1) > 0 and float(s2) > 0
            and p1 and p2 and float(p1) > 0 and float(p2) > 0):
        return 0.0

    sr = abs(float(s1) - float(s2)) / max(float(s1), float(s2))
    pr = abs(float(p1) - float(p2)) / max(float(p1), float(p2))
    if sr > 0.05 or pr > 0.10:
        return 0.0

    dpe_a, dpe_b = a.get("dpe"), b.get("dpe")
    _STD = {"A", "B", "C", "D", "E", "F", "G"}
    if dpe_a and dpe_b and dpe_a != dpe_b:
        if dpe_a != "N" and dpe_b != "N":
            return 0.0  # deux DPE standards différents → pas le même bien
        if (dpe_a == "N" and dpe_b in _STD) or (dpe_b == "N" and dpe_a in _STD):
            return 0.0  # neuf vs ancien → pas comparable

    score = 0.50
    score += 0.20 * (1.0 - sr / 0.05)
    score += 0.20 * (1.0 - pr / 0.10)

    if dpe_a and dpe_b:
        score += 0.15 if dpe_a == dpe_b else -0.25

    ges_a, ges_b = a.get("ges"), b.get("ges")
    if ges_a and ges_b and ges_a == ges_b:
        score += 0.08

    np_a, np_b = a.get("nb_pieces"), b.get("nb_pieces")
    if np_a and np_b and int(np_a) == int(np_b):
        score += 0.10

    ya, yb = a.get("annee_construction"), b.get("annee_construction")
    if ya and yb and abs(int(ya) - int(yb)) <= 5:
        score += 0.05

    return max(min(score, 1.0), 0.0)


# ---------------------------------------------------------------------------
# Parsers raw → annonces normalisées
# ---------------------------------------------------------------------------

def _lbc_attr(ad, key):
    return next((a.get("value") for a in ad.get("attributes", []) if a.get("key") == key), None)

def parse_lbc_ads(data_brute: dict) -> list[dict]:
    ads = data_brute.get("ads", [])
    results = []
    for a in ads:
        owner = a.get("owner", {})
        siren = owner.get("siren") or None
        if siren:
            siren = re.sub(r"\D", "", siren)[:9] or None

        nom = owner.get("name", "")
        type_e = "particulier" if owner.get("type") == "private" else "agence"
        cp = a.get("location", {}).get("zipcode", "")
        prix = a.get("price", [None])[0] if a.get("price") else None
        surface_raw = _lbc_attr(a, "square")
        try:
            surface = float(str(surface_raw).replace(",", ".")) if surface_raw else None
        except ValueError:
            surface = None

        # Type de bien — real_estate_type attr en priorité (1=maison, 2=appartement)
        rt = _lbc_attr(a, "real_estate_type")
        if rt == "1":
            type_bien = "maison"
        elif rt == "2":
            type_bien = "appartement"
        else:
            subj = a.get("subject", "").lower()
            type_bien = "maison" if ("maison" in subj or "villa" in subj) else "appartement"

        dpe_raw = _lbc_attr(a, "energy_rate")
        if not dpe_raw and _lbc_attr(a, "immo_sell_type") == "new":
            dpe_raw = "N"
        ges_raw = _lbc_attr(a, "ges")

        def _lbc_int(ad, key):
            v = _lbc_attr(ad, key)
            try: return int(v) if v else None
            except (ValueError, TypeError): return None

        results.append({
            "id_annonce":          f"lbc_{a.get('list_id', '')}",
            "source":              "lbc",
            "type_bien":           type_bien,
            "titre":               a.get("subject", ""),
            "prix_affiche":        int(prix) if prix else None,
            "surface":             surface,
            "nb_pieces":           int(_lbc_attr(a, "rooms") or 0) or None,
            "code_postal":         cp,
            "commune":             a.get("location", {}).get("city", ""),
            "url_annonce":         a.get("url", ""),
            "date_publication":    a.get("first_publication_date", "")[:10] if a.get("first_publication_date") else None,
            "sur_lbc":             True,
            "sur_seloger":         False,
            "dpe":                 dpe_raw.upper() if dpe_raw else None,
            "ges":                 ges_raw.upper() if ges_raw else None,
            "energie_budget_min":  _lbc_int(a, "annual_energy_budget_min"),
            "energie_budget_max":  _lbc_int(a, "annual_energy_budget_max"),
            "annee_construction":  _lbc_int(a, "building_year"),
            "etage":               _lbc_int(a, "floor_number"),
            "_entity": {
                "nom":             nom,
                "siren":           siren,
                "type":            type_e,
                "store_id":        owner.get("store_id"),
                "cp":              cp,
            },
        })
    return results


def _parse_siret_from_legal(info_list: list) -> tuple[str | None, str | None]:
    """Extrait (siret, siren) depuis agencyLegalInformations SeLoger."""
    siret = siren = None
    for item in info_list:
        m = re.search(r"SIRET[^0-9]*(\d{14})", item)
        if m:
            siret = m.group(1)
            siren = siret[:9]
            break
        m = re.search(r"RCS[^0-9]*(\d{9})", item)
        if m and not siren:
            siren = m.group(1)
    return siret, siren


def parse_seloger_ads(data_brute: dict) -> list[dict]:
    ids = data_brute.get("classifieds_ids", [])
    cd  = data_brute.get("classifieds_data", {})
    results = []
    for cid in ids:
        item = cd.get(cid)
        if not isinstance(item, dict):
            continue
        hf    = item.get("hardFacts", {})
        loc   = item.get("location", {}).get("address", {})
        facts = {f["type"]: f.get("splitValue") for f in hf.get("facts", [])}
        prov  = item.get("provider", {})
        meta  = item.get("metadata", {})
        lt    = item.get("legacyTracking", {})

        price_raw = hf.get("price", {}).get("ariaLabel", "")
        prix = int(re.sub(r"[^\d]", "", price_raw)) if price_raw else None

        # Surface : hardFacts en priorité (peut avoir décimale), legacyTracking.space en fallback
        surface_raw = facts.get("livingSpace")
        try:
            surface = float(str(surface_raw).replace(",", ".")) if surface_raw else None
        except ValueError:
            surface = None
        if surface is None and lt.get("space"):
            try:
                surface = float(lt["space"])
            except (ValueError, TypeError):
                pass

        cp    = loc.get("zipCode", "")
        ville = loc.get("city", "")
        lid   = meta.get("legacyId", cid)

        # Type de bien : legacyTracking.estate_type (1=appartement, 2=maison) > titre
        et = lt.get("estate_type")
        if et == 1:
            type_bien = "appartement"
        elif et == 2:
            type_bien = "maison"
        else:
            title_l = hf.get("title", "").lower()
            type_bien = "maison" if "maison" in title_l else "appartement"

        # Nom commercial : intermediaryCard (agence) > cardProvider (agent individuel) > address
        nom_agence = (prov.get("intermediaryCard") or {}).get("title", "")
        if not nom_agence:
            nom_agence = (item.get("cardProvider") or {}).get("title", "")
        if not nom_agence:
            nom_agence = prov.get("address", "")

        siret, siren = _parse_siret_from_legal(prov.get("agencyLegalInformations") or [])
        pt = prov.get("publisherType", "")
        type_e = "particulier" if prov.get("isPrivateOwner") else (
            "promoteur" if pt == "DEVELOPER" else "agence"
        )

        # nb_pieces : hardFacts en priorité, legacyTracking.nb_rooms en fallback
        nb_pieces_raw = facts.get("numberOfRooms")
        try:
            nb_pieces = int(nb_pieces_raw) if nb_pieces_raw else None
        except ValueError:
            nb_pieces = None
        if nb_pieces is None and lt.get("nb_rooms"):
            try:
                nb_pieces = int(lt["nb_rooms"])
            except (ValueError, TypeError):
                pass

        results.append({
            "id_annonce":       f"seloger_{lid}",
            "source":           "seloger",
            "type_bien":        type_bien,
            "titre":            hf.get("title", ""),
            "prix_affiche":     prix,
            "surface":          surface,
            "nb_pieces":        nb_pieces,
            "code_postal":      cp,
            "commune":          ville,
            "url_annonce":      f"https://www.seloger.com/annonces/achat/{ville.lower().replace(' ','-')}/{lid}.htm",
            "date_publication": meta.get("creationDate", "")[:10] if meta.get("creationDate") else None,
            "sur_lbc":          False,
            "sur_seloger":      True,
            "dpe":              item.get("energyClass"),  # 'A'–'G' ou None
            "_entity": {
                "nom":              nom_agence,
                "siren":            siren,
                "siret":            siret,
                "type":             type_e,
                "profile_url":      prov.get("profileUrl"),
                "cp":               cp,
            },
        })
    return results


# ---------------------------------------------------------------------------
# Upsert entité → retourne l'id UUID
# ---------------------------------------------------------------------------

def upsert_entity(e: dict, scraped_at: str) -> str | None:
    nom  = e.get("nom", "") or ""
    siren = e.get("siren")
    siret = e.get("siret")
    cp    = e.get("cp", "")
    type_e = e.get("type", "agence")
    source = e.get("source", "")  # 'lbc' ou 'seloger'

    if not nom:
        return None

    sig = entity_signature(siren, type_e, nom, cp)

    # Chercher entité existante
    existing = sb.table("entites").select("*").eq("signature", sig).execute().data
    if existing:
        row = existing[0]
        # Mise à jour des noms portails
        updates: dict = {}
        if source == "lbc" and not row.get("nom_lbc"):
            updates["nom_lbc"]    = nom
            updates["sur_lbc"]    = True
            if e.get("store_id"):
                updates["lbc_store_id"] = e["store_id"]
        if source == "seloger" and not row.get("nom_seloger"):
            updates["nom_seloger"]           = nom
            updates["sur_seloger"]           = True
            if e.get("profile_url"):
                updates["seloger_profile_url"] = e["profile_url"]
        # Enrichissement SIRET
        if siret and not row.get("siret"):
            updates["siret"] = siret
        if siren and not row.get("siren"):
            updates["siren"] = siren
        # CP coverage
        cps = list(set((row.get("codes_postaux") or []) + ([cp] if cp else [])))
        if cps != row.get("codes_postaux"):
            updates["codes_postaux"] = cps
        if updates:
            sb.table("entites").update(updates).eq("id", row["id"]).execute()
        return row["id"]

    # Nouvelle entité
    new_row = {
        "signature":         sig,
        "nom_commercial":    nom,
        "type_entite":       type_e,
        "siren":             siren,
        "siret":             siret,
        "sur_lbc":           source == "lbc",
        "sur_seloger":       source == "seloger",
        "lbc_store_id":      e.get("store_id") if source == "lbc" else None,
        "seloger_profile_url": e.get("profile_url") if source == "seloger" else None,
        "codes_postaux":     [cp] if cp else [],
        "date_premiere_obs": scraped_at,
        "nb_annonces_actives": 0,
    }
    if source == "lbc":
        new_row["nom_lbc"] = nom
    else:
        new_row["nom_seloger"] = nom

    res = sb.table("entites").insert(new_row).execute()
    return res.data[0]["id"] if res.data else None


# ---------------------------------------------------------------------------
# Upsert annonce
# ---------------------------------------------------------------------------

def upsert_annonce(ann: dict, entite_id: str | None, run_id: int, scraped_at: str) -> str:
    """Retourne 'new', 'price_change' ou 'unchanged'."""
    id_ann = ann["id_annonce"]

    # Prix au m²
    prix_m2 = None
    if ann.get("prix_affiche") and ann.get("surface") and ann["surface"] > 0:
        prix_m2 = round(ann["prix_affiche"] / ann["surface"], 2)

    existing = sb.table("annonces").select(
        "id_annonce, prix_affiche, historique_prix, date_premiere_obs, run_id_premiere_obs, "
        "dpe, ges, energie_budget_min, energie_budget_max, annee_construction, etage"
    ).eq("id_annonce", id_ann).execute().data

    now = scraped_at

    if not existing:
        sb.table("annonces").insert({
            "id_annonce":          id_ann,
            "source":              ann["source"],
            "type_bien":           ann.get("type_bien"),
            "titre":               ann.get("titre"),
            "prix_affiche":        ann.get("prix_affiche"),
            "surface":             ann.get("surface"),
            "nb_pieces":           ann.get("nb_pieces"),
            "code_postal":         ann.get("code_postal"),
            "commune":             ann.get("commune"),
            "quartier_calcule":    ann.get("quartier"),
            "url_annonce":         ann.get("url_annonce"),
            "date_publication":    ann.get("date_publication"),
            "date_premiere_obs":   now,
            "date_derniere_obs":   now,
            "est_active":          True,
            "prix_m2_affiche":     prix_m2,
            "bien_id":             id_ann,
            "sur_lbc":             ann.get("sur_lbc", False),
            "sur_seloger":         ann.get("sur_seloger", False),
            "historique_prix":     [],
            "run_id_premiere_obs": run_id,
            "entite_id":           entite_id,
            "dpe":                 ann.get("dpe"),
            "ges":                 ann.get("ges"),
            "energie_budget_min":  ann.get("energie_budget_min"),
            "energie_budget_max":  ann.get("energie_budget_max"),
            "annee_construction":  ann.get("annee_construction"),
            "etage":               ann.get("etage"),
            "nom_commercial":      ann["_entity"].get("nom"),
            "signature_entite_bien": entity_signature(
                ann["_entity"].get("siren"), ann["_entity"].get("type", "agence"),
                ann["_entity"].get("nom", ""), ann.get("code_postal", ""),
            ),
        }).execute()
        return "new"

    row = existing[0]
    ancien_prix = row.get("prix_affiche")
    updates: dict = {
        "date_derniere_obs": now,
        "est_active":        True,
    }

    # type_bien : corrigible si le parseur s'améliore (villa/pavillon non détectés par titre)
    if ann.get("type_bien") and ann["type_bien"] != row.get("type_bien"):
        updates["type_bien"] = ann["type_bien"]

    if ann.get("sur_lbc"):
        updates["sur_lbc"] = True
    if ann.get("sur_seloger"):
        updates["sur_seloger"] = True

    if entite_id and not row.get("entite_id"):
        updates["entite_id"] = entite_id
    if ann.get("dpe") and not row.get("dpe"):
        updates["dpe"] = ann["dpe"]
    if ann.get("ges") and not row.get("ges"):
        updates["ges"] = ann["ges"]
    for field in ("energie_budget_min", "energie_budget_max", "annee_construction", "etage"):
        if ann.get(field) and not row.get(field):
            updates[field] = ann[field]

    status = "unchanged"
    if ancien_prix and ann.get("prix_affiche") and ann["prix_affiche"] != ancien_prix:
        hist = row.get("historique_prix") or []
        hist.append({"date": now, "prix": ann["prix_affiche"], "ancien_prix": ancien_prix})
        updates["prix_affiche"]    = ann["prix_affiche"]
        updates["prix_m2_affiche"] = prix_m2
        updates["historique_prix"] = hist
        status = "price_change"

    sb.table("annonces").update(updates).eq("id_annonce", id_ann).execute()
    return status


# ---------------------------------------------------------------------------
# Marquer les annonces disparues
# ---------------------------------------------------------------------------

def mark_inactive(code_postal: str, source: str, seen_ids: set[str], scraped_at: str) -> int:
    active = sb.table("annonces").select("id_annonce").eq("code_postal", code_postal).eq("source", source).eq("est_active", True).execute().data
    disparus = [r["id_annonce"] for r in active if r["id_annonce"] not in seen_ids]
    if disparus:
        sb.table("annonces").update({
            "est_active":        False,
            "date_derniere_obs": scraped_at,
        }).in_("id_annonce", disparus).execute()
    return len(disparus)


# ---------------------------------------------------------------------------
# Mise à jour snapshot entités
# ---------------------------------------------------------------------------

def update_entity_snapshots(code_postal: str, scraped_at: str) -> None:
    actives = sb.table("annonces").select(
        "entite_id, sur_lbc, sur_seloger"
    ).eq("code_postal", code_postal).eq("est_active", True).not_.is_("entite_id", "null").execute().data

    counts: dict[str, dict] = {}
    for row in actives:
        eid = row["entite_id"]
        if eid not in counts:
            counts[eid] = {"lbc": 0, "seloger": 0}
        if row.get("sur_lbc"):
            counts[eid]["lbc"] += 1
        if row.get("sur_seloger"):
            counts[eid]["seloger"] += 1

    date_snap = scraped_at[:10]
    for eid, c in counts.items():
        nb_total = c["lbc"] + c["seloger"]
        entite = sb.table("entites").select("historique_activite, nb_annonces_actives").eq("id", eid).execute().data
        if not entite:
            continue
        hist = entite[0].get("historique_activite") or []
        # Remplacer snapshot du même jour s'il existe
        hist = [h for h in hist if h.get("date") != date_snap]
        hist.append({"date": date_snap, "nb_lbc": c["lbc"], "nb_seloger": c["seloger"], "nb_total": nb_total})
        sb.table("entites").update({
            "nb_annonces_actives": nb_total,
            "historique_activite": hist,
        }).eq("id", eid).execute()


# ---------------------------------------------------------------------------
# Matching — deux appels sur le même score unique
# ---------------------------------------------------------------------------

def intra_entity_match(code_postal: str) -> int:
    """Apparie LBC↔SeLoger de la même entité. Seuil 0.70.
    Action : sur_lbc/sur_seloger=True, bien_id = id_annonce LBC, match_confidence.
    Retourne le nombre de paires matchées."""
    from collections import defaultdict
    fields = (
        "id_annonce, source, type_bien, surface, prix_affiche, nb_pieces, "
        "dpe, ges, annee_construction, entite_id, bien_id"
    )
    lbc = (sb.table("annonces").select(fields)
           .eq("code_postal", code_postal).eq("source", "lbc").eq("est_active", True)
           .not_.is_("entite_id", "null").execute().data)
    sl  = (sb.table("annonces").select(fields)
           .eq("code_postal", code_postal).eq("source", "seloger").eq("est_active", True)
           .not_.is_("entite_id", "null").execute().data)
    if not lbc or not sl:
        return 0

    lbc_by_ent: dict = defaultdict(list)
    sl_by_ent:  dict = defaultdict(list)
    for a in lbc: lbc_by_ent[a["entite_id"]].append(a)
    for a in sl:  sl_by_ent[a["entite_id"]].append(a)

    candidates = []
    for eid in set(lbc_by_ent) & set(sl_by_ent):
        for a in lbc_by_ent[eid]:
            for b in sl_by_ent[eid]:
                s = compute_match_score(a, b)
                if s >= 0.70:
                    candidates.append((s, a, b))
    candidates.sort(key=lambda x: -x[0])

    matched_lbc: set[str] = set()
    matched_sl:  set[str] = set()
    n = 0
    for score, a, b in candidates:
        if a["id_annonce"] in matched_lbc or b["id_annonce"] in matched_sl:
            continue
        lbc_id = a["id_annonce"]
        conf = round(score, 3)
        sb.table("annonces").update({
            "sur_seloger": True, "match_confidence": conf, "bien_id": lbc_id,
        }).eq("id_annonce", lbc_id).execute()
        sb.table("annonces").update({
            "sur_lbc": True, "match_confidence": conf, "bien_id": lbc_id,
        }).eq("id_annonce", b["id_annonce"]).execute()
        matched_lbc.add(lbc_id)
        matched_sl.add(b["id_annonce"])
        n += 1
    return n


def cross_entity_match(code_postal: str) -> dict:
    """Regroupe les biens identiques publiés par entités différentes. Seuil 0.80.
    Union-Find. Action : cluster_bien_id + bien_id partagés sur tous les membres.
    Retourne {n_clusters, n_annonces}."""
    import uuid
    fields = (
        "id_annonce, source, type_bien, surface, prix_affiche, nb_pieces, "
        "dpe, ges, annee_construction, entite_id, cluster_bien_id"
    )
    ads = (sb.table("annonces").select(fields)
           .eq("code_postal", code_postal).eq("est_active", True)
           .not_.is_("entite_id", "null").execute().data)
    if len(ads) < 2:
        return {"n_clusters": 0, "n_annonces": 0}

    _STD_DPE = {"A", "B", "C", "D", "E", "F", "G"}

    def _dpe_cat(dpe):
        if dpe == "N":   return "N"
        if dpe in _STD_DPE: return "std"
        return "none"

    parent   = {a["id_annonce"]: a["id_annonce"] for a in ads}
    root_dpe = {a["id_annonce"]: _dpe_cat(a.get("dpe")) for a in ads}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        dx, dy = root_dpe[rx], root_dpe[ry]
        # Ne jamais fusionner un cluster VEFA (N) avec un cluster existant (std)
        if (dx == "N" and dy == "std") or (dx == "std" and dy == "N"):
            return
        parent[rx] = ry
        # Propager la catégorie DPE la plus précise au nouveau root
        if dx == "N" or dy == "N":
            root_dpe[ry] = "N"
        elif dx == "std" or dy == "std":
            root_dpe[ry] = "std"

    for i, a in enumerate(ads):
        for b in ads[i + 1:]:
            if a.get("entite_id") == b.get("entite_id"):
                continue
            if compute_match_score(a, b) >= 0.80:
                union(a["id_annonce"], b["id_annonce"])

    ad_map = {a["id_annonce"]: a for a in ads}
    roots: dict[str, list[str]] = {}
    for a in ads:
        roots.setdefault(find(a["id_annonce"]), []).append(a["id_annonce"])

    n_clusters = n_touched = 0
    for members in roots.values():
        if len(members) < 2:
            continue
        existing = {ad_map[m].get("cluster_bien_id") for m in members} - {None}
        cluster_id = next(iter(existing), None) or uuid.uuid4().hex[:16]

        scores = []
        mlist = list(members)
        for i2, ma in enumerate(mlist):
            for mb in mlist[i2 + 1:]:
                s = compute_match_score(ad_map[ma], ad_map[mb])
                if s > 0:
                    scores.append(s)
        avg_conf = round(sum(scores) / len(scores), 3) if scores else 0.80

        for mid in members:
            if ad_map[mid].get("cluster_bien_id") != cluster_id:
                sb.table("annonces").update({
                    "cluster_bien_id":    cluster_id,
                    "cluster_confidence": avg_conf,
                    "bien_id":            cluster_id,
                }).eq("id_annonce", mid).execute()

        n_clusters += 1
        n_touched  += len(members)

    return {"n_clusters": n_clusters, "n_annonces": n_touched}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_transform(code_postal: str, commune: str = "") -> None:
    t0 = time.time()
    scraped_at = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Transform {code_postal} {commune} ===")

    # Créer le run
    run_res = sb.table("runs").insert({
        "scraped_at":  scraped_at,
        "code_postal": code_postal,
        "commune":     commune or code_postal,
        "source":      "all",
        "statut":      "ok",
    }).execute()
    run_id = run_res.data[0]["id"]
    print(f"  run_id = {run_id}")

    stats = {"trouvees": 0, "nouvelles": 0, "disparues": 0, "prix_modifies": 0}

    for source, table, parser in [
        ("lbc",     "stg_lbc",     parse_lbc_ads),
        ("seloger", "stg_seloger", parse_seloger_ads),
    ]:
        print(f"\n  [{source.upper()}]")
        rows = sb.table(table).select("*").eq("data_brute->_meta->>code_postal", code_postal).execute().data
        if not rows:
            # Fallback: essayer sans filtre sur code_postal (données sans migration)
            rows = sb.table(table).select("*").execute().data

        seen_ids: set[str] = set()
        for row in rows:
            raw = row.get("data_brute", {})
            ads = parser(raw)
            print(f"    page {raw.get('_meta',{}).get('page','?')} → {len(ads)} annonces")

            for ann in ads:
                ann["_entity"]["source"] = source
                entite_id = upsert_entity(ann["_entity"], scraped_at)
                status = upsert_annonce(ann, entite_id, run_id, scraped_at)
                seen_ids.add(ann["id_annonce"])
                stats["trouvees"] += 1
                if status == "new":
                    stats["nouvelles"] += 1
                elif status == "price_change":
                    stats["prix_modifies"] += 1

        n_disparus = mark_inactive(code_postal, source, seen_ids, scraped_at)
        stats["disparues"] += n_disparus
        if n_disparus:
            print(f"    {n_disparus} annonces marquées inactives")

    n_intra = intra_entity_match(code_postal)
    if n_intra:
        print(f"\n  {n_intra} paires intra-entité matchées (LBC↔SeLoger, seuil 0.70)")

    cl = cross_entity_match(code_postal)
    if cl["n_annonces"]:
        print(f"  {cl['n_clusters']} clusters multi-entités ({cl['n_annonces']} annonces, seuil 0.80)")

    update_entity_snapshots(code_postal, scraped_at)

    duree = int(time.time() - t0)
    sb.table("runs").update({
        "nb_annonces_trouvees": stats["trouvees"],
        "nb_nouvelles":         stats["nouvelles"],
        "nb_disparues":         stats["disparues"],
        "nb_prix_modifies":     stats["prix_modifies"],
        "duree_secondes":       duree,
    }).eq("id", run_id).execute()

    print(f"\n  Résultat run #{run_id} ({duree}s) :")
    print(f"    trouvées     : {stats['trouvees']}")
    print(f"    nouvelles    : {stats['nouvelles']}")
    print(f"    disparues    : {stats['disparues']}")
    print(f"    prix modifiés: {stats['prix_modifies']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 transform.py <code_postal> [commune]")
        sys.exit(1)
    cp      = sys.argv[1]
    commune = sys.argv[2] if len(sys.argv) > 2 else ""
    run_transform(cp, commune)
