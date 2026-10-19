"""Testy pipeline'u danych. Uruchomienie: python -m unittest discover -s tests -v

Fixture'y odwzorowują strukturę odpowiedzi API NFZ v1.4/v1.3 i GUS BDL
zweryfikowaną na żywych zapytaniach (październik 2026).
"""

from __future__ import annotations

import copy
import gzip
import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import gus_powiaty  # noqa: E402
import nfz_api  # noqa: E402
import nfz_snapshot  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def wczytaj_fixture(nazwa: str):
    return json.loads((FIX / nazwa).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)[:200] if payload is not None else ""

    def json(self):
        return copy.deepcopy(self._payload)


class FakeSession:
    """Serwuje strony z fixture'a: klucz (province, case) -> lista rekordów."""

    def __init__(self, rekordy_wg_klucza: dict, bledy_429: int = 0):
        self.rekordy = rekordy_wg_klucza
        self.bledy_429 = bledy_429
        self.wywolania = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.wywolania.append(dict(params))
        if self.bledy_429 > 0:
            self.bledy_429 -= 1
            return FakeResponse(429, {"error": "too many"}, {"Retry-After": "0"})
        klucz = (params["province"], int(params["case"]))
        rekordy = self.rekordy.get(klucz, [])
        limit, strona = int(params["limit"]), int(params["page"])
        wycinek = rekordy[(strona - 1) * limit: strona * limit]
        return FakeResponse(200, {"meta": {"count": len(rekordy)}, "links": {}, "data": wycinek})


class TestPcus(unittest.TestCase):
    def test_formaty(self):
        przypadki = {
            "0 dni": 0, "2 dni": 2, "30 dni": 30, "1 dzień": 1,
            "1 mies. 1 tydz.": 37, "1 mies. 3 tyg.": 51, "2 mies.": 60,
            "10 mies. 3 tyg.": 321, "11 mies.": 330, "1 rok 2 mies.": 425,
            "2 lata": 730, "14": 14, "": None, None: None, "brak": None,
        }
        for tekst, oczekiwane in przypadki.items():
            with self.subTest(tekst=tekst):
                self.assertEqual(common.parsuj_pcus(tekst), oczekiwane)


class TestTekst(unittest.TestCase):
    def test_zdanie(self):
        self.assertEqual(common.zdanie("PORADNIA KARDIOLOGICZNA DLA DZIECI"), "Poradnia kardiologiczna dla dzieci")
        self.assertEqual(common.zdanie("ODDZIAŁ CHORÓB WEWNĘTRZNYCH II"), "Oddział chorób wewnętrznych II")
        self.assertEqual(common.zdanie("BADANIA USG (NFZ)"), "Badania USG (NFZ)")

    def test_klucz_niezalezny_od_wielkosci_liter(self):
        self.assertEqual(nfz_api.klucz_swiadczenia("PORADNIA ALERGOLOGICZNA", "Nie"),
                         nfz_api.klucz_swiadczenia("Poradnia  alergologiczna", "Nie"))


class TestGeografia(unittest.TestCase):
    def test_wspolrzedne(self):
        self.assertEqual(common.popraw_wspolrzedne(52.87, 20.59)[2], "NFZ")
        lat, lon, opis = common.popraw_wspolrzedne(18.9123, 50.3539)  # zamienione w API v1.4
        self.assertEqual((lat, lon, opis), (50.3539, 18.9123, "NFZ (zamienione)"))
        self.assertEqual(common.popraw_wspolrzedne(None, 20.0)[2], "")
        self.assertEqual(common.popraw_wspolrzedne(0, 0)[2], "")

    def test_kandydaci_gminy(self):
        self.assertEqual(common.kandydaci_gminy("1402011"), ["1402011"])
        self.assertEqual(common.kandydaci_gminy("2410054"), ["2410054", "2410053"])
        self.assertEqual(common.kandydaci_gminy("1465058"), ["1465058", "1465011"])
        self.assertEqual(common.kandydaci_gminy("abc"), [])

    def test_odleglosc(self):
        # Warszawa – Kraków ok. 252 km w linii prostej
        self.assertAlmostEqual(common.odleglosc_km(52.2297, 21.0122, 50.0647, 19.9450), 252, delta=5)


