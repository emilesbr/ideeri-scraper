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

def id_unique_bien(type_bien: str, surface, code_postal: str, prix) -> str:
    s = round(float(surface or 0) / 5) * 5
    p = round(float(prix or 0) / 10_000) * 10_000
    key = f"{type_bien}|{s}|{code_postal}|{p}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def compute_match_score(a: dict, b: dict) -> float:
    """Score inter-portail 0.0 (pas de match) à 1.0 (match parfait).
    Conditions obligatoires : CP, type_bien, surface ≤5%, prix ≤10%, entité identique."""
    if a.get("code_postal") != b.get("code_postal"):
        return 0.0
    if a.get("type_bien") and b.get("type_bien") and a["type_bien"] != b["type_bien"]:
        return 0.0

    s1, s2 = a.get("surface"), b.get("surface")
    p1, p2 = a.get("prix_affiche"), b.get("prix_affiche")
    if not (s1 and s2 and s1 > 0 and s2 > 0 and p1 and p2 and p1 > 0 and p2 > 0):
        return 0.0

    sr = abs(s1 - s2) / max(s1, s2)
    pr = abs(p1 - p2) / max(p1, p2)
    if sr > 0.05 or pr > 0.10:
        return 0.0

    entity_exact = bool(
        a.get("entite_id") and b.get("entite_id") and a["entite_id"] == b["entite_id"]
    )
    if not entity_exact:
        na = normalize_name(a.get("nom_commercial") or "")
        nb = normalize_name(b.get("nom_commercial") or "")
        if not (na and nb and na == nb):
            return 0.0  # aucune correspondance d'entité → pas de match

    score = 0.50
    score += 0.15 * (1 - sr / 0.05)
    score += 0.15 * (1 - pr / 0.10)
    if entity_exact:
        score += 0.10
    if a.get("dpe") and b.get("dpe") and a["dpe"] == b["dpe"]:
        score += 0.10
    return min(score, 1.0)


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

        # Type de bien
        cat = a.get("category", {}).get("name", "").lower()
        type_bien = "maison" if "maison" in cat else "appartement"

        dpe_raw = _lbc_attr(a, "energy_rate")
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

        price_raw = hf.get("price", {}).get("ariaLabel", "")
        prix = int(re.sub(r"[^\d]", "", price_raw)) if price_raw else None

        surface_raw = facts.get("livingSpace")
        try:
            surface = float(str(surface_raw).replace(",", ".")) if surface_raw else None
        except ValueError:
            surface = None

        cp    = loc.get("zipCode", "")
        ville = loc.get("city", "")
        lid   = meta.get("legacyId", cid)

        title = hf.get("title", "").lower()
        type_bien = "maison" if "maison" in title else "appartement"

        nom_agence = (prov.get("intermediaryCard") or {}).get("title", "")
        siret, siren = _parse_siret_from_legal(prov.get("agencyLegalInformations") or [])
        pt = prov.get("publisherType", "")
        type_e = "particulier" if prov.get("isPrivateOwner") else (
            "promoteur" if pt == "DEVELOPER" else "agence"
        )

        nb_pieces_raw = facts.get("numberOfRooms")
        try:
            nb_pieces = int(nb_pieces_raw) if nb_pieces_raw else None
        except ValueError:
            nb_pieces = None

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

    id_bien = id_unique_bien(
        ann.get("type_bien", ""), ann.get("surface"),
        ann.get("code_postal", ""), ann.get("prix_affiche"),
    )

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
            "id_unique_bien":      id_bien,
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
# Matching inter-portail (passe 2 — fuzzy)
# ---------------------------------------------------------------------------

