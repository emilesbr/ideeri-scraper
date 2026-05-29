"""
run_update.py — Mise à jour complète de toutes les communes en base.
URLs extraites directement des scrape_state pour garantir la cohérence.
"""
import sys, os, json, re
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()

from pipeline import cmd_run

DEBUG = os.path.join(os.path.dirname(__file__), "debug")

def _extract_params(state_file):
    """Extrait sl_code et lbc_loc depuis un fichier scrape_state."""
    path = os.path.join(DEBUG, state_file)
    if not os.path.exists(path):
        return None, None
    d = json.load(open(path))
    sl_base  = d.get("sl_base")
    lbc_base = d.get("lbc_base")
    # sl_code = valeur du paramètre locations= dans l'URL SeLoger
    sl_code = None
    if sl_base:
        m = re.search(r"locations=([^&]+)", sl_base)
        if m:
            sl_code = m.group(1)
    # lbc_loc = valeur du paramètre locations= dans l'URL LBC
    lbc_loc = None
    if lbc_base:
        m = re.search(r"locations=([^&]+)", lbc_base)
        if m:
            import urllib.parse
            lbc_loc = urllib.parse.unquote(m.group(1))
    return sl_code, lbc_loc


# Overrides manuels : sl_code confirmés directement depuis les URLs SeLoger actives.
# Priorité sur les scrape_state qui peuvent contenir des codes obsolètes.
SL_CODE_OVERRIDES = {
    "69001": "POCOFR4445",   # confirmé le 29/05/2026 — AD09FR17 (scrape_state) était obsolète
    "69002": "POCOFR4446",   # confirmé le 29/05/2026
}

LBC_LOC_OVERRIDES = {
    "69001": "Lyon_69001__45.76852_4.83187_1445",  # confirmé le 29/05/2026
    "69002": "Lyon_69002__45.75917_4.82965_3494",  # confirmé le 29/05/2026
}

COMMUNES = [
    # (cp, commune, state_file, lbc_premium)
    # lbc_premium=True : zones urbaines denses — premium_proxy + wait=10s + 1 worker
    ("69001", "Lyon 1er",                  "scrape_state_69001_lyon_1.json",                  True),
    ("69700", "Givors",                    "scrape_state_69700_givors.json",                  False),
    ("42800", "Rive-de-Gier",              "scrape_state_42800_rive_de_gier.json",             False),
    ("42410", "Pélussin",                  "scrape_state_42410_verin.json",                   False),
    ("69260", "Charbonnières-les-Bains",   "scrape_state_69260_charbonnieres_les_bains.json", False),
    ("69130", "Écully",                    "scrape_state_69130_ecully.json",                  True),
    ("69290", "Craponne",                  "scrape_state_69290_craponne.json",                False),
    ("69570", "Dardilly",                  "scrape_state_69570_dardilly.json",                False),
]

print("\n=== PARAMÈTRES EXTRAITS ===")
for cp, commune, state_file, lbc_premium in COMMUNES:
    sl_code, lbc_loc = _extract_params(state_file)
    sl_code = SL_CODE_OVERRIDES.get(cp, sl_code)
    lbc_loc = LBC_LOC_OVERRIDES.get(cp, lbc_loc)
    mode = "URBAN (premium+1w)" if lbc_premium else "standard"
    print(f"  {cp} {commune:25s} | sl={sl_code or 'N/A':20s} | lbc={mode}")

print("\n" + "="*60)
print("Lancement des scrapes...")
print("="*60)

for cp, commune, state_file, lbc_premium in COMMUNES:
    sl_code, lbc_loc = _extract_params(state_file)
    sl_code = SL_CODE_OVERRIDES.get(cp, sl_code)
    lbc_loc = LBC_LOC_OVERRIDES.get(cp, lbc_loc)
    mode_lbc = "URBAN premium+1w" if lbc_premium else "standard"
    print(f"\n{'='*60}")
    print(f"  → {commune} ({cp}) | LBC: {mode_lbc}")
    print(f"     sl_code : {sl_code or 'auto'}")
    print(f"     lbc_loc : {str(lbc_loc)[:60] if lbc_loc else 'auto'}")
    print(f"{'='*60}")
    try:
        cmd_run(cp, commune, sl_code=sl_code, lbc_loc=lbc_loc, auto_yes=True, lbc_premium=lbc_premium)
    except Exception as e:
        print(f"  ERREUR {commune} ({cp}) : {e}")
        continue

print("\n✓ Mise à jour terminée.")
