"""Testy pipeline'u danych. Uruchomienie: python -m unittest discover -s tests -v

Fixture'y odwzorowują strukturę odpowiedzi API NFZ v1.4/v1.3 i GUS BDL
zweryfikowaną na żywych zapytaniach (październik 2026).
"""

from __future__ import annotations

import copy
import json
import math
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import gus_powiaty  # noqa: E402
import nfz_api  # noqa: E402
import nfz_snapshot  # noqa: E402
import nfz_wspolrzedne_v13  # noqa: E402

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


class FakeZegar:
    """Czas testowy: uśpienie przesuwa zegar zamiast naprawdę czekać."""

    def __init__(self):
        self.t = 1000.0
        self.uspienia = []
        self._lock = threading.Lock()

    def __call__(self):
        return self.t

    def uspij(self, sekundy):
        with self._lock:
            self.uspienia.append(sekundy)
            self.t += max(0.0, sekundy)


class FakeSerwerZLimitem(FakeSession):
    """Jak API v1.4: najwyżej `limit` zapytań w oknie `okno` s liczonym od pierwszego zapytania,
    potem HTTP 429 z Retry-After (sekundy do końca okna, w górę)."""

    def __init__(self, rekordy, zegar, limit=10, okno=10.0):
        super().__init__(rekordy)
        self.zegar, self.limit, self.okno = zegar, limit, okno
        self.poczatek, self.w_oknie, self.odmowy = None, 0, 0
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None, headers=None):
        with self._lock:
            t = self.zegar()
            if self.poczatek is None or t >= self.poczatek + self.okno:
                self.poczatek, self.w_oknie = t, 0
            if self.w_oknie >= self.limit:
                self.odmowy += 1
                return FakeResponse(429, {"error": "too many"},
                                    {"Retry-After": str(math.ceil(self.poczatek + self.okno - t))})
            self.w_oknie += 1
        return super().get(url, params, timeout, headers)


def rekordy_testowe(n, przedrostek="r"):
    return [{"id": f"{przedrostek}{i}", "attributes": {"case": 1, "benefit": "X"}} for i in range(n)]


def klient_testowy(sesja, na_sekunde=1000.0, **kwargs):
    zegar = FakeZegar()
    return nfz_api.KlientNfz(sesja=sesja, na_sekunde=na_sekunde, zegar=zegar, uspij=zegar.uspij, **kwargs), zegar


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


