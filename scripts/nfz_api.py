"""Klient API NFZ „Terminy leczenia” i spłaszczanie rekordów do tabel.

API v1.4 (od 10.07.2026): https://apinfz.nfz.gov.pl/app-itl-api-pcus/queues
  - dates.pcus – prognozowany czas oczekiwania (PCUŚ), tekst, np. "1 mies. 3 tyg."
  - dates.date-situation-as-at – data, na którą policzono PCUŚ
  - queue-in-cer – kolejka prowadzona w centralnej e-rejestracji (brak PCUŚ i statystyk)
  - współrzędne są w większości puste, część ma zamienioną szerokość z długością
  - limit: 10 zapytań na 10 sekund. Zmierzony 19.10.2026 w GitHub Actions: po 10
    zapytaniach serwer odpowiada HTTP 429 z nagłówkiem Retry-After (do 8 s)
API v1.3 (nieaktualne, ale nadal dostępne): https://api.nfz.gov.pl/app-itl-api/queues
  - używane wyłącznie do uzupełnienia współrzędnych placówek
  - adres poprzedzony cechą ulicy („UL. DŁUGA 5”), więc klucz miejsca bywa inny niż w v1.4
    (dopasowanie: warianty_klucza_v13, klucz_adresu, klucz_komorki)
"""

from __future__ import annotations

import email.utils
import logging
import math
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

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
LIMIT_NA_STRONE = 25          # wielkość strony, którą API przyjmuje na pewno
KANDYDACI_LIMITU = (100, 50)  # większe strony sprawdzane na starcie – mniej zapytań, jeśli API je zwraca
TEMPO_V14 = 0.9               # zapytań na sekundę, czyli 9 na 10 s – tuż poniżej limitu API
TEMPO_V13 = 1.8

log = logging.getLogger("nfz")


class BladApi(RuntimeError):
    pass


class Ogranicznik:
    """Wspólne dla wszystkich wątków tempo zapytań.

    * Zapytania są rozłożone równo w czasie, co najmniej `odstep` sekund między kolejnymi.
    * Po HTTP 429 wszystkie wątki czekają tyle, ile każe serwer (Retry-After),
      a tempo spada o 20%, raz na epizod, a nie osobno w każdym wątku.
    * Po długiej serii udanych zapytań tempo stopniowo wraca do bazowego, ale nigdy go nie przekracza.
    """

    def __init__(self, na_sekunde: float, zegar: Callable[[], float] = time.monotonic,
                 uspij: Callable[[float], None] = time.sleep, maks_odstep: float = 6.0,
                 seria_do_przyspieszenia: int = 300) -> None:
        self.odstep_bazowy = 1.0 / max(na_sekunde, 0.01)
        self.odstep = self.odstep_bazowy
        self.maks_odstep = max(maks_odstep, self.odstep_bazowy)
        self.seria_do_przyspieszenia = seria_do_przyspieszenia
        self.zegar = zegar
        self.uspij = uspij
        self.epizody_429 = 0
        self._lock = threading.Lock()
        self._nastepny = float("-inf")
        self._pauza_do = float("-inf")
        self._seria = 0

    @property
    def na_sekunde(self) -> float:
        return 1.0 / self.odstep

    def czekaj(self) -> None:
        """Blokuje wątek do chwili, w której wolno wysłać kolejne zapytanie."""
        while True:
            with self._lock:
                start = max(self.zegar(), self._nastepny, self._pauza_do)
                self._nastepny = start + self.odstep
            opoznienie = start - self.zegar()
            if opoznienie > 0:
                self.uspij(opoznienie)
            with self._lock:
                if self.zegar() >= self._pauza_do:
                    return
            # W trakcie czekania inny wątek dostał 429, więc ustawiamy się w kolejce po pauzie.

    def przy_429(self, retry_after: float | None, domyslna: float = 10.0) -> tuple[float, bool]:
        """Ogłasza wspólną pauzę. Zwraca (sekundy do końca pauzy, czy to nowy epizod)."""
        with self._lock:
            teraz = self.zegar()
            pauza = domyslna if retry_after is None else retry_after
            pauza = min(max(pauza, 1.0), 300.0)
            nowy = teraz >= self._pauza_do
            if nowy:
                self.odstep = min(self.odstep * 1.25, self.maks_odstep)
                self.epizody_429 += 1
            self._pauza_do = max(self._pauza_do, teraz + pauza)
            self._seria = 0
            return self._pauza_do - teraz, nowy

    def wstrzymaj(self, sekundy: float) -> None:
        with self._lock:
            self._pauza_do = max(self._pauza_do, self.zegar() + sekundy)

    def zwolnij(self, mnoznik: float) -> None:
        with self._lock:
            self.odstep = min(self.odstep * mnoznik, self.maks_odstep)
            self._seria = 0

    def sukces(self) -> None:
        with self._lock:
            self._seria += 1
            if self._seria >= self.seria_do_przyspieszenia and self.odstep > self.odstep_bazowy:
                self.odstep = max(self.odstep / 1.1, self.odstep_bazowy)
                self._seria = 0