def compute_multi_entity_score(a: dict, b: dict) -> float:
    """Score pour biens identiques publiés par entités différentes.
    Plus strict que compute_match_score (surface ±3%, prix ±5%, DPE obligatoirement cohérent).
    Retourne 0.0 si conditions non remplies ou DPE/GES contradictoires."""
    if a.get("code_postal") != b.get("code_postal"):
        return 0.0
    if a.get("type_bien") and b.get("type_bien") and a["type_bien"] != b["type_bien"]:
        return 0.0

    s1, s2 = a.get("surface"), b.get("surface")
    p1, p2 = a.get("prix_affiche"), b.get("prix_affiche")
    if not (s1 and s2 and s1 > 0 and s2 > 0 and p1 and p2 and p1 > 0 and p2 > 0):
        return 0.0

    sr = abs(float(s1) - float(s2)) / max(float(s1), float(s2))
    pr = abs(float(p1) - float(p2)) / max(float(p1), float(p2))
    if sr > 0.03 or pr > 0.05:
        return 0.0

    # DPE / GES : si les deux sont renseignés et différents → pas le même bien
    if a.get("dpe") and b.get("dpe") and a["dpe"] != b["dpe"]:
        return 0.0
    if a.get("ges") and b.get("ges") and a["ges"] != b["ges"]:
        return 0.0

    score = 0.40
    score += 0.15 * (1 - sr / 0.03)
    score += 0.15 * (1 - pr / 0.05)

    # DPE identique = fort signal
    if a.get("dpe") and b.get("dpe") and a["dpe"] == b["dpe"]:
        score += 0.20
    # GES identique = signal supplémentaire
    if a.get("ges") and b.get("ges") and a["ges"] == b["ges"]:
        score += 0.10
    # Nombre de pièces cohérent
    if a.get("nb_pieces") and b.get("nb_pieces"):
        if a["nb_pieces"] == b["nb_pieces"]:
            score += 0.10
        else:
            score -= 0.15  # pénalité — même bien doit avoir le même nb de pièces
    # Année de construction cohérente (±5 ans)
    ya, yb = a.get("annee_construction"), b.get("annee_construction")
    if ya and yb:
        if abs(int(ya) - int(yb)) <= 5:
            score += 0.05
        else:
            score -= 0.10

    return max(min(score, 1.0), 0.0)


def cluster_multi_entity(code_postal: str) -> dict:
    """
    Regroupe les annonces représentant le même bien physique, même quand publiées
    par des entités différentes. Utilise Union-Find pour la transitivité.

    Retourne un dict avec :
    - n_clusters_multi : clusters avec vraiment plusieurs entités différentes
    - n_clusters_same  : clusters mono-entité (détection de doublons internes)
    - n_annonces       : annonces touchées
    """
    import uuid

    fields = (
        "id_annonce, source, type_bien, surface, prix_affiche, nb_pieces, "
        "dpe, ges, annee_construction, entite_id, nom_commercial, cluster_bien_id"
    )
    ads = (
        sb.table("annonces").select(fields)
        .eq("code_postal", code_postal).eq("est_active", True)
        .execute().data
    )
    if len(ads) < 2:
        return {"n_clusters_multi": 0, "n_clusters_same": 0, "n_annonces": 0}

    # Union-Find
    parent = {a["id_annonce"]: a["id_annonce"] for a in ads}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    # Construire toutes les paires O(n²)
    for i, a in enumerate(ads):
        for b in ads[i + 1:]:
            if compute_multi_entity_score(a, b) >= 0.65:
                union(a["id_annonce"], b["id_annonce"])

    # Regrouper par root, garder seulement les clusters ≥ 2 membres
    ad_map = {a["id_annonce"]: a for a in ads}
    roots: dict[str, list[str]] = {}
    for a in ads:
        r = find(a["id_annonce"])
        roots.setdefault(r, []).append(a["id_annonce"])

    n_multi = n_same = n_touched = 0
    for root, members in roots.items():
        if len(members) < 2:
            continue

        # Identifier si plusieurs entités réellement différentes
        entity_ids  = {ad_map[m]["entite_id"] for m in members if ad_map[m].get("entite_id")}
        entity_norms = {normalize_name(ad_map[m].get("nom_commercial") or "") for m in members}
        entity_norms.discard("")
        multi_entity = len(entity_ids) > 1 or len(entity_norms) > 1

        # Réutiliser un cluster_bien_id existant ou en créer un
        existing = {ad_map[m].get("cluster_bien_id") for m in members}
        existing.discard(None)
        cluster_id = next(iter(existing), None) or uuid.uuid4().hex[:16]

        # Score moyen du cluster (calcul approximatif sur les paires)
        scores = []
        mlist = list(members)
        for i2, mid_a in enumerate(mlist):
            for mid_b in mlist[i2 + 1:]:
                s = compute_multi_entity_score(ad_map[mid_a], ad_map[mid_b])
                if s > 0:
                    scores.append(s)
        avg_conf = round(sum(scores) / len(scores), 3) if scores else 0.65

        for mid in members:
            if ad_map[mid].get("cluster_bien_id") != cluster_id:
                sb.table("annonces").update({
                    "cluster_bien_id":   cluster_id,
                    "cluster_confidence": avg_conf,
                }).eq("id_annonce", mid).execute()

        if multi_entity:
            n_multi += 1
        else:
            n_same += 1
        n_touched += len(members)

    return {"n_clusters_multi": n_multi, "n_clusters_same": n_same, "n_annonces": n_touched}