class TestDopasowanieV13(unittest.TestCase):
    """API v1.3 podaje adres z cechą ulicy („UL. ZEGRZYŃSKA 8”), a v1.4 bez niej („ZEGRZYŃSKA 8”)."""

    def setUp(self):
        self.v14 = wczytaj_fixture("nfz_v14_rekordy.json")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def miejsce(self, i: int, **zmiany) -> dict:
        r = copy.deepcopy(self.v14[i])
        r["attributes"].update(zmiany)
        return nfz_api.splaszcz(r, "2026-10-20", "07")[1]

    def lokalizator(self, wiersze_v13: list[dict], kolumny=None) -> nfz_snapshot.Lokalizator:
        common.zapisz_csv(self.tmp / "wspolrzedne_v13.csv.gz", wiersze_v13, kolumny or nfz_wspolrzedne_v13.KOLUMNY)
        return nfz_snapshot.Lokalizator(ROOT / "data" / "geo", self.tmp)

    @staticmethod
    def wiersz_v13(m: dict, lat: float, lon: float, data: str = "2026-10-20") -> dict:
        return {"MiejsceKey": m["MiejsceKey"], "KluczAdres": nfz_api.klucz_adresu(m),
                "KluczKomorka": nfz_api.klucz_komorki(m), "Szerokosc": lat, "Dlugosc": lon, "DataPobrania": data}

    def test_normalizacja_adresu_i_miejscowosci(self):
        self.assertEqual(nfz_api.norm_adres("UL. DŁUGA 5"), "DŁUGA 5")
        self.assertEqual(nfz_api.norm_adres("Długa  5"), "DŁUGA 5")
        self.assertEqual(nfz_api.norm_adres("ul. Gen. Józefa Bema 5"), "GEN JÓZEFA BEMA 5")
        self.assertEqual(nfz_api.norm_adres("PL. PLAC WOLNOŚCI 1"), nfz_api.norm_adres("PLAC WOLNOŚCI 1"))
        self.assertEqual(nfz_api.norm_adres("RYNEK"), "RYNEK")
        self.assertEqual(nfz_api.norm_miejscowosc("KRAKÓW-KROWODRZA"), "KRAKÓW")
        self.assertEqual(nfz_api.norm_miejscowosc("Warszawa Śródmieście"), "WARSZAWA")
        self.assertEqual(nfz_api.norm_miejscowosc(None), "")

    def test_klucz_z_wiersza_miejsca_jak_z_api(self):
        m = self.miejsce(0)
        pola = [m[p] for p in nfz_api._POLA_KLUCZA_MIEJSCA]
        self.assertEqual(common.hash_klucz(*pola, dlugosc=16), m["MiejsceKey"])

    def test_wariant_klucza_z_cecha_ulicy(self):
        m14 = self.miejsce(0)
        m13 = self.miejsce(0, address="UL. ZEGRZYŃSKA 8")
        self.assertNotEqual(m13["MiejsceKey"], m14["MiejsceKey"])
        self.assertIn(m13["MiejsceKey"], set(nfz_api.warianty_klucza_v13(m14)))

    def test_klucze_zapasowe(self):
        m14 = self.miejsce(3)  # WARSZAWA MOKOTÓW, PUŁAWSKA 100
        m13 = self.miejsce(3, address="UL. PUŁAWSKA 100", locality="WARSZAWA")
        self.assertEqual(nfz_api.klucz_adresu(m13), nfz_api.klucz_adresu(m14))
        self.assertEqual(nfz_api.klucz_komorki(m13), nfz_api.klucz_komorki(m14))
        self.assertNotEqual(nfz_api.klucz_adresu(self.miejsce(3, **{"teryt-place": "1465011"})), nfz_api.klucz_adresu(m14))
        self.assertEqual(nfz_api.klucz_adresu(self.miejsce(3, **{"teryt-place": None})), "")
        self.assertEqual(nfz_api.klucz_komorki(self.miejsce(3, **{"id-resort-part-VII": None})), "")

    def test_stary_plik_dopasowanie_po_wariancie_klucza(self):
        # plik sprzed 20.10.2026: tylko MiejsceKey, liczony z adresu „UL. …”
        m13 = self.miejsce(0, address="UL. ZEGRZYŃSKA 8")
        lok = self.lokalizator([{"MiejsceKey": m13["MiejsceKey"], "Szerokosc": 52.4012, "Dlugosc": 20.9265,
                                 "DataPobrania": "2026-10-19"}], ["MiejsceKey", "Szerokosc", "Dlugosc", "DataPobrania"])
        m = lok.uzupelnij(self.miejsce(0))
        self.assertEqual(m["ZrodloLokalizacji"], "NFZ (API v1.3)")
        self.assertAlmostEqual(m["Szerokosc"], 52.4012)

    def test_ten_sam_adres_innej_komorki(self):
        inna = self.miejsce(3, address="UL. PUŁAWSKA 100", locality="WARSZAWA",
                            **{"id-resort-part-VII": "099", "id-resort-part-VIII": "1300"})
        lok = self.lokalizator([self.wiersz_v13(inna, 52.1937, 21.0207)])
        m = lok.uzupelnij(self.miejsce(3))
        self.assertEqual(m["ZrodloLokalizacji"], "NFZ (API v1.3, ten sam adres)")
        self.assertAlmostEqual(m["Dlugosc"], 21.0207)
        self.assertEqual(m["TerytPowiat"], "1465")

    def test_ta_sama_komorka_przy_innym_zapisie_adresu(self):
        m13 = self.miejsce(3, address="PUŁAWSKA 100/102")
        lok = self.lokalizator([self.wiersz_v13(m13, 52.1937, 21.0207)])
        self.assertEqual(lok.uzupelnij(self.miejsce(3))["ZrodloLokalizacji"], "NFZ (API v1.3, ta sama komórka)")

    def test_niejednoznaczny_adres_pominiety(self):
        # ten sam adres, ale punkty 5 km od siebie -> nie zgadujemy
        a = self.miejsce(3, **{"id-resort-part-VII": "098"})
        b = self.miejsce(3, **{"id-resort-part-VII": "099"})
        lok = self.lokalizator([self.wiersz_v13(a, 52.1937, 21.0207), self.wiersz_v13(b, 52.2387, 21.0207)])
        m = lok.uzupelnij(self.miejsce(3, **{"id-resort-part-VII": "097"}))
        self.assertEqual(m["ZrodloLokalizacji"], "środek gminy")

    def test_najnowsze_pobranie_wygrywa(self):
        a = self.miejsce(3, **{"id-resort-part-VII": "098"})
        b = self.miejsce(3, **{"id-resort-part-VII": "099"})
        lok = self.lokalizator([self.wiersz_v13(a, 52.1937, 21.0207, data="2026-09-01"),
                                self.wiersz_v13(b, 52.2387, 21.0207, data="2026-10-20")])
        m = lok.uzupelnij(self.miejsce(3, **{"id-resort-part-VII": "097"}))
        self.assertEqual(m["ZrodloLokalizacji"], "NFZ (API v1.3, ten sam adres)")
        self.assertAlmostEqual(m["Szerokosc"], 52.2387)

    def test_wspolrzedne_v13_daleko_od_gminy_odrzucone(self):
        lok = self.lokalizator([self.wiersz_v13(self.miejsce(0), 50.06, 19.94)])  # Kraków przy TERYT Legionowa
        self.assertEqual(lok.uzupelnij(self.miejsce(0))["ZrodloLokalizacji"], "środek gminy")