class TestSplaszczanie(unittest.TestCase):
    def setUp(self):
        self.v14 = wczytaj_fixture("nfz_v14_rekordy.json")

    def test_rekord_z_pcus(self):
        fakt, miejsce, sw = nfz_api.splaszcz(self.v14[0], "2026-10-19", "07")
        self.assertEqual(fakt["PCUS"], "1 mies. 3 tyg.")
        self.assertEqual(fakt["PCUSDni"], 51)
        self.assertEqual(fakt["PCUSStanNa"], "2026-10-10")
        self.assertEqual(fakt["Przypadek"], "Stabilny")
        self.assertEqual(fakt["Oczekujacy"], 361)
        self.assertEqual(fakt["SredniCzasDni"], 50)
        self.assertEqual(fakt["WCeR"], "N")
        self.assertIsNone(miejsce["SzerokoscNFZ"])
        self.assertEqual(sw["DlaDzieci"], "Nie")
        self.assertEqual(len(fakt["MiejsceKey"]), 16)

    def test_rekord_w_cer_bez_statystyk(self):
        fakt, _, _ = nfz_api.splaszcz(self.v14[1], "2026-10-19", "12")
        self.assertEqual(fakt["WCeR"], "Y")
        self.assertIsNone(fakt["PCUSDni"])
        self.assertIsNone(fakt["Oczekujacy"])
        self.assertEqual(fakt["TerminDostepny"], "")

    def test_zamienione_wspolrzedne(self):
        _, miejsce, _ = nfz_api.splaszcz(self.v14[2], "2026-10-19", "12")
        self.assertAlmostEqual(miejsce["SzerokoscNFZ"], 50.3539)
        self.assertAlmostEqual(miejsce["DlugoscNFZ"], 18.9123)


class TestLokalizator(unittest.TestCase):
    def setUp(self):
        self.lok = nfz_snapshot.Lokalizator(ROOT / "data" / "geo", Path(tempfile.mkdtemp()))

    def test_bledne_wspolrzedne_daleko_od_gminy(self):
        # współrzędne z Krakowa przy TERYT Legionowa -> nie ufamy, bierzemy środek gminy
        m = self.lok.uzupelnij({"MiejsceKey": "x", "TerytMiejsca": "1408011", "SzerokoscNFZ": 50.06, "DlugoscNFZ": 19.94})
        self.assertEqual(m["ZrodloLokalizacji"], "środek gminy")
        self.assertEqual(m["TerytPowiat"], "1408")

    def test_dzielnica_warszawy(self):
        m = self.lok.uzupelnij({"MiejsceKey": "x", "TerytMiejsca": "1465058", "SzerokoscNFZ": None, "DlugoscNFZ": None})
        self.assertEqual((m["TerytPowiat"], m["ZrodloLokalizacji"]), ("1465", "środek gminy"))

    def test_brak_teryt_miejsca_powiat_z_siedziby(self):
        m = self.lok.uzupelnij({"MiejsceKey": "x", "TerytMiejsca": "", "TerytSwiadczeniodawcy": "3064011",
                                "SzerokoscNFZ": None, "DlugoscNFZ": None})
        self.assertEqual(m["TerytPowiat"], "3064")
        self.assertEqual(m["ZrodloLokalizacji"], "środek powiatu (siedziba świadczeniodawcy)")
        self.assertIsNotNone(m["Szerokosc"])

    def test_brak_teryt_ale_wspolrzedne(self):
        # punkt w Poznaniu (TERYT 3064) – powiat z przecięcia z granicami
        m = self.lok.uzupelnij({"MiejsceKey": "x", "TerytMiejsca": "", "SzerokoscNFZ": 52.4064, "DlugoscNFZ": 16.9252})
        self.assertEqual(m["TerytPowiat"], "3064")
        self.assertEqual(m["ZrodloLokalizacji"], "NFZ")


class TestKlient(unittest.TestCase):
    def test_ponawianie_429_i_stronicowanie(self):
        rekordy = [{"id": f"id{i}", "attributes": {"case": 1, "benefit": "X"}} for i in range(60)]
        sesja = FakeSession({("07", 1): rekordy}, bledy_429=2)
        uspienia = []
        klient = nfz_api.KlientNfz(sesja=sesja, na_sekunde=1000, uspij=uspienia.append)
        wynik = list(klient.kolejki(1, "07"))
        self.assertEqual(len(wynik), 60)
        self.assertEqual(len(uspienia), 2)                 # dwa ponowienia po 429
        strony = [c["page"] for c in sesja.wywolania]
        self.assertEqual(strony, [1, 1, 1, 2, 3])          # 60 rekordów / 25 = 3 strony
        self.assertTrue(all(c["limit"] == 25 for c in sesja.wywolania))

    def test_blad_trwaly(self):
        class Zawsze500(FakeSession):
            def get(self, url, params=None, timeout=None, headers=None):
                return FakeResponse(503, {})
        klient = nfz_api.KlientNfz(sesja=Zawsze500({}), na_sekunde=1000, maks_prob=3, uspij=lambda s: None)
        with self.assertRaises(nfz_api.BladApi):
            klient.pobierz({"case": 1, "province": "07", "page": 1, "limit": 25})


class TestSnapshotEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "data" / "geo").mkdir(parents=True)
        for plik in ("powiaty.csv", "gminy_centroidy.csv", "powiaty_uproszczone.geojson"):
            shutil.copy(ROOT / "data" / "geo" / plik, self.tmp / "data" / "geo" / plik)
        v14 = wczytaj_fixture("nfz_v14_rekordy.json")
        self.sesja = FakeSession({("07", 1): [v14[0], v14[3]], ("12", 1): [v14[1], v14[2], v14[4]], ("07", 2): [], ("12", 2): []})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def uruchom(self, data, dodatkowe=()):
        klient = nfz_api.KlientNfz(sesja=self.sesja, na_sekunde=1000)
        argv = ["--oddzialy", "07,12", "--przypadki", "1,2", "--data", data, *dodatkowe]
        return nfz_snapshot.main(argv, klient=klient, root=self.tmp)

    def czytaj(self, sciezka):
        return common.wczytaj_csv(self.tmp / sciezka)

    def test_pelny_przebieg(self):
        self.assertEqual(self.uruchom("2026-10-19"), 0)
        fakty = self.czytaj("data/nfz/snapshots/2026-10-19.csv.gz")
        self.assertEqual(len(fakty), 5)
        self.assertEqual(list(fakty[0].keys()), nfz_snapshot.KOLUMNY_FAKT)
        miejsca = {m["MiejsceKey"]: m for m in self.czytaj("data/nfz/miejsca.csv.gz")}
        self.assertEqual(len(miejsca), 5)
        zrodla = sorted(m["ZrodloLokalizacji"] for m in miejsca.values())
        self.assertIn("środek gminy", zrodla)               # brak współrzędnych -> gmina
        self.assertIn("NFZ", zrodla)                        # poprawione (zamienione) współrzędne
        for m in miejsca.values():
            self.assertTrue(m["TerytPowiat"], m)
            self.assertTrue(m["Szerokosc"], m)
        sw = self.czytaj("data/nfz/swiadczenia.csv")
        nazwy = {s["Swiadczenie"] for s in sw}
        # ta sama poradnia z dwóch oddziałów (WIELKIE i zwykłe litery) -> jeden wpis, ładna nazwa
        self.assertIn("Poradnia alergologiczna", nazwy)
        self.assertEqual(len([s for s in sw if s["SwiadczenieKlucz"] == "PORADNIA ALERGOLOGICZNA"]), 1)
        manifest = self.czytaj("data/nfz/manifest.csv")
        self.assertEqual(manifest[0]["Plik"], "data/nfz/snapshots/2026-10-19.csv.gz")
        self.assertEqual(manifest[0]["Wiersze"], "5")
        raport = json.loads((self.tmp / "data/nfz/ostatni_przebieg.json").read_text(encoding="utf-8"))
        self.assertEqual(raport["wiersze_w_cer"], 1)

    def test_drugi_snapshot_i_ochrona_przed_spadkiem(self):
        self.assertEqual(self.uruchom("2026-10-19"), 0)
        self.assertEqual(self.uruchom("2026-10-26"), 0)
        manifest = self.czytaj("data/nfz/manifest.csv")
        self.assertEqual([m["DataSnapshotu"] for m in manifest], ["2026-10-19", "2026-10-26"])
        # nagły spadek liczby wierszy -> brak zapisu
        self.sesja.rekordy = {("07", 1): self.sesja.rekordy[("07", 1)][:1]}
        self.assertEqual(self.uruchom("2026-11-02"), 2)
        self.assertFalse((self.tmp / "data/nfz/snapshots/2026-11-02.csv.gz").exists())
        # pierwsze wystąpienie miejsca zachowane po kolejnych przebiegach
        miejsca = self.czytaj("data/nfz/miejsca.csv.gz")
        self.assertTrue(all(m["PierwszyRaz"] == "2026-10-19" for m in miejsca))

    def test_gzip_deterministyczny(self):
        self.uruchom("2026-10-19")
        a = (self.tmp / "data/nfz/snapshots/2026-10-19.csv.gz").read_bytes()
        self.uruchom("2026-10-19")
        b = (self.tmp / "data/nfz/snapshots/2026-10-19.csv.gz").read_bytes()
        self.assertEqual(a, b)


class TestGus(unittest.TestCase):
    def test_teryt_z_id(self):
        self.assertEqual(gus_powiaty.teryt_z_id_bdl("011212001000"), "1201")
        self.assertEqual(gus_powiaty.teryt_z_id_bdl("011212006000"), "1206")
        with self.assertRaises(ValueError):
            gus_powiaty.teryt_z_id_bdl("123")

    def test_zbuduj_wiersze(self):
        bdl = wczytaj_fixture("bdl_odpowiedzi.json")
        dane = {}
        for nazwa, wyniki in bdl.items():
            dane[nazwa] = {r["id"]: {"nazwa": r["name"], "wartosci": gus_powiaty.wartosci_wg_roku(r)} for r in wyniki}
        wiersze = gus_powiaty.zbuduj_wiersze(dane, {"1201", "1206"})
        self.assertEqual([w["TerytPowiat"] for w in wiersze], ["1201", "1206"])
        boch = wiersze[0]
        self.assertEqual(boch["Ludnosc"], 106926)
        self.assertEqual(boch["RokLudnosci"], 2025)
        self.assertEqual(boch["Ludnosc65plus"], 6100 + 13200)
        self.assertEqual(boch["Wynagrodzenie"], 7123.45)
        self.assertEqual(boch["RokWynagrodzenia"], 2024)


if __name__ == "__main__":
    unittest.main()