def _retry_after(odpowiedz) -> float | None:
    """Wartość nagłówka Retry-After w sekundach (liczba albo data HTTP)."""
    naglowki = getattr(odpowiedz, "headers", None) or {}
    wartosc = naglowki.get("Retry-After")
    if wartosc in (None, ""):
        return None
    try:
        return float(wartosc)
    except (TypeError, ValueError):
        pass
    try:
        kiedy = email.utils.parsedate_to_datetime(str(wartosc))
        return max(0.0, (kiedy - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


class KlientNfz:
    """Pobiera strony z API: wspólne tempo dla wątków, wspólna pauza po HTTP 429,
    ponawianie błędów 5xx i błędów sieci z wykładniczym odstępem."""

    KODY_DO_PONOWIENIA = {500, 502, 503, 504}

    def __init__(
        self,
        baza: str = API_V14,
        na_sekunde: float = TEMPO_V14,
        maks_prob: int = 8,
        maks_prob_429: int = 40,
        timeout: float = 60.0,
        sesja=None,
        dodatkowe_parametry: dict | None = None,
        uspij: Callable[[float], None] = time.sleep,
        zegar: Callable[[], float] = time.monotonic,
    ) -> None:
        if sesja is None:
            import requests  # zależność tylko przy prawdziwym pobieraniu

            sesja = requests.Session()
            sesja.headers.update({
                "Accept": "application/json",
                "User-Agent": "gdzie-czeka-sie-najkrocej/1.1 (projekt portfolio; snapshot tygodniowy)",
            })
        self.baza = baza
        self.sesja = sesja
        self.ogranicznik = Ogranicznik(na_sekunde, zegar=zegar, uspij=uspij)
        self.maks_prob = maks_prob
        self.maks_prob_429 = maks_prob_429
        self.timeout = timeout
        self.dodatkowe = dodatkowe_parametry or {}
        self.uspij = uspij
        self.zegar = zegar
        self.statystyki = {"zapytania": 0, "ponowienia": 0, "http_429": 0}
        self._lock = threading.Lock()

    def _licz(self, klucz: str) -> None:
        with self._lock:
            self.statystyki[klucz] += 1

    def pobierz(self, parametry: dict, maks_prob: int | None = None) -> dict:
        params = {"format": "json", **self.dodatkowe, **parametry}
        maks_prob = maks_prob or self.maks_prob
        proby = proby_429 = 0
        while True:
            self.ogranicznik.czekaj()
            self._licz("zapytania")
            odpowiedz = None
            status = None
            try:
                odpowiedz = self.sesja.get(self.baza, params=params, timeout=self.timeout)
            except Exception as e:  # błąd sieci / timeout
                blad = f"{type(e).__name__}: {e}"
            else:
                status = odpowiedz.status_code
                if status == 200:
                    try:
                        dane = odpowiedz.json()
                    except ValueError as e:
                        blad = f"niepoprawny JSON: {e}"
                    else:
                        self.ogranicznik.sukces()
                        return dane
                elif status == 429 or status in self.KODY_DO_PONOWIENIA:
                    blad = f"HTTP {status}"
                else:
                    raise BladApi(f"HTTP {status} dla {params}: {getattr(odpowiedz, 'text', '')[:300]}")

            if status == 429:
                # Limit zapytań to nie awaria: czekamy wspólnie, zwalniamy i próbujemy dalej.
                proby_429 += 1
                self._licz("http_429")
                if proby_429 >= self.maks_prob_429:
                    raise BladApi(f"Nie udało się pobrać {params}: {proby_429} razy z rzędu HTTP 429")
                pauza, nowy = self.ogranicznik.przy_429(_retry_after(odpowiedz))
                if nowy:
                    log.warning("Limit zapytań API (HTTP 429): wspólna pauza %.0f s, dalej %.2f zapytania/s",
                                pauza, self.ogranicznik.na_sekunde)
                continue

            proby += 1
            if proby >= maks_prob:
                raise BladApi(f"Nie udało się pobrać {params} po {maks_prob} próbach: {blad}")
            self._licz("ponowienia")
            czekanie = min(300.0, 2.0 ** proby) + random.uniform(0, 1.5)
            log.warning("Ponawiam (%s/%s) za %.1f s: %s %s", proby, maks_prob, czekanie, blad, params)
            self.uspij(czekanie)

    def kolejki(self, przypadek: int, oddzial: str, limit: int = LIMIT_NA_STRONE,
                maks_stron: int | None = None) -> Iterator[dict]:
        """Iteruje po kolei po wszystkich rekordach pary (przypadek, oddział NFZ). Do użytku doraźnego."""
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
# Pobieranie całych zestawów (oddział × przypadek)
# ---------------------------------------------------------------------------
def ustal_limit(klient: KlientNfz, kandydaci: Iterable[int] = KANDYDACI_LIMITU, oddzial: str = "07",
                przypadek: int = 1) -> int:
    """Największa strona, którą API naprawdę zwraca. Mniej stron oznacza mniej zapytań.

    API może odrzucić duży limit (błąd 4xx) albo po cichu przyciąć stronę do 25 rekordów.
    W obu przypadkach zostajemy przy bezpiecznych 25."""
    for limit in kandydaci:
        try:
            dane = klient.pobierz({"case": przypadek, "province": oddzial, "limit": limit, "page": 1}, maks_prob=2)
        except BladApi as e:
            log.info("Strona po %s rekordów niedostępna: %s", limit, e)
            continue
        liczba = int((dane.get("meta") or {}).get("count") or 0)
        zwrocone = len(dane.get("data") or [])
        if zwrocone > LIMIT_NA_STRONE and zwrocone == min(limit, liczba):
            log.info("API zwraca do %s rekordów na stronę – używam tej wielkości strony.", limit)
            return limit
        log.info("Przy limit=%s API zwróciło %s z %s rekordów – sprawdzam mniejszą stronę.", limit, zwrocone, liczba)
    log.info("Używam stron po %s rekordów.", LIMIT_NA_STRONE)
    return LIMIT_NA_STRONE


@dataclass
class WynikPobierania:
    limit: int
    rekordy_wg_bloku: dict = field(default_factory=dict)        # "07/1" -> meta.count z pierwszej strony
    strony_wg_bloku: dict = field(default_factory=dict)         # "07/1" -> liczba stron
    pobrane_wg_bloku: Counter = field(default_factory=Counter)  # "07/1" -> rekordy faktycznie pobrane
    strony_pobrane: int = 0
    brakujace: list = field(default_factory=list)               # [{"blok", "strona", "blad"}]
    czas_s: float = 0.0

    @property
    def strony_lacznie(self) -> int:
        return sum(self.strony_wg_bloku.values())

    @property
    def bloki_bez_danych(self) -> list[str]:
        return sorted({b["blok"] for b in self.brakujace if b["strona"] == 1})


def pobierz_bloki(
    klient: KlientNfz,
    bloki: list[tuple[str, int]],
    przetworz: Callable[[str, int, list[dict]], None],
    watki: int = 3,
    limit: int = LIMIT_NA_STRONE,
    maks_stron: int | None = None,
    pauzy_dogrywki: tuple[float, ...] = (60.0, 300.0),
) -> WynikPobierania:
    """Pobiera wszystkie strony dla par (oddział, przypadek).

    Bloki idą po kolei, więc stronicowanie jednego bloku trwa krótko. Strony 2..N bloku
    pobierają wątki równolegle, ale we wspólnym tempie klienta. Nieudana strona nie przerywa
    bloku: trafia do dogrywki na końcu, po pauzie i w wolniejszym tempie.
    `przetworz(oddzial, przypadek, rekordy)` jest wywoływane w wątku głównym."""
    wynik = WynikPobierania(limit=limit)
    zegar = klient.zegar
    start = zegar()

    def zapytanie(oddzial: str, przypadek: int, strona: int) -> dict:
        return klient.pobierz({"case": przypadek, "province": oddzial, "limit": limit, "page": strona})

    def obsluz(oddzial: str, przypadek: int, dane: dict) -> None:
        rekordy = dane.get("data") or []
        wynik.strony_pobrane += 1
        wynik.pobrane_wg_bloku[f"{oddzial}/{przypadek}"] += len(rekordy)
        przetworz(oddzial, przypadek, rekordy)

    def pobierz_blok(pula: ThreadPoolExecutor, oddzial: str, przypadek: int) -> list[tuple[str, int, int, str]]:
        blok = f"{oddzial}/{przypadek}"
        try:
            pierwsza = zapytanie(oddzial, przypadek, 1)
        except BladApi as e:
            log.error("Blok %s: nie udało się pobrać pierwszej strony: %s", blok, e)
            return [(oddzial, przypadek, 1, str(e))]
        liczba = int((pierwsza.get("meta") or {}).get("count") or 0)
        stron = max(1, math.ceil(liczba / limit))
        if maks_stron:
            stron = min(stron, maks_stron)
        wynik.rekordy_wg_bloku[blok] = liczba
        wynik.strony_wg_bloku[blok] = stron
        obsluz(oddzial, przypadek, pierwsza)
        nieudane = []
        przyszle = {pula.submit(zapytanie, oddzial, przypadek, s): s for s in range(2, stron + 1)}
        for f in as_completed(przyszle):
            strona = przyszle[f]
            try:
                dane = f.result()
            except BladApi as e:
                nieudane.append((oddzial, przypadek, strona, str(e)))
                log.warning("Blok %s, strona %s: %s – ponowię na końcu.", blok, strona, e)
                continue
            obsluz(oddzial, przypadek, dane)
        return nieudane

    nieudane: list[tuple[str, int, int, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, watki)) as pula:
        for nr, (oddzial, przypadek) in enumerate(bloki, 1):
            blok = f"{oddzial}/{przypadek}"
            t0, z0 = zegar(), klient.statystyki["zapytania"]
            nieudane += pobierz_blok(pula, oddzial, przypadek)
            dt = zegar() - t0
            tempo = f"{(klient.statystyki['zapytania'] - z0) / dt:.2f}" if dt >= 1 else "–"
            log.info("Blok %s/%s (oddział %s, przypadek %s): %s rekordów, %s stron, %.0f s, tempo %s zapytania/s, "
                     "HTTP 429 dotąd: %s", nr, len(bloki), oddzial, przypadek, wynik.rekordy_wg_bloku.get(blok, "?"),
                     wynik.strony_wg_bloku.get(blok, "?"), dt, tempo, klient.statystyki["http_429"])
        for runda, pauza in enumerate(pauzy_dogrywki, 1):
            if not nieudane:
                break
            log.warning("Dogrywka %s: %s stron do ponowienia – wspólna pauza %.0f s, potem wolniejsze tempo.",
                        runda, len(nieudane), pauza)
            klient.ogranicznik.wstrzymaj(pauza)
            klient.ogranicznik.zwolnij(1.5)
            do_ponowienia, nieudane = nieudane, []
            for oddzial, przypadek, strona, _ in do_ponowienia:
                if strona == 1:
                    nieudane += pobierz_blok(pula, oddzial, przypadek)
                    continue
                try:
                    dane = zapytanie(oddzial, przypadek, strona)
                except BladApi as e:
                    nieudane.append((oddzial, przypadek, strona, str(e)))
                    continue
                obsluz(oddzial, przypadek, dane)
            if not nieudane:
                log.info("Dogrywka %s: wszystkie strony pobrane.", runda)

    wynik.brakujace = [{"blok": f"{o}/{c}", "strona": s, "blad": b} for o, c, s, b in nieudane]
    wynik.czas_s = round(zegar() - start, 1)
    if not maks_stron:
        for blok, liczba in wynik.rekordy_wg_bloku.items():
            if liczba and wynik.pobrane_wg_bloku[blok] < 0.98 * liczba:
                log.warning("Blok %s: pobrano %s z %s rekordów.", blok, wynik.pobrane_wg_bloku[blok], liczba)
    return wynik


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


# ---------------------------------------------------------------------------
# Dopasowanie miejsc z API v1.3 (współrzędne) do miejsc z v1.4
# ---------------------------------------------------------------------------
# API v1.3 poprzedza adres cechą ulicy z TERYT („UL. DŁUGA 5”), a v1.4 nie („DŁUGA 5”). Klucz miejsca
# z obu wersji różni się wtedy, choć to ta sama placówka – w danych z 19–20.10.2026 dotyczyło to
# ok. 17 tys. z 50 tys. miejsc. Część miejsc v1.3 podaje też samo miasto zamiast dzielnicy
# („WROCŁAW” zamiast „WROCŁAW-KRZYKI”).
PREFIKSY_ADRESU_V13 = ("UL.", "AL.", "PL.", "OS.", "WYB.", "BULW.", "RONDO", "PARK", "DROGA", "SKWER", "RYNEK", "SZOSA")
_CECHY_ULIC = {
    "UL", "ULICA", "AL", "ALEJA", "ALEJE", "PL", "PLAC", "OS", "OSIEDLE", "WYB", "WYBRZEŻE", "BULW", "BULWAR",
    "RONDO", "PARK", "DROGA", "SKWER", "RYNEK", "SZOSA", "OGRÓD", "WYSPA",
}
_POLA_KLUCZA_MIEJSCA = (
    "KodSwiadczeniodawcy", "NrKsiegiRejestrowej", "KodResortowyVII", "KodResortowyVIII",
    "TerytMiejsca", "Adres", "Miejscowosc",
)  # te same pola i ta sama kolejność co w klucz_miejsca()


def norm_adres(adres: str | None) -> str:
    """Adres do porównań między wersjami API: bez cechy ulicy na początku i bez kropek.

    'UL. GEN. JÓZEFA BEMA 5' -> 'GEN JÓZEFA BEMA 5'; 'PL. PLAC WOLNOŚCI 1' -> 'WOLNOŚCI 1'.
    """
    tokeny = klucz_tekstowy(adres).replace(".", " ").replace(",", " ").split()
    while len(tokeny) > 1 and tokeny[0] in _CECHY_ULIC:
        tokeny = tokeny[1:]
    return " ".join(tokeny)


def norm_miejscowosc(miejscowosc: str | None) -> str:
    """Pierwszy człon nazwy: 'KRAKÓW-KROWODRZA' -> 'KRAKÓW', 'WARSZAWA ŚRÓDMIEŚCIE' -> 'WARSZAWA'."""
    czlony = klucz_tekstowy(miejscowosc).replace("-", " ").split()
    return czlony[0] if czlony else ""


def klucz_adresu(m: dict) -> str:
    """Zapasowy klucz: ten sam adres w tej samej gminie (TERYT bez cyfry rodzaju gminy). Pusty, gdy brak danych."""
    gmina = (m.get("TerytMiejsca") or "")[:6]
    adres = norm_adres(m.get("Adres"))
    if len(gmina) < 6 or not adres:
        return ""
    return hash_klucz("ADRES", gmina, norm_miejscowosc(m.get("Miejscowosc")), adres, dlugosc=12)


def klucz_komorki(m: dict) -> str:
    """Zapasowy klucz: ta sama komórka organizacyjna świadczeniodawcy (kody resortowe VII i VIII) w tej samej gminie."""
    gmina = (m.get("TerytMiejsca") or "")[:6]
    if not m.get("KodSwiadczeniodawcy") or not m.get("KodResortowyVII") or len(gmina) < 6:
        return ""
    return hash_klucz("KOMORKA", m.get("KodSwiadczeniodawcy"), m.get("NrKsiegiRejestrowej"),
                      m.get("KodResortowyVII"), m.get("KodResortowyVIII"), gmina, dlugosc=12)


def warianty_klucza_v13(m: dict) -> Iterator[str]:
    """Klucze, jakie to samo miejsce dostałoby w API v1.3 (adres poprzedzony cechą ulicy)."""
    adres = m.get("Adres") or ""
    if not adres:
        return
    pola = [m.get(p) or "" for p in _POLA_KLUCZA_MIEJSCA]
    for prefiks in PREFIKSY_ADRESU_V13:
        pola[5] = f"{prefiks} {adres}"
        yield hash_klucz(*pola, dlugosc=16)


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
