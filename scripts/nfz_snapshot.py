"""Tygodniowy snapshot kolejek NFZ (API „Terminy leczenia” v1.4).

Iteruje po 16 oddziałach NFZ × 2 przypadkach (stabilny/pilny), stronicuje po 25
rekordów, ponawia zapytania przy błędach 429/5xx i zapisuje:

  data/nfz/snapshots/RRRR-MM-DD.csv.gz  – fakty: snapshot × miejsce × świadczenie × przypadek
  data/nfz/miejsca.csv.gz               – wymiar miejsc (najświeższe atrybuty + lokalizacja)
  data/nfz/swiadczenia.csv              – wymiar świadczeń (nazwa, dla dzieci)
  data/nfz/manifest.csv                 – lista snapshotów (czyta ją Power BI)
  data/nfz/ostatni_przebieg.json        – statystyki ostatniego uruchomienia

Przykłady:
  python scripts/nfz_snapshot.py                       # pełny snapshot (ok. 20–40 min)
  python scripts/nfz_snapshot.py --oddzialy 07 --przypadki 1 --maks-stron 2 --bez-zapisu
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    GEO_DIR,
    NFZ_DIR,
    ODDZIALY_NFZ,
    ROOT,
    SNAPSHOT_DIR,
    dzis_w_polsce,
    kandydaci_gminy,
    klucz_tekstowy,
    odleglosc_km,
    wczytaj_csv,
    zapisz_csv,
    zdanie,
)
from nfz_api import API_V14, LIMIT_NA_STRONE, KlientNfz, splaszcz  # noqa: E402

log = logging.getLogger("nfz")

KOLUMNY_FAKT = [
    "DataSnapshotu", "MiejsceKey", "SwiadczenieKey", "Przypadek", "Oddzial", "WieleMiejsc", "WCeR",
    "Znieczulenie", "PrzedzialWieku", "Oczekujacy", "Skresleni", "SredniCzasDni", "OkresStatystyk",
    "PCUS", "PCUSDni", "PCUSStanNa", "TerminDostepny", "PierwszyTermin", "PrognozaNFZDni",
]  # IdKolejki (GUID) służy tylko do usuwania duplikatów – nie zapisujemy go, bo słabo się kompresuje
KOLUMNY_MIEJSCA = [
    "MiejsceKey", "Swiadczeniodawca", "KodSwiadczeniodawcy", "RegonSwiadczeniodawcy", "NipSwiadczeniodawcy",
    "TerytSwiadczeniodawcy", "Miejsce", "Adres", "Miejscowosc", "Telefon", "TerytMiejsca", "TerytPowiat",
    "NrKsiegiRejestrowej", "KodResortowyVII", "KodResortowyVIII", "Oddzial", "Toaleta", "Podjazd", "Parking",
    "Winda", "Klimatyzacja", "DrzwiAutomatyczne", "WozkiInwalidzkie", "Korytarze", "TypBudynku",
    "KomunikacjaPubliczna", "Szerokosc", "Dlugosc", "ZrodloLokalizacji", "SzerokoscNFZ", "DlugoscNFZ",
    "PierwszyRaz", "OstatnioWidziane",
]
KOLUMNY_SWIADCZENIA = ["SwiadczenieKey", "Swiadczenie", "SwiadczenieKlucz", "DlaDzieci", "PierwszyRaz", "OstatnioWidziane"]
KOLUMNY_MANIFESTU = ["DataSnapshotu", "Plik", "Wiersze", "WierszeZPCUS", "WierszeWCeR", "WersjaApi"]

MAKS_ODLEGLOSC_OD_GMINY_KM = 60.0


# ---------------------------------------------------------------------------
# Lokalizacja placówek
# ---------------------------------------------------------------------------
class Lokalizator:
    """Ustala współrzędne i powiat miejsca na podstawie (kolejno):
    1) współrzędnych z NFZ v1.4 (po walidacji), 2) współrzędnych z NFZ v1.3 (plik
    wspolrzedne_v13.csv.gz), 3) środka gminy wg TERYT, 4) środka powiatu."""

    def __init__(self, katalog_geo: Path = GEO_DIR, katalog_nfz: Path = NFZ_DIR) -> None:
        self.powiaty = {w["TerytPowiat"]: w for w in wczytaj_csv(katalog_geo / "powiaty.csv")}
        self.gminy = {w["TerytGminy"]: w for w in wczytaj_csv(katalog_geo / "gminy_centroidy.csv")}
        self.v13 = {w["MiejsceKey"]: w for w in wczytaj_csv(katalog_nfz / "wspolrzedne_v13.csv.gz")}
        self._ksztalty = None
        self._sciezka_geojson = katalog_geo / "powiaty_uproszczone.geojson"

    def _gmina(self, teryt7: str) -> dict | None:
        for kod in kandydaci_gminy(teryt7):
            if kod in self.gminy:
                return self.gminy[kod]
        return None

    def _powiat_z_punktu(self, lat: float, lon: float) -> str:
        if self._ksztalty is None:
            self._ksztalty = []
            try:
                from shapely.geometry import Point, shape  # noqa: F401
                dane = json.loads(self._sciezka_geojson.read_text(encoding="utf-8"))
                self._ksztalty = [(f["properties"]["teryt"], shape(f["geometry"])) for f in dane["features"]]
            except Exception as e:  # brak shapely lub pliku – pomijamy ten krok
                log.warning("Przypisanie powiatu po współrzędnych niedostępne: %s", e)
        if not self._ksztalty:
            return ""
        from shapely.geometry import Point
        p = Point(lon, lat)
        for teryt, ksztalt in self._ksztalty:
            if ksztalt.contains(p):
                return teryt
        return ""

    def uzupelnij(self, m: dict) -> dict:
        teryt7 = m.get("TerytMiejsca", "")
        gmina = self._gmina(teryt7)
        lat = m.get("SzerokoscNFZ")
        lon = m.get("DlugoscNFZ")
        lat = float(lat) if lat not in (None, "") else None
        lon = float(lon) if lon not in (None, "") else None
        zrodlo = "NFZ" if lat is not None else ""

        if lat is not None and gmina is not None:
            d = odleglosc_km(lat, lon, float(gmina["Szerokosc"]), float(gmina["Dlugosc"]))
            if d > MAKS_ODLEGLOSC_OD_GMINY_KM:  # współrzędne niezgodne z TERYT – nie ufamy im
                lat = lon = None
                zrodlo = ""
        if lat is None and m["MiejsceKey"] in self.v13:
            w = self.v13[m["MiejsceKey"]]
            lat, lon, zrodlo = float(w["Szerokosc"]), float(w["Dlugosc"]), "NFZ (API v1.3)"
        if lat is None and gmina is not None:
            lat, lon, zrodlo = float(gmina["Szerokosc"]), float(gmina["Dlugosc"]), "środek gminy"

        powiat = teryt7[:4] if teryt7[:4] in self.powiaty else ""
        if not powiat and lat is not None:
            powiat = self._powiat_z_punktu(lat, lon)
        if not powiat:
            # ostatnia deska ratunku: powiat siedziby świadczeniodawcy
            t_sw = (m.get("TerytSwiadczeniodawcy") or "")[:4]
            if t_sw in self.powiaty:
                powiat = t_sw
                if lat is None:
                    zrodlo = "środek powiatu (siedziba świadczeniodawcy)"
        if lat is None and powiat:
            p = self.powiaty[powiat]
            lat, lon = float(p["SzerokoscSrodka"]), float(p["DlugoscSrodka"])
            zrodlo = zrodlo or "środek powiatu"

        m["Szerokosc"] = None if lat is None else round(lat, 6)
        m["Dlugosc"] = None if lon is None else round(lon, 6)
        m["ZrodloLokalizacji"] = zrodlo or "brak"
        m["TerytPowiat"] = powiat
        return m


# ---------------------------------------------------------------------------
# Pobieranie
# ---------------------------------------------------------------------------
def pobierz_wszystko(klient: KlientNfz, oddzialy: list[str], przypadki: list[int], data_snapshotu: str,
                     watki: int, maks_stron: int | None) -> tuple[list[dict], dict, dict, Counter, list[str]]:
    fakty: dict[str, dict] = {}
    miejsca: dict[str, dict] = {}
    swiadczenia: dict[str, dict] = {}
    warianty_nazw: dict[str, Counter] = defaultdict(Counter)
    liczniki: Counter = Counter()
    bledy: list[str] = []

    def zadanie(oddzial: str, przypadek: int) -> tuple[str, int, list[dict]]:
        return oddzial, przypadek, list(klient.kolejki(przypadek, oddzial, LIMIT_NA_STRONE, maks_stron))

    with ThreadPoolExecutor(max_workers=watki) as pula:
        przyszle = [pula.submit(zadanie, o, c) for o in oddzialy for c in przypadki]
        for f in as_completed(przyszle):
            try:
                oddzial, przypadek, rekordy = f.result()
            except Exception as e:  # zbieramy błędy, na końcu decydujemy
                bledy.append(str(e))
                log.error("Błąd pobierania: %s", e)
                continue
            liczniki[f"{oddzial}/{przypadek}"] = len(rekordy)
            for r in rekordy:
                fakt, miejsce, sw = splaszcz(r, data_snapshotu, oddzial)
                klucz = fakt["IdKolejki"] or f"{fakt['MiejsceKey']}|{fakt['SwiadczenieKey']}|{fakt['Przypadek']}|{fakt['Znieczulenie']}"
                fakty[klucz] = fakt
                stare = miejsca.get(miejsce["MiejsceKey"])
                if stare is None or (not stare.get("SzerokoscNFZ") and miejsce.get("SzerokoscNFZ")):
                    miejsca[miejsce["MiejsceKey"]] = miejsce
                swiadczenia[sw["SwiadczenieKey"]] = sw
                warianty_nazw[sw["SwiadczenieKlucz"]][sw["Swiadczenie"]] += 1
    # Nazwa do wyświetlania: wariant pisany normalnie (jeśli któryś oddział go podaje),
    # inaczej zamiana WIELKICH LITER na zdanie.
    for sw in swiadczenia.values():
        warianty = warianty_nazw[sw["SwiadczenieKlucz"]]
        mieszane = [(n, c) for n, c in warianty.items() if n != n.upper()]
        sw["Swiadczenie"] = max(mieszane, key=lambda x: x[1])[0] if mieszane else zdanie(sw["SwiadczenieKlucz"])
    return list(fakty.values()), miejsca, swiadczenia, liczniki, bledy


# ---------------------------------------------------------------------------
# Scalanie wymiarów z poprzednimi przebiegami
# ---------------------------------------------------------------------------
def scal_miejsca(stare: list[dict], nowe: dict, data_snapshotu: str, lokalizator: Lokalizator) -> list[dict]:
    wynik = {w["MiejsceKey"]: w for w in stare}
    for key, m in nowe.items():
        poprzednie = wynik.get(key, {})
        if not m.get("SzerokoscNFZ") and poprzednie.get("SzerokoscNFZ"):
            # NFZ czasem gubi współrzędne – zachowujemy ostatnie znane
            m["SzerokoscNFZ"], m["DlugoscNFZ"] = poprzednie["SzerokoscNFZ"], poprzednie["DlugoscNFZ"]
        m["PierwszyRaz"] = poprzednie.get("PierwszyRaz") or data_snapshotu
        m["OstatnioWidziane"] = data_snapshotu
        wynik[key] = lokalizator.uzupelnij(m)
    return sorted(wynik.values(), key=lambda w: w["MiejsceKey"])


def scal_swiadczenia(stare: list[dict], nowe: dict, data_snapshotu: str) -> list[dict]:
    wynik = {w["SwiadczenieKey"]: w for w in stare}
    for key, s in nowe.items():
        poprzednie = wynik.get(key, {})
        s["PierwszyRaz"] = poprzednie.get("PierwszyRaz") or data_snapshotu
        s["OstatnioWidziane"] = data_snapshotu
        wynik[key] = s
    return sorted(wynik.values(), key=lambda w: (w["SwiadczenieKlucz"], w["DlaDzieci"]))


def aktualizuj_manifest(katalog_nfz: Path, katalog_snapshotow: Path, root: Path) -> list[dict]:
    poprzedni = {w["DataSnapshotu"]: w for w in wczytaj_csv(katalog_nfz / "manifest.csv")}
    wiersze = []
    for plik in sorted(katalog_snapshotow.glob("*.csv.gz")):
        data = plik.name[:10]
        stary = poprzedni.get(data)
        if stary and stary.get("Wiersze"):
            wiersz = dict(stary)
        else:
            dane = wczytaj_csv(plik)
            wiersz = {
                "DataSnapshotu": data,
                "Wiersze": len(dane),
                "WierszeZPCUS": sum(1 for w in dane if w.get("PCUSDni") not in ("", None)),
                "WierszeWCeR": sum(1 for w in dane if w.get("WCeR") == "Y"),
                "WersjaApi": "1.4",
            }
        wiersz["Plik"] = plik.relative_to(root).as_posix()
        wiersze.append(wiersz)
    zapisz_csv(katalog_nfz / "manifest.csv", wiersze, KOLUMNY_MANIFESTU)
    return wiersze


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None, klient: KlientNfz | None = None, root: Path = ROOT) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default=API_V14, help="adres zasobu /queues (domyślnie API v1.4)")
    p.add_argument("--oddzialy", default=",".join(ODDZIALY_NFZ), help="kody oddziałów NFZ 01–16, po przecinku")
    p.add_argument("--przypadki", default="1,2", help="1 = stabilny, 2 = pilny")
    p.add_argument("--na-sekunde", type=float, default=5.0, help="limit zapytań na sekundę (łącznie)")
    p.add_argument("--watki", type=int, default=4)
    p.add_argument("--maks-stron", type=int, default=None, help="tylko do testów")
    p.add_argument("--data", default=None, help="data snapshotu RRRR-MM-DD (domyślnie dziś w Polsce)")
    p.add_argument("--min-udzial", type=float, default=0.7,
                   help="przerwij, jeśli liczba wierszy spadła poniżej tego udziału poprzedniego snapshotu")
    p.add_argument("--force", action="store_true", help="zapisz mimo błędów lub dużego spadku liczby wierszy")
    p.add_argument("--bez-zapisu", action="store_true", help="tylko pobierz i pokaż statystyki")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    nfz_dir = root / "data" / "nfz"
    snap_dir = nfz_dir / "snapshots"
    geo_dir = root / "data" / "geo"
    data_snapshotu = a.data or dzis_w_polsce().isoformat()
    oddzialy = [o.strip().zfill(2) for o in a.oddzialy.split(",") if o.strip()]
    przypadki = [int(c) for c in a.przypadki.split(",") if c.strip()]

    start = time.time()
    klient = klient or KlientNfz(baza=a.api, na_sekunde=a.na_sekunde)
    fakty, miejsca, swiadczenia, liczniki, bledy = pobierz_wszystko(
        klient, oddzialy, przypadki, data_snapshotu, a.watki, a.maks_stron)
    czas = time.time() - start

    z_pcus = sum(1 for f in fakty if f["PCUSDni"] is not None)
    w_cer = sum(1 for f in fakty if f["WCeR"] == "Y")
    nieodczytane = Counter(f["PCUS"] for f in fakty if f["PCUS"] and f["PCUSDni"] is None)
    podsumowanie = {
        "data_snapshotu": data_snapshotu,
        "api": a.api,
        "czas_s": round(czas, 1),
        "zapytania": klient.statystyki["zapytania"],
        "ponowienia": klient.statystyki["ponowienia"],
        "wiersze": len(fakty),
        "wiersze_z_pcus": z_pcus,
        "wiersze_w_cer": w_cer,
        "miejsca": len(miejsca),
        "swiadczenia": len(swiadczenia),
        "na_oddzial_i_przypadek": dict(sorted(liczniki.items())),
        "nieodczytane_pcus": dict(nieodczytane.most_common(20)),
        "bledy": bledy,
    }
    log.info("Podsumowanie: %s", json.dumps({k: v for k, v in podsumowanie.items() if k != "na_oddzial_i_przypadek"}, ensure_ascii=False))

    if bledy and not a.force:
        log.error("Pobieranie niekompletne (%s błędów) – snapshot NIE został zapisany.", len(bledy))
        return 1
    if a.bez_zapisu:
        print(json.dumps(podsumowanie, ensure_ascii=False, indent=2))
        return 0

    manifest = wczytaj_csv(nfz_dir / "manifest.csv")
    poprzednie = [w for w in manifest if w["DataSnapshotu"] < data_snapshotu and w.get("Wiersze")]
    if poprzednie and not a.force:
        ostatni = int(poprzednie[-1]["Wiersze"])
        if len(fakty) < a.min_udzial * ostatni:
            log.error("Liczba wierszy spadła z %s do %s – podejrzenie niepełnych danych. Użyj --force, jeśli to celowe.",
                      ostatni, len(fakty))
            return 2

    fakty.sort(key=lambda f: (f["Oddzial"], f["Przypadek"], f["SwiadczenieKey"], f["MiejsceKey"], f["IdKolejki"]))
    plik = snap_dir / f"{data_snapshotu}.csv.gz"
    zapisz_csv(plik, fakty, KOLUMNY_FAKT)

    lokalizator = Lokalizator(geo_dir, nfz_dir)
    m = scal_miejsca(wczytaj_csv(nfz_dir / "miejsca.csv.gz"), miejsca, data_snapshotu, lokalizator)
    zapisz_csv(nfz_dir / "miejsca.csv.gz", m, KOLUMNY_MIEJSCA)
    s = scal_swiadczenia(wczytaj_csv(nfz_dir / "swiadczenia.csv"), swiadczenia, data_snapshotu)
    zapisz_csv(nfz_dir / "swiadczenia.csv", s, KOLUMNY_SWIADCZENIA)
    aktualizuj_manifest(nfz_dir, snap_dir, root)

    zrodla = Counter(w["ZrodloLokalizacji"] for w in m if w["OstatnioWidziane"] == data_snapshotu)
    podsumowanie["zrodla_lokalizacji"] = dict(zrodla)
    podsumowanie["miejsca_bez_powiatu"] = sum(1 for w in m if w["OstatnioWidziane"] == data_snapshotu and not w["TerytPowiat"])
    (nfz_dir / "ostatni_przebieg.json").write_text(json.dumps(podsumowanie, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("Zapisano %s (%s wierszy). Lokalizacje: %s", plik.relative_to(root), len(fakty), dict(zrodla))
    return 0


if __name__ == "__main__":
    sys.exit(main())
