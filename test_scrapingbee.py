"""
Test des paramètres ScrapingBee sur SeLoger et LeBonCoin.
Sauvegarde les HTML bruts dans debug/ et affiche un résumé par test.
"""

import os
import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

SCRAPINGBEE_KEY = os.environ["SCRAPINGBEE_KEY"]
SUPA_URL = os.environ["SUPA_URL"]
SUPA_KEY = os.environ["SUPA_KEY"]

SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"
DEBUG_DIR = Path(__file__).parent / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# Format validé : classified-search avec le code postal dans locations=
# L'ancien format /achat/appartements-maisons/<slug>/ est obsolète sur SeLoger.
URL_SELOGER = (
    "https://www.seloger.com/classified-search"
    "?classifiedBusiness=Professional"
    "&distributionTypes=Buy"
    "&estateTypes=House,Apartment"
    "&locations=69005"
    "&projectTypes=Resale"
)

# Format /cl/ validé — l'endpoint /recherche?category=9 échoue avec Datadome.
URL_LBC = "https://www.leboncoin.fr/cl/ventes_immobilieres/cp_lyon_69005/real_estate_type:2/p-1"

SELOGER_MARKER = "__UFRN_FETCHER__"
LBC_MARKER = "__NEXT_DATA__"


def scrape_via_scrapingbee(url: str, params: dict, timeout: int = 60) -> requests.Response:
    payload = {"api_key": SCRAPINGBEE_KEY, "url": url, **params}
    return requests.get(SCRAPINGBEE_URL, params=payload, timeout=timeout)


def scrape_direct(url: str) -> requests.Response:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    return requests.get(url, headers=headers, timeout=30)


def analyse_response(
    label: str,
    response: requests.Response,
    marker: str,
    output_path: Path,
    credits_header: str = "x-scrapingbee-cost",
) -> None:
    html = response.text
    output_path.write_text(html, encoding="utf-8")

    size_ko = len(html.encode("utf-8")) / 1024
    marker_found = marker in html
    credits = response.headers.get(credits_header, "n/a")

    status_icon = "OK" if response.status_code == 200 else "ERR"
    marker_icon = "PRESENT" if marker_found else "ABSENT"

    print(
        f"  [{status_icon}] {label:10s} | HTTP {response.status_code}"
        f" | {size_ko:8.1f} Ko"
        f" | {marker:24s} : {marker_icon}"
        f" | credits: {credits}"
    )


def run_seloger_tests() -> None:
    print(f"\n=== SeLoger ===")
    print(f"  URL : {URL_SELOGER}\n")

    tests = [
        (
            "Test A",
            {"render_js": "false", "premium_proxy": "false"},
        ),
        (
            "Test B",
            {"render_js": "false", "premium_proxy": "true"},
        ),
        (
            "Test C",
            {"render_js": "true", "premium_proxy": "true", "wait": "3000"},
        ),
        (
            "Test D",
            {"render_js": "true", "premium_proxy": "true", "wait": "5000"},
        ),
    ]

    for label, params in tests:
        print(f"  Lancement {label}...", end="", flush=True)
        suffix = label.split()[-1].lower()
        try:
            resp = scrape_via_scrapingbee(URL_SELOGER, params)
            out = DEBUG_DIR / f"seloger_test_{suffix}.html"
            print(f"\r", end="")
            analyse_response(label, resp, SELOGER_MARKER, out)
        except Exception as exc:
            print(f"\r  [ERR] {label:10s} | Exception : {exc}")


def run_lbc_tests() -> None:
    print("\n=== LeBonCoin ===")
    print(f"  URL : {URL_LBC}\n")

    print("  Lancement Test E (direct, sans ScrapingBee)...", end="", flush=True)
    try:
        resp_e = scrape_direct(URL_LBC)
        out_e = DEBUG_DIR / "lbc_test_e.html"
        print(f"\r", end="")
        analyse_response("Test E", resp_e, LBC_MARKER, out_e, credits_header="x-none")
    except Exception as exc:
        print(f"\r  [ERR] Test E     | Exception : {exc}")

    sb_tests = [
        (
            "Test F",
            {"render_js": "false", "premium_proxy": "false"},
        ),
        (
            # stealth_proxy requis sur LBC (Datadome bloque premium_proxy)
            "Test G",
            {
                "render_js": "true",
                "stealth_proxy": "true",
                "wait": "3000",
                "block_resources": "false",
            },
            120,
        ),
        (
            # stealth_proxy : contourne Datadome (75 crédits), timeout élevé
            "Test H",
            {
                "render_js": "true",
                "stealth_proxy": "true",
                "wait": "5000",
                "block_resources": "false",
            },
            120,
        ),
    ]

    for item in sb_tests:
        label, params, *rest = item
        timeout = rest[0] if rest else 60
        print(f"  Lancement {label}...", end="", flush=True)
        suffix = label.split()[-1].lower()
        try:
            resp = scrape_via_scrapingbee(URL_LBC, params, timeout=timeout)
            out = DEBUG_DIR / f"lbc_test_{suffix}.html"
            print(f"\r", end="")
            analyse_response(label, resp, LBC_MARKER, out)
        except Exception as exc:
            print(f"\r  [ERR] {label:10s} | Exception : {exc}")


def main() -> None:
    print("=== Test ScrapingBee — Ideeri ===")
    print(f"Clé ScrapingBee : {'*' * 8}{SCRAPINGBEE_KEY[-6:]}")
    print(f"Résultats HTML  : {DEBUG_DIR}/")

    run_seloger_tests()
    run_lbc_tests()

    print("\nTerminé. Vérifiez le dossier debug/ pour les HTML bruts.")


if __name__ == "__main__":
    main()
