"""Uzupełnia współrzędne placówek na podstawie API NFZ v1.3.

W API v1.4 większość rekordów nie ma szerokości i długości geograficznej, a API
v1.3 (oznaczone przez NFZ jako nieaktualne, ale nadal działające) je podaje.
Skrypt pobiera v1.3 i zapisuje słownik MiejsceKey -> współrzędne:

  data/nfz/wspolrzedne_v13.csv.gz

Wystarczy przypadek 1 (stabilny). W przebiegu z 19.10.2026 v1.3 zwróciło dla obu
przypadków identyczne liczby rekordów we wszystkich 16 oddziałach, czyli te same
miejsca. Pobieranie jednego przypadku skraca krok o połowę, do ok. 30 minut.

Uruchamiany przez workflow raz w miesiącu (lub ręcznie). Gdy v1.3 zostanie
wyłączone, snapshot nadal działa – brakujące lokalizacje dostaną środek gminy.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import NFZ_DIR, ODDZIALY_NFZ, dzis_w_polsce, wczytaj_csv, zapisz_csv  # noqa: E402
from nfz_api import API_V13, TEMPO_V13, KlientNfz, pobierz_bloki, splaszcz, ustal_limit  # noqa: E402

log = logging.getLogger("nfz")
KOLUMNY = ["MiejsceKey", "Szerokosc", "Dlugosc", "DataPobrania"]


def main(argv: list[str] | None = None, klient: KlientNfz | None = None, katalog: Path = NFZ_DIR) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default=API_V13)
    p.add_argument("--oddzialy", default=",".join(ODDZIALY_NFZ))
    p.add_argument("--przypadki", default="1", help="1 = stabilny, 2 = pilny (oba dają te same miejsca)")
    p.add_argument("--na-sekunde", type=float, default=TEMPO_V13)
    p.add_argument("--watki", type=int, default=3)
    p.add_argument("--limit", default="auto", help="rekordów na stronę: auto albo liczba")
    p.add_argument("--maks-stron", type=int, default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    klient = klient or KlientNfz(baza=a.api, na_sekunde=a.na_sekunde, dodatkowe_parametry={"api-version": "1.3"})
    dzis = dzis_w_polsce().isoformat()
    znalezione: dict[str, dict] = {}

    def przetworz(oddzial: str, przypadek: int, rekordy: list[dict]) -> None:
        for r in rekordy:
            _, m, _ = splaszcz(r, dzis, oddzial)
            if m["SzerokoscNFZ"] is not None:
                znalezione[m["MiejsceKey"]] = {
                    "MiejsceKey": m["MiejsceKey"],
                    "Szerokosc": m["SzerokoscNFZ"],
                    "Dlugosc": m["DlugoscNFZ"],
                    "DataPobrania": dzis,
                }

    oddzialy = [o.strip().zfill(2) for o in a.oddzialy.split(",") if o.strip()]
    przypadki = [int(c) for c in a.przypadki.split(",") if c.strip()]
    limit = ustal_limit(klient) if str(a.limit).lower() == "auto" else int(a.limit)
    wynik = pobierz_bloki(klient, [(o, c) for o in oddzialy for c in przypadki], przetworz,
                          watki=a.watki, limit=limit, maks_stron=a.maks_stron)

    plik = katalog / "wspolrzedne_v13.csv.gz"
    stare = {w["MiejsceKey"]: w for w in wczytaj_csv(plik)}
    stare.update(znalezione)
    n = zapisz_csv(plik, sorted(stare.values(), key=lambda w: w["MiejsceKey"]), KOLUMNY)
    log.info("Współrzędne z v1.3: %s nowych/odświeżonych, %s łącznie; brakujące strony: %s; zapytania: %s, HTTP 429: %s",
             len(znalezione), n, len(wynik.brakujace), klient.statystyki["zapytania"], klient.statystyki["http_429"])
    return 0  # brak v1.3 nie powinien zatrzymywać workflow


if __name__ == "__main__":
    sys.exit(main())
