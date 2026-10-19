"""Uzupełnia współrzędne placówek na podstawie API NFZ v1.3.

W API v1.4 większość rekordów nie ma szerokości i długości geograficznej, a API
v1.3 (oznaczone przez NFZ jako nieaktualne, ale nadal działające) je podaje.
Skrypt pobiera v1.3 i zapisuje słownik MiejsceKey -> współrzędne:

  data/nfz/wspolrzedne_v13.csv.gz

Uruchamiany przez workflow raz w miesiącu (lub ręcznie). Gdy v1.3 zostanie
wyłączone, snapshot nadal działa – brakujące lokalizacje dostaną środek gminy.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import NFZ_DIR, ODDZIALY_NFZ, dzis_w_polsce, wczytaj_csv, zapisz_csv  # noqa: E402
from nfz_api import API_V13, LIMIT_NA_STRONE, KlientNfz, splaszcz  # noqa: E402

log = logging.getLogger("nfz")
KOLUMNY = ["MiejsceKey", "Szerokosc", "Dlugosc", "DataPobrania"]


def main(argv: list[str] | None = None, klient: KlientNfz | None = None, katalog: Path = NFZ_DIR) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default=API_V13)
    p.add_argument("--oddzialy", default=",".join(ODDZIALY_NFZ))
    p.add_argument("--przypadki", default="1,2")
    p.add_argument("--na-sekunde", type=float, default=5.0)
    p.add_argument("--watki", type=int, default=4)
    p.add_argument("--maks-stron", type=int, default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    klient = klient or KlientNfz(baza=a.api, na_sekunde=a.na_sekunde, dodatkowe_parametry={"api-version": "1.3"})
    dzis = dzis_w_polsce().isoformat()
    znalezione: dict[str, dict] = {}
    bledy = 0

    def zadanie(oddzial: str, przypadek: int) -> tuple[str, list[dict]]:
        return oddzial, list(klient.kolejki(przypadek, oddzial, LIMIT_NA_STRONE, a.maks_stron))

    oddzialy = [o.strip().zfill(2) for o in a.oddzialy.split(",") if o.strip()]
    przypadki = [int(c) for c in a.przypadki.split(",") if c.strip()]
    with ThreadPoolExecutor(max_workers=a.watki) as pula:
        for f in as_completed([pula.submit(zadanie, o, c) for o in oddzialy for c in przypadki]):
            try:
                oddzial, rekordy = f.result()
            except Exception as e:
                bledy += 1
                log.error("Błąd pobierania v1.3: %s", e)
                continue
            for r in rekordy:
                _, m, _ = splaszcz(r, dzis, oddzial)
                if m["SzerokoscNFZ"] is not None:
                    znalezione[m["MiejsceKey"]] = {
                        "MiejsceKey": m["MiejsceKey"],
                        "Szerokosc": m["SzerokoscNFZ"],
                        "Dlugosc": m["DlugoscNFZ"],
                        "DataPobrania": dzis,
                    }

    plik = katalog / "wspolrzedne_v13.csv.gz"
    stare = {w["MiejsceKey"]: w for w in wczytaj_csv(plik)}
    stare.update(znalezione)
    n = zapisz_csv(plik, sorted(stare.values(), key=lambda w: w["MiejsceKey"]), KOLUMNY)
    log.info("Współrzędne z v1.3: %s nowych/odświeżonych, %s łącznie, błędy: %s", len(znalezione), n, bledy)
    return 0  # brak v1.3 nie powinien zatrzymywać workflow


if __name__ == "__main__":
    sys.exit(main())
