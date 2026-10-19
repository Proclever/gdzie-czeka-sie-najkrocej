"""Klient API NFZ „Terminy leczenia” i spłaszczanie rekordów do tabel.

API v1.4 (od 10.07.2026): https://apinfz.nfz.gov.pl/app-itl-api-pcus/queues
  - dates.pcus – prognozowany czas oczekiwania (PCUŚ), tekst, np. "1 mies. 3 tyg."
  - dates.date-situation-as-at – data, na którą policzono PCUŚ
  - queue-in-cer – kolejka prowadzona w centralnej e-rejestracji (brak PCUŚ i statystyk)
  - współrzędne są w większości puste, część ma zamienioną szerokość z długością
API v1.3 (nieaktualne, ale nadal dostępne): https://api.nfz.gov.pl/app-itl-api/queues
  - używane wyłącznie do uzupełnienia współrzędnych placówek
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Callable, Iterator

from common import (
    PRZYPADKI,
    hash_klucz,
    klucz_tekstowy,
    norm_spacje,
    parsuj_pcus,
    popraw_wspolrzedne,
)

API_V14 = "https://apinfz.nfz.gov.pl/app-itl-api-pcus/queues"
API_V13 = "https://api.nfz.gov.pl/app-itl-api/queues"
LIMIT_NA_STRONE = 25  # maksimum dopuszczane przez API

log = logging.getLogger("nfz")


class BladApi(RuntimeError):
    pass


class Ogranicznik:
    """Wspólny dla wątków limit liczby zapytań na sekundę."""

    def __init__(self, na_sekunde: float) -> None:
        self.odstep = 1.0 / max(na_sekunde, 0.1)
        self._lock = threading.Lock()
        self._nastepny = 0.0

    def czekaj(self) -> None:
        with self._lock:
            teraz = time.monotonic()
            start = max(teraz, self._nastepny)
            self._nastepny = start + self.odstep
        opoznienie = start - time.monotonic()
        if opoznienie > 0:
            time.sleep(opoznienie)


class KlientNfz:
    """Pobiera strony z API z ponawianiem (429/5xx, błędy sieci) i wykładniczym backoffem."""

    KODY_DO_PONOWIENIA = {429, 500, 502, 503, 504}

    def __init__(
        self,
        baza: str = API_V14,
        na_sekunde: float = 5.0,
        maks_prob: int = 8,
        timeout: float = 60.0,
        sesja=None,
        dodatkowe_parametry: dict | None = None,
        uspij: Callable[[float], None] = time.sleep,
    ) -> None:
        if sesja is None:
            import requests  # zależność tylko przy prawdziwym pobieraniu

            sesja = requests.Session()
            sesja.headers.update({
                "Accept": "application/json",
                "User-Agent": "gdzie-czeka-sie-najkrocej/1.0 (projekt portfolio; snapshot tygodniowy)",
            })
        self.baza = baza
        self.sesja = sesja
        self.ogranicznik = Ogranicznik(na_sekunde)
        self.maks_prob = maks_prob
        self.timeout = timeout
        self.dodatkowe = dodatkowe_parametry or {}
        self.uspij = uspij
        self.statystyki = {"zapytania": 0, "ponowienia": 0}
        self._lock = threading.Lock()

    def _licz(self, klucz: str) -> None:
        with self._lock:
            self.statystyki[klucz] += 1

    def pobierz(self, parametry: dict) -> dict:
        params = {"format": "json", **self.dodatkowe, **parametry}
        ostatni_blad = ""
        for proba in range(1, self.maks_prob + 1):
            self.ogranicznik.czekaj()
            self._licz("zapytania")
            try:
                r = self.sesja.get(self.baza, params=params, timeout=self.timeout)
            except Exception as e:  # błąd sieci / timeout
                ostatni_blad = f"{type(e).__name__}: {e}"
                status = None
            else:
                status = r.status_code
                if status == 200:
                    try:
                        return r.json()
                    except ValueError as e:
                        ostatni_blad = f"niepoprawny JSON: {e}"
                elif status in self.KODY_DO_PONOWIENIA:
                    ostatni_blad = f"HTTP {status}"
                else:
                    raise BladApi(f"HTTP {status} dla {params}: {getattr(r, 'text', '')[:300]}")
            if proba == self.maks_prob:
                break
            self._licz("ponowienia")
            czekanie = min(300.0, 2.0 ** proba) + random.uniform(0, 1.5)
            retry_after = None
            if status == 429:
                try:
                    retry_after = float(r.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    retry_after = None
            if retry_after:
                czekanie = max(czekanie, retry_after)
            log.warning("Ponawiam (%s/%s) za %.1f s: %s %s", proba, self.maks_prob, czekanie, ostatni_blad, params)
            self.uspij(czekanie)
        raise BladApi(f"Nie udało się pobrać {params} po {self.maks_prob} próbach: {ostatni_blad}")

    def kolejki(self, przypadek: int, oddzial: str, limit: int = LIMIT_NA_STRONE,
                maks_stron: int | None = None) -> Iterator[dict]:
        """Iteruje po wszystkich rekordach dla pary (przypadek, oddział NFZ)."""
        bazowe = {"case": przypadek, "province": oddzial, "limit": limit}
        pierwsza = self.pobierz({**bazowe, "page": 1})
        liczba = int((pierwsza.get("meta") or {}).get("count") or 0)
        stron = max(1, math.ceil(liczba / limit))
        if maks_stron:
            stron = min(stron, maks_stron)
        log.info("Oddział %s, przypadek %s: %s rekordów, %s stron", oddzial, przypadek, liczba, stron)
        yield from (pierwsza.get("data") or [])
        for strona in range(2, stron + 1):
            dane = self.pobierz({**bazowe, "page": strona})
            rekordy = dane.get("data") or []
            if not rekordy:
                log.warning("Pusta strona %s (oddział %s, przypadek %s) – kończę wcześniej", strona, oddzial, przypadek)
                break
            yield from rekordy


# ---------------------------------------------------------------------------
# Spłaszczanie
# ---------------------------------------------------------------------------
def _yn(wartosc) -> str:
    if wartosc is None:
        return ""
    s = str(wartosc).strip().upper()
    return {"Y": "Y", "N": "N", "T": "Y", "TRUE": "Y", "FALSE": "N"}.get(s, s)


def _int(wartosc) -> int | None:
    if wartosc is None or wartosc == "":
        return None
    try:
        return int(round(float(wartosc)))
    except (TypeError, ValueError):
        return None


def _data(wartosc) -> str:
    s = norm_spacje(wartosc)
    return s[:10] if len(s) >= 10 else s


def klucz_miejsca(a: dict) -> str:
    """Stabilny klucz miejsca udzielania świadczeń (komórka organizacyjna + adres)."""
    return hash_klucz(
        a.get("provider-code"), a.get("registry-number"), a.get("id-resort-part-VII"),
        a.get("id-resort-part-VIII"), a.get("teryt-place"), a.get("address"), a.get("locality"),
        dlugosc=16,
    )


def dla_dzieci(a: dict) -> str:
    return "Tak" if _yn(a.get("benefits-for-children")) == "Y" else "Nie"


def klucz_swiadczenia(nazwa: str, dzieci: str) -> str:
    return hash_klucz(nazwa, dzieci, dlugosc=12)


def splaszcz(rekord: dict, data_snapshotu: str, oddzial: str) -> tuple[dict, dict, dict]:
    """Rozbija rekord API na (wiersz faktu, wiersz miejsca, wiersz świadczenia)."""
    a = rekord.get("attributes") or {}
    stat = a.get("statistics") or {}
    pd = stat.get("provider-data") or {}
    cd = stat.get("computed-data") or {}
    daty = a.get("dates") or {}

    miejsce_key = klucz_miejsca(a)
    nazwa_sw = norm_spacje(a.get("benefit"))
    dzieci = dla_dzieci(a)
    sw_key = klucz_swiadczenia(nazwa_sw, dzieci)
    przypadek = _int(a.get("case"))

    pcus_tekst = norm_spacje(daty.get("pcus")) if daty else ""
    fakt = {
        "DataSnapshotu": data_snapshotu,
        "IdKolejki": rekord.get("id", ""),
        "MiejsceKey": miejsce_key,
        "SwiadczenieKey": sw_key,
        "Przypadek": PRZYPADKI.get(przypadek, str(przypadek or "")),
        "Oddzial": oddzial,
        "WieleMiejsc": _yn(a.get("many-places")),
        "WCeR": _yn(a.get("queue-in-cer")) or "N",
        "Znieczulenie": _yn(a.get("anesthesia")),
        "PrzedzialWieku": norm_spacje(a.get("age-range")),
        "Oczekujacy": _int(pd.get("awaiting")),
        "Skresleni": _int(pd.get("removed")),
        "SredniCzasDni": _int(pd.get("average-period")),
        "OkresStatystyk": norm_spacje(pd.get("update")),
        "PCUS": pcus_tekst,
        "PCUSDni": parsuj_pcus(pcus_tekst),
        "PCUSStanNa": _data(daty.get("date-situation-as-at")) if daty else "",
        "TerminDostepny": "" if not daty or daty.get("applicable") is None else ("Y" if daty.get("applicable") else "N"),
        "PierwszyTermin": _data(daty.get("date")) if daty else "",
        "PrognozaNFZDni": _int(cd.get("average-period")),
    }

    lat, lon, _ = popraw_wspolrzedne(a.get("latitude"), a.get("longitude"))
    miejsce = {
        "MiejsceKey": miejsce_key,
        "Swiadczeniodawca": norm_spacje(a.get("provider")),
        "KodSwiadczeniodawcy": norm_spacje(a.get("provider-code")),
        "RegonSwiadczeniodawcy": norm_spacje(a.get("regon-provider")),
        "NipSwiadczeniodawcy": norm_spacje(a.get("nip-provider")),
        "TerytSwiadczeniodawcy": norm_spacje(a.get("teryt-provider")),
        "Miejsce": norm_spacje(a.get("place")),
        "Adres": norm_spacje(a.get("address")),
        "Miejscowosc": norm_spacje(a.get("locality")),
        "Telefon": norm_spacje(a.get("phone")),
        "TerytMiejsca": norm_spacje(a.get("teryt-place")),
        "NrKsiegiRejestrowej": norm_spacje(a.get("registry-number")),
        "KodResortowyVII": norm_spacje(a.get("id-resort-part-VII")),
        "KodResortowyVIII": norm_spacje(a.get("id-resort-part-VIII")),
        "Oddzial": oddzial,
        "Toaleta": _yn(a.get("toilet")),
        "Podjazd": _yn(a.get("ramp")),
        "Parking": _yn(a.get("car-park")),
        "Winda": _yn(a.get("elevator")),
        "Klimatyzacja": _yn(a.get("ac")),
        "DrzwiAutomatyczne": _yn(a.get("automatic-door")),
        "WozkiInwalidzkie": _yn(a.get("wheelchairs")),
        "Korytarze": _yn(a.get("corridors")),
        "TypBudynku": "" if a.get("type-building") is None else str(a.get("type-building")),
        "KomunikacjaPubliczna": norm_spacje(a.get("public-transport-lines")),
        "SzerokoscNFZ": lat,
        "DlugoscNFZ": lon,
    }
    swiadczenie = {
        "SwiadczenieKey": sw_key,
        "Swiadczenie": nazwa_sw,
        "SwiadczenieKlucz": klucz_tekstowy(nazwa_sw),
        "DlaDzieci": dzieci,
    }
    return fakt, miejsce, swiadczenie
