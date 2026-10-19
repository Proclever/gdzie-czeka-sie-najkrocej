"""Pobiera z GUS BDL (API v1) dane o powiatach: ludność ogółem, ludność 65+
i przeciętne wynagrodzenie, i zapisuje je do data/gus/powiaty_gus.csv.

Zmienne BDL (sprawdzone w API, październik 2026):
  72305  Ludność wg grup wieku i płci (P2137): ogółem, ogółem
  72239  P2137: 65-69, ogółem
  72240  P2137: 70 i więcej, ogółem          -> 65+ = 72239 + 72240
  64428  Przeciętne miesięczne wynagrodzenia brutto (P2497): ogółem [zł]
  64429  P2497: w relacji do średniej krajowej (Polska = 100) [%]

Identyfikator jednostki BDL (12 znaków) zawiera kod TERYT:
  011212001000 -> województwo = znaki 3–4 ("12"), powiat = znaki 8–9 ("01") -> TERYT 1201

Klucz API (opcjonalny, wyższe limity) można podać w zmiennej środowiskowej BDL_API_KEY.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GEO_DIR, GUS_DIR, wczytaj_csv, zapisz_csv  # noqa: E402

log = logging.getLogger("gus")

API = "https://bdl.stat.gov.pl/api/v1/data/by-variable/{id}"
ZMIENNE = {"ludnosc": 72305, "l65_69": 72239, "l70plus": 72240, "wynagrodzenie": 64428, "wynagrodzenie_pl100": 64429}
KOLUMNY = ["TerytPowiat", "NazwaBDL", "Ludnosc", "Ludnosc65plus", "RokLudnosci",
           "Wynagrodzenie", "WynagrodzenieIndeksPL100", "RokWynagrodzenia"]


def teryt_z_id_bdl(id_bdl: str) -> str:
    """'011212001000' -> '1201'."""
    if len(id_bdl) != 12 or not id_bdl.isdigit():
        raise ValueError(f"nieoczekiwany identyfikator BDL: {id_bdl!r}")
    return id_bdl[2:4] + id_bdl[7:9]


def wartosci_wg_roku(wynik: dict) -> dict[int, float]:
    out = {}
    for v in wynik.get("values") or []:
        if v.get("val") is None:
            continue
        try:
            out[int(v["year"])] = float(v["val"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def pobierz_zmienna(sesja, id_zmiennej: int, klucz_api: str | None, pauza: float = 0.25) -> dict[str, dict]:
    """Zwraca {id_jednostki_bdl: {"nazwa": ..., "wartosci": {rok: wartość}}} dla powiatów."""
    naglowki = {"Accept": "application/json"}
    if klucz_api:
        naglowki["X-ClientId"] = klucz_api
    wynik: dict[str, dict] = {}
    strona = 0
    while True:
        params = {"unit-level": 5, "page-size": 100, "page": strona, "format": "json"}
        for proba in range(6):
            r = sesja.get(API.format(id=id_zmiennej), params=params, headers=naglowki, timeout=60)
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 502, 503, 504):
                czekaj = min(120, 5 * 2 ** proba)
                log.warning("BDL HTTP %s – ponawiam za %s s", r.status_code, czekaj)
                time.sleep(czekaj)
                continue
            r.raise_for_status()
        else:
            raise RuntimeError(f"BDL: nie udało się pobrać zmiennej {id_zmiennej}, strona {strona}")
        dane = r.json()
        for jednostka in dane.get("results") or []:
            wynik[jednostka["id"]] = {"nazwa": jednostka.get("name", ""), "wartosci": wartosci_wg_roku(jednostka)}
        if not (dane.get("links") or {}).get("next"):
            break
        strona += 1
        time.sleep(pauza)  # limit anonimowy: 5 zapytań/s, 100 na 15 min
    return wynik


def zbuduj_wiersze(dane: dict[str, dict[str, dict]], poprawne_teryt: set[str]) -> list[dict]:
    """Łączy zmienne w jeden wiersz na powiat; bierze najnowszy rok z kompletem danych."""
    wiersze = {}
    for id_bdl, info in dane["ludnosc"].items():
        try:
            teryt = teryt_z_id_bdl(id_bdl)
        except ValueError:
            continue
        if poprawne_teryt and teryt not in poprawne_teryt:
            continue  # jednostki historyczne lub spoza bieżącego podziału
        lud = info["wartosci"]
        a = dane["l65_69"].get(id_bdl, {}).get("wartosci", {})
        b = dane["l70plus"].get(id_bdl, {}).get("wartosci", {})
        lata = sorted(set(lud) & set(a) & set(b))
        if not lata:
            continue
        rok = lata[-1]
        wyn = dane["wynagrodzenie"].get(id_bdl, {}).get("wartosci", {})
        idx = dane["wynagrodzenie_pl100"].get(id_bdl, {}).get("wartosci", {})
        rok_w = max(wyn) if wyn else None
        wiersze[teryt] = {
            "TerytPowiat": teryt,
            "NazwaBDL": info["nazwa"],
            "Ludnosc": int(lud[rok]),
            "Ludnosc65plus": int(a[rok] + b[rok]),
            "RokLudnosci": rok,
            "Wynagrodzenie": None if rok_w is None else round(wyn[rok_w], 2),
            "WynagrodzenieIndeksPL100": None if rok_w is None or rok_w not in idx else round(idx[rok_w], 1),
            "RokWynagrodzenia": rok_w,
        }
    return [wiersze[k] for k in sorted(wiersze)]


def main(argv: list[str] | None = None, sesja=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wyjscie", default=str(GUS_DIR / "powiaty_gus.csv"))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sesja is None:
        import requests
        sesja = requests.Session()
    klucz = os.environ.get("BDL_API_KEY") or None
    dane = {nazwa: pobierz_zmienna(sesja, idz, klucz) for nazwa, idz in ZMIENNE.items()}
    poprawne = {w["TerytPowiat"] for w in wczytaj_csv(GEO_DIR / "powiaty.csv")}
    wiersze = zbuduj_wiersze(dane, poprawne)
    brakujace = sorted(poprawne - {w["TerytPowiat"] for w in wiersze})
    if brakujace:
        log.warning("Brak danych GUS dla %s powiatów: %s", len(brakujace), ", ".join(brakujace[:20]))
    if len(wiersze) < 300:
        log.error("Za mało powiatów (%s) – nie zapisuję pliku.", len(wiersze))
        return 1
    n = zapisz_csv(Path(a.wyjscie), wiersze, KOLUMNY)
    log.info("Zapisano %s powiatów do %s", n, a.wyjscie)
    return 0


if __name__ == "__main__":
    sys.exit(main())