class TestKlient(unittest.TestCase):
    def test_ponawianie_429_i_stronicowanie(self):
        sesja = FakeSession({("07", 1): rekordy_testowe(60)}, bledy_429=2)
        klient, zegar = klient_testowy(sesja)
        wynik = list(klient.kolejki(1, "07"))
        self.assertEqual(len(wynik), 60)
        self.assertEqual(klient.statystyki["http_429"], 2)
        self.assertEqual(klient.statystyki["ponowienia"], 0)   # 429 nie zużywa prób na awarie
        strony = [c["page"] for c in sesja.wywolania]
        self.assertEqual(strony, [1, 1, 1, 2, 3])              # 2 × 429, potem 60 rekordów / 25 = 3 strony
        self.assertTrue(all(c["limit"] == 25 for c in sesja.wywolania))
        self.assertGreaterEqual(sum(zegar.uspienia), 2.0)      # Retry-After: 0 -> mimo to min. 1 s pauzy

    def test_blad_trwaly(self):
        class Zawsze503(FakeSession):
            def get(self, url, params=None, timeout=None, headers=None):
                return FakeResponse(503, {})
        klient, _ = klient_testowy(Zawsze503({}), maks_prob=3)
        with self.assertRaises(nfz_api.BladApi):
            klient.pobierz({"case": 1, "province": "07", "page": 1, "limit": 25})
        self.assertEqual(klient.statystyki["zapytania"], 3)

    def test_dluga_seria_429_nie_przerywa_pobierania(self):
        # stary klient poddawał się po 8 próbach; teraz 429 to tylko sygnał, żeby zwolnić
        sesja = FakeSession({("07", 1): rekordy_testowe(10)}, bledy_429=20)
        klient, _ = klient_testowy(sesja)
        dane = klient.pobierz({"case": 1, "province": "07", "page": 1, "limit": 25})
        self.assertEqual(len(dane["data"]), 10)
        self.assertEqual(klient.statystyki["http_429"], 20)

    def test_retry_after_jako_data_http(self):
        odp = FakeResponse(429, {}, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.assertEqual(nfz_api._retry_after(odp), 0.0)       # data z przeszłości -> bez czekania
        self.assertEqual(nfz_api._retry_after(FakeResponse(429, {}, {"Retry-After": "8"})), 8.0)
        self.assertIsNone(nfz_api._retry_after(FakeResponse(429, {}, {})))


class TestOgranicznik(unittest.TestCase):
    def test_rowne_odstepy(self):
        z = FakeZegar()
        o = nfz_api.Ogranicznik(2.0, zegar=z, uspij=z.uspij)
        starty = []
        for _ in range(5):
            o.czekaj()
            starty.append(z())
        self.assertEqual([round(b - a, 6) for a, b in zip(starty, starty[1:])], [0.5] * 4)

    def test_wspolna_pauza_i_jedno_zwolnienie_na_epizod(self):
        z = FakeZegar()
        o = nfz_api.Ogranicznik(1.0, zegar=z, uspij=z.uspij)
        o.czekaj()
        pauza, nowy = o.przy_429(8.0)
        self.assertTrue(nowy)
        self.assertEqual(pauza, 8.0)
        _, nowy2 = o.przy_429(7.0)                 # 429 z drugiego wątku w tym samym epizodzie
        self.assertFalse(nowy2)
        self.assertAlmostEqual(o.odstep, 1.25)     # tempo spadło tylko raz
        t0 = z()
        o.czekaj()
        self.assertGreaterEqual(z() - t0, 8.0)     # każdy wątek czeka do końca pauzy

    def test_powrot_do_tempa_bazowego(self):
        z = FakeZegar()
        o = nfz_api.Ogranicznik(1.0, zegar=z, uspij=z.uspij, seria_do_przyspieszenia=10)
        o.przy_429(None)
        self.assertAlmostEqual(o.odstep, 1.25)
        for _ in range(50):
            o.sukces()
        self.assertAlmostEqual(o.odstep, 1.0)      # nigdy szybciej niż tempo bazowe


class TestPobieranieBlokow(unittest.TestCase):
    def test_tempo_ponizej_limitu_api_bez_odmow(self):
        z = FakeZegar()
        serwer = FakeSerwerZLimitem({("07", 1): rekordy_testowe(500, "a"), ("07", 2): rekordy_testowe(260, "b")}, z)
        klient = nfz_api.KlientNfz(sesja=serwer, na_sekunde=nfz_api.TEMPO_V14, zegar=z, uspij=z.uspij)
        zebrane = []
        wynik = nfz_api.pobierz_bloki(klient, [("07", 1), ("07", 2)], lambda o, c, r: zebrane.extend(r),
                                      watki=1, limit=25)
        self.assertEqual(len(zebrane), 760)
        self.assertEqual(wynik.brakujace, [])
        self.assertEqual(wynik.strony_lacznie, 20 + 11)
        self.assertEqual(serwer.odmowy, 0)

    def test_za_szybki_klient_dostosowuje_sie_i_nie_gubi_stron(self):
        # tak wyglądał przebieg z 19.10.2026: 5 zapytań/s przy limicie 10 na 10 s
        z = FakeZegar()
        serwer = FakeSerwerZLimitem({("07", 1): rekordy_testowe(2000)}, z)
        klient = nfz_api.KlientNfz(sesja=serwer, na_sekunde=5.0, zegar=z, uspij=z.uspij)
        zebrane = []
        t0 = z()
        wynik = nfz_api.pobierz_bloki(klient, [("07", 1)], lambda o, c, r: zebrane.extend(r), watki=1, limit=25)
        self.assertEqual(len(zebrane), 2000)
        self.assertEqual(wynik.brakujace, [])
        self.assertGreater(serwer.odmowy, 0)
        self.assertLess(serwer.odmowy, 15)                        # kilka epizodów, nie setki
        self.assertLessEqual(klient.ogranicznik.na_sekunde, 1.1)  # zjechał w okolice tego, co API przyjmuje
        tempo = wynik.strony_pobrane / (z() - t0)
        self.assertGreater(tempo, 0.7)                            # ...ale nie zwolnił przesadnie

    def test_dogrywka_odzyskuje_strone_po_awariach(self):
        class Kaprysny(FakeSession):
            awarie = 0

            def get(self, url, params=None, timeout=None, headers=None):
                if int(params["page"]) == 3 and self.awarie < 10:  # więcej awarii niż maks_prob (8)
                    self.awarie += 1
                    return FakeResponse(503, {})
                return super().get(url, params, timeout, headers)
        klient, _ = klient_testowy(Kaprysny({("07", 1): rekordy_testowe(100)}))
        zebrane = []
        wynik = nfz_api.pobierz_bloki(klient, [("07", 1)], lambda o, c, r: zebrane.extend(r), watki=2, limit=25)
        self.assertEqual(len(zebrane), 100)
        self.assertEqual(wynik.brakujace, [])

    def test_trwale_niedostepna_strona_trafia_do_brakow(self):
        class Dziura(FakeSession):
            def get(self, url, params=None, timeout=None, headers=None):
                if int(params["page"]) == 2:
                    return FakeResponse(503, {})
                return super().get(url, params, timeout, headers)
        klient, _ = klient_testowy(Dziura({("07", 1): rekordy_testowe(100)}))
        zebrane = []
        wynik = nfz_api.pobierz_bloki(klient, [("07", 1)], lambda o, c, r: zebrane.extend(r), watki=2, limit=25)
        self.assertEqual(len(zebrane), 75)                        # pozostałe strony bloku są pobrane
        self.assertEqual([(b["blok"], b["strona"]) for b in wynik.brakujace], [("07/1", 2)])
        self.assertEqual(wynik.bloki_bez_danych, [])


class TestLimitStrony(unittest.TestCase):
    def test_wieksza_strona_gdy_api_ja_zwraca(self):
        klient, _ = klient_testowy(FakeSession({("07", 1): rekordy_testowe(300)}))
        self.assertEqual(nfz_api.ustal_limit(klient), 100)

    def test_api_po_cichu_przycina_strone(self):
        class Przycina(FakeSession):
            def get(self, url, params=None, timeout=None, headers=None):
                return super().get(url, {**params, "limit": min(25, int(params["limit"]))}, timeout, headers)
        klient, _ = klient_testowy(Przycina({("07", 1): rekordy_testowe(300)}))
        self.assertEqual(nfz_api.ustal_limit(klient), 25)

    def test_api_odrzuca_duzy_limit(self):
        class Odrzuca(FakeSession):
            def get(self, url, params=None, timeout=None, headers=None):
                if int(params["limit"]) > 25:
                    return FakeResponse(400, {"errors": [{"title": "limit"}]})
                return super().get(url, params, timeout, headers)
        klient, _ = klient_testowy(Odrzuca({("07", 1): rekordy_testowe(300)}))
        self.assertEqual(nfz_api.ustal_limit(klient), 25)


class TestWspolrzedneV13(unittest.TestCase):
    def test_tylko_przypadek_stabilny(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            v14 = wczytaj_fixture("nfz_v14_rekordy.json")
            sesja = FakeSession({("12", 1): [v14[2]], ("12", 2): [v14[2]]})
            klient, _ = klient_testowy(sesja, dodatkowe_parametry={"api-version": "1.3"})
            kod = nfz_wspolrzedne_v13.main(["--oddzialy", "12", "--limit", "25"], klient=klient, katalog=tmp)
            self.assertEqual(kod, 0)
            self.assertEqual({int(c["case"]) for c in sesja.wywolania}, {1})
            wiersze = common.wczytaj_csv(tmp / "wspolrzedne_v13.csv.gz")
            self.assertEqual(len(wiersze), 1)
            self.assertAlmostEqual(float(wiersze[0]["Szerokosc"]), 50.3539, places=4)
            self.assertEqual(list(wiersze[0].keys()), nfz_wspolrzedne_v13.KOLUMNY)
            self.assertTrue(wiersze[0]["KluczAdres"] and wiersze[0]["KluczKomorka"])
        finally:
            shutil.rmtree(tmp)


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
        klient, _ = klient_testowy(self.sesja)
        argv = ["--oddzialy", "07,12", "--przypadki", "1,2", "--limit", "25", "--data", data, *dodatkowe]
        return nfz_snapshot.main(argv, klient=klient, root=self.tmp)

    def raport(self):
        return json.loads((self.tmp / "data/nfz/ostatni_przebieg.json").read_text(encoding="utf-8"))

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

    def test_wspolrzedne_z_v13_mimo_cechy_ulicy_w_adresie(self):
        r = copy.deepcopy(wczytaj_fixture("nfz_v14_rekordy.json")[0])
        r["attributes"]["address"] = "UL. " + r["attributes"]["address"]
        _, m13, _ = nfz_api.splaszcz(r, "2026-10-19", "07")
        common.zapisz_csv(self.tmp / "data/nfz/wspolrzedne_v13.csv.gz",
                          [{"MiejsceKey": m13["MiejsceKey"], "Szerokosc": 52.4012, "Dlugosc": 20.9265,
                            "DataPobrania": "2026-10-19"}], ["MiejsceKey", "Szerokosc", "Dlugosc", "DataPobrania"])
        self.assertEqual(self.uruchom("2026-10-20"), 0)
        legionowo = next(m for m in self.czytaj("data/nfz/miejsca.csv.gz") if m["Miejscowosc"] == "LEGIONOWO")
        self.assertEqual(legionowo["ZrodloLokalizacji"], "NFZ (API v1.3)")
        self.assertEqual(self.raport()["zrodla_lokalizacji"]["NFZ (API v1.3)"], 1)

    def test_gzip_deterministyczny(self):
        self.uruchom("2026-10-19")
        a = (self.tmp / "data/nfz/snapshots/2026-10-19.csv.gz").read_bytes()
        self.uruchom("2026-10-19")
        b = (self.tmp / "data/nfz/snapshots/2026-10-19.csv.gz").read_bytes()
        self.assertEqual(a, b)

    def test_brakujace_strony_a_prog_zapisu(self):
        wzor = wczytaj_fixture("nfz_v14_rekordy.json")[0]
        rekordy = []
        for i in range(60):
            r = copy.deepcopy(wzor)
            r["id"] = f"q{i}"
            rekordy.append(r)

        class ZepsutaStrona(FakeSession):
            def get(self, url, params=None, timeout=None, headers=None):
                if (params["province"], int(params["case"]), int(params["page"])) == ("07", 1, 2):
                    return FakeResponse(503, {})
                return super().get(url, params, timeout, headers)

        self.sesja = ZepsutaStrona({("07", 1): rekordy, ("12", 1): [], ("07", 2): [], ("12", 2): []})
        # 1 z 6 stron po dogrywce nadal brakuje -> powyżej progu 1% -> brak zapisu, ale jest raport
        self.assertEqual(self.uruchom("2026-10-19"), 1)
        self.assertFalse((self.tmp / "data/nfz/snapshots/2026-10-19.csv.gz").exists())
        raport = self.raport()
        self.assertFalse(raport["zapisany"])
        self.assertEqual(raport["brakujace_strony"], 1)
        self.assertIn("brakuje 1 z 6 stron", raport["powod_odrzucenia"])
        # przy wyższym progu snapshot jest zapisany z adnotacją
        self.assertEqual(self.uruchom("2026-10-19", ["--maks-braki", "0.5"]), 0)
        self.assertEqual(len(self.czytaj("data/nfz/snapshots/2026-10-19.csv.gz")), 35)  # 60 − 25 z brakującej strony
        raport = self.raport()
        self.assertTrue(raport["zapisany"])
        self.assertFalse(raport["kompletny"])
        self.assertEqual(raport["brakujace_strony_szczegoly"][0]["strona"], 2)


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
