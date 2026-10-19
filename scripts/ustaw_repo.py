"""Wpisuje adres Twojego repozytorium GitHub w projekcie Power BI.

Zmienia w jednym kroku:
  • parametry Power Query (GitHubWlasciciel, GitHubRepozytorium, GitHubGalaz),
  • adres pliku mapy powiatów w wizualizacji Shape Map,
  • link do repozytorium na stronie „O danych” i w README.

Użycie (z katalogu głównego repozytorium, przy zamkniętym Power BI Desktop):
    python scripts/ustaw_repo.py LOGIN/NAZWA-REPOZYTORIUM [--galaz main]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPR = ROOT / "powerbi" / "GdzieNajkrocej.SemanticModel" / "definition" / "expressions.tmdl"
RAPORT = ROOT / "powerbi" / "GdzieNajkrocej.Report" / "definition"


def odczytaj_parametr(tekst: str, nazwa: str) -> str:
    m = re.search(rf'^expression {nazwa} = "([^"]*)"', tekst, flags=re.M)
    if not m:
        sys.exit(f"Nie znaleziono parametru {nazwa} w {EXPR}")
    return m.group(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("repo", help="LOGIN/NAZWA-REPOZYTORIUM, np. jankowalski/gdzie-czeka-sie-najkrocej")
    p.add_argument("--galaz", default="main")
    a = p.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", a.repo):
        sys.exit("Podaj repozytorium w formacie LOGIN/NAZWA, np. jankowalski/gdzie-czeka-sie-najkrocej")
    wlasciciel, repo = a.repo.split("/")

    tekst = EXPR.read_text(encoding="utf-8")
    stary_w = odczytaj_parametr(tekst, "GitHubWlasciciel")
    stary_r = odczytaj_parametr(tekst, "GitHubRepozytorium")
    stara_g = odczytaj_parametr(tekst, "GitHubGalaz")
    for nazwa, wartosc in (("GitHubWlasciciel", wlasciciel), ("GitHubRepozytorium", repo), ("GitHubGalaz", a.galaz)):
        tekst = re.sub(rf'^(expression {nazwa} = )"[^"]*"', rf'\g<1>"{wartosc}"', tekst, flags=re.M)
    EXPR.write_text(tekst, encoding="utf-8")

    zamiany = {
        f"https://raw.githubusercontent.com/{stary_w}/{stary_r}/{stara_g}/":
            f"https://raw.githubusercontent.com/{wlasciciel}/{repo}/{a.galaz}/",
        f"https://github.com/{stary_w}/{stary_r}": f"https://github.com/{wlasciciel}/{repo}",
    }
    zmienione = [EXPR]
    pliki = list(RAPORT.rglob("visual.json")) + [ROOT / "README.md"]
    for plik in pliki:
        if not plik.exists():
            continue
        tresc = plik.read_text(encoding="utf-8")
        nowa = tresc
        for stare, nowe in zamiany.items():
            nowa = nowa.replace(stare, nowe)
        if nowa != tresc:
            plik.write_text(nowa, encoding="utf-8")
            zmienione.append(plik)
    print(f"Repozytorium ustawione na {wlasciciel}/{repo} (gałąź {a.galaz}). Zmienione pliki:")
    for z in zmienione:
        print("  -", z.relative_to(ROOT))


if __name__ == "__main__":
    main()