def fuzzy_match_cross_portal(code_postal: str, scraped_at: str) -> int:
    """
    Associe les annonces LBC et SeLoger représentant le même bien.
    Pour chaque paire matchée : met sur_seloger/sur_lbc=True, aligne id_unique_bien,
    enregistre match_confidence. Retourne le nombre de nouveaux matches.
    """
    fields = "id_annonce, source, code_postal, type_bien, surface, prix_affiche, entite_id, nom_commercial, dpe, id_unique_bien, match_confidence"
    lbc = sb.table("annonces").select(fields).eq("code_postal", code_postal).eq("source", "lbc").eq("est_active", True).execute().data
    sl  = sb.table("annonces").select(fields).eq("code_postal", code_postal).eq("source", "seloger").eq("est_active", True).execute().data

    if not lbc or not sl:
        return 0

    # Construire toutes les paires candidates avec score
    candidates: list[tuple[float, str, str, dict, dict]] = []
    for a in lbc:
        for b in sl:
            score = compute_match_score(a, b)
            if score > 0:
                candidates.append((score, a["id_annonce"], b["id_annonce"], a, b))
    candidates.sort(key=lambda x: -x[0])

    matched_lbc: set[str] = set()
    matched_sl:  set[str] = set()
    n_matches = 0

    for score, lbc_id, sl_id, a, b in candidates:
        if lbc_id in matched_lbc or sl_id in matched_sl:
            continue
        shared_id = a.get("id_unique_bien") or b.get("id_unique_bien")
        conf = round(score, 3)
        sb.table("annonces").update({
            "sur_seloger":      True,
            "match_confidence": conf,
            "id_unique_bien":   shared_id,
        }).eq("id_annonce", lbc_id).execute()
        sb.table("annonces").update({
            "sur_lbc":          True,
            "match_confidence": conf,
            "id_unique_bien":   shared_id,
        }).eq("id_annonce", sl_id).execute()
        matched_lbc.add(lbc_id)
        matched_sl.add(sl_id)
        n_matches += 1

    return n_matches


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

    n_matches = fuzzy_match_cross_portal(code_postal, scraped_at)
    if n_matches:
        print(f"\n  {n_matches} biens matchés inter-portails (fuzzy)")

    cl = cluster_multi_entity(code_postal)
    if cl["n_annonces"]:
        print(f"  clustering multi-entités : {cl['n_clusters_multi']} clusters multi-mandats, "
              f"{cl['n_clusters_same']} doublons internes ({cl['n_annonces']} annonces)")

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
