"""Wspólne funkcje dla skryptów projektu „Gdzie w Polsce czeka się najkrócej”.

Moduł nie ma zależności poza biblioteką standardową, żeby testy i skrypty
dało się uruchomić na każdym Pythonie 3.10+.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import math
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:  # strefa czasowa jest potrzebna tylko do ustalenia daty snapshotu
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

# ---------------------------------------------------------------------------
# Ścieżki
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NFZ_DIR = DATA / "nfz"
SNAPSHOT_DIR = NFZ_DIR / "snapshots"
GUS_DIR = DATA / "gus"
GEO_DIR = DATA / "geo"

# ---------------------------------------------------------------------------
# Słowniki terytorialne
# ---------------------------------------------------------------------------
# Kody województw TERYT (2 znaki) -> nazwa
WOJEWODZTWA = {
    "02": "dolnośląskie",
    "04": "kujawsko-pomorskie",
    "06": "lubelskie",
    "08": "lubuskie",
    "10": "łódzkie",
    "12": "małopolskie",
    "14": "mazowieckie",
    "16": "opolskie",
    "18": "podkarpackie",
    "20": "podlaskie",
    "22": "pomorskie",
    "24": "śląskie",
    "26": "świętokrzyskie",
    "28": "warmińsko-mazurskie",
    "30": "wielkopolskie",
    "32": "zachodniopomorskie",
}

# Oddziały wojewódzkie NFZ mają kody 01–16 w kolejności alfabetycznej województw,
# więc kod NFZ n odpowiada kodowi TERYT 2n.
ODDZIALY_NFZ = {f"{i:02d}": f"{2 * i:02d}" for i in range(1, 17)}

PRZYPADKI = {1: "Stabilny", 2: "Pilny"}

# Prostokąt obejmujący Polskę z zapasem (do walidacji współrzędnych)
LAT_MIN, LAT_MAX = 49.0, 54.9
LON_MIN, LON_MAX = 14.1, 24.2


def teryt_woj_z_oddzialu(kod_oddzialu: str) -> str:
    return ODDZIALY_NFZ[kod_oddzialu]


# ---------------------------------------------------------------------------
# Daty
# ---------------------------------------------------------------------------
def dzis_w_polsce() -> date:
    """Data „dzisiaj” w strefie Europe/Warsaw (GitHub Actions działa w UTC)."""
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Europe/Warsaw")).date()
    return date.today()  # pragma: no cover


# ---------------------------------------------------------------------------
# Tekst
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def norm_spacje(tekst: str | None) -> str:
    """Usuwa nadmiarowe spacje; None -> ''."""
    if tekst is None:
        return ""
    return _WS.sub(" ", str(tekst)).strip()


def klucz_tekstowy(tekst: str | None) -> str:
    """Postać do porównań: bez nadmiarowych spacji, wielkie litery, NFC."""
    return unicodedata.normalize("NFC", norm_spacje(tekst)).upper()


# Skróty, które po zamianie na „zdanie” mają zostać wielkimi literami
SKROTY = {
    "NFZ", "HIV", "AIDS", "POZ", "RTG", "TK", "MR", "MRI", "USG", "EEG", "EKG",
    "EMG", "SOR", "OIOM", "OIT", "ZOL", "ZPO", "DDOM", "COVID", "COVID-19",
    "ECMO", "WZW", "ADHD", "CZP", "TBC", "PET", "SPECT", "PET-CT", "PET-TK",
    "IVF", "ICD", "CRT", "DOM", "HBO", "AOS", "LSD", "DPS",
}
_RZYMSKIE = re.compile(r"^(?=[IVXLC]+$)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def zdanie(tekst: str) -> str:
    """'PORADNIA KARDIOLOGICZNA DLA DZIECI' -> 'Poradnia kardiologiczna dla dzieci'.

    Zachowuje skróty (NFZ, USG…) i liczby rzymskie (II, IV…).
    """
    tekst = norm_spacje(tekst)
    if not tekst:
        return tekst
    tokens = re.split(r"(\s+|[()/,.;:])", tekst)
    out = []
    for tok in tokens:
        gole = tok.strip()
        if gole and (gole.upper() in SKROTY or (len(gole) > 1 and _RZYMSKIE.match(gole.upper()))):
            out.append(gole.upper())
        else:
            out.append(tok.lower())
    wynik = "".join(out)
    return wynik[:1].upper() + wynik[1:]


def hash_klucz(*czesci: object, dlugosc: int = 16) -> str:
    """Stabilny, krótki klucz tekstowy z kilku pól (SHA-1, hex)."""
    tekst = "|".join(klucz_tekstowy("" if c is None else str(c)) for c in czesci)
    return hashlib.sha1(tekst.encode("utf-8")).hexdigest()[:dlugosc]


# ---------------------------------------------------------------------------
# PCUŚ – prognozowany czas oczekiwania
# ---------------------------------------------------------------------------
# NFZ podaje PCUŚ jako tekst: w dniach do 30 dni, powyżej w miesiącach i tygodniach,
# np. "14 dni", "1 mies. 3 tyg.", "11 mies.". Miesiąc liczymy jako 30 dni
# (ta sama granica, od której NFZ przechodzi na miesiące), tydzień jako 7 dni.
_JEDNOSTKI_PCUS = (
    (re.compile(r"(\d+)\s*(?:lat[a]?|rok(?:u|i)?|r\.)(?![a-ząćęłńóśźż])"), 365),
    (re.compile(r"(\d+)\s*(?:mies(?:\.|iąc[a-z]*|ięcy|iące)?|m-c[ey]?|mc)(?![a-ząćęłńóśźż])"), 30),
    (re.compile(r"(\d+)\s*(?:tydz(?:\.|ień)?|tyg(?:\.|odni[ea]?)?)(?![a-ząćęłńóśźż])"), 7),
    (re.compile(r"(\d+)\s*(?:dni|dzień|dnia|d\.)(?![a-ząćęłńóśźż])"), 1),
)


def parsuj_pcus(tekst: str | None) -> int | None:
    """Zamienia tekst PCUŚ na liczbę dni. Zwraca None, gdy nie da się odczytać."""
    if tekst is None:
        return None
    s = norm_spacje(tekst).lower()
    if not s:
        return None
    if re.fullmatch(r"\d+", s):
        return int(s)
    suma, znaleziono = 0, False
    for wzorzec, mnoznik in _JEDNOSTKI_PCUS:
        for m in wzorzec.finditer(s):
            suma += int(m.group(1)) * mnoznik
            znaleziono = True
    return suma if znaleziono else None


# ---------------------------------------------------------------------------
# Geografia
# ---------------------------------------------------------------------------
def w_polsce(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and LAT_MIN <= lat <= LAT_MAX
        and LON_MIN <= lon <= LON_MAX
    )


def popraw_wspolrzedne(lat, lon) -> tuple[float | None, float | None, str]:
    """Waliduje współrzędne z NFZ. Zwraca (lat, lon, opis).

    Część rekordów API v1.4 ma zamienioną szerokość z długością – wtedy je odwracamy.
    """
    try:
        lat = float(lat) if lat is not None and lat != "" else None
        lon = float(lon) if lon is not None and lon != "" else None
    except (TypeError, ValueError):
        return None, None, ""
    if lat is None or lon is None:
        return None, None, ""
    if w_polsce(lat, lon):
        return lat, lon, "NFZ"
    if w_polsce(lon, lat):
        return lon, lat, "NFZ (zamienione)"
    return None, None, ""


def odleglosc_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Odległość po kole wielkim (wzór haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def kandydaci_gminy(teryt7: str) -> list[str]:
    """Kody gmin z PRG, do których można przypisać TERYT miejsca z NFZ.

    PRG zawiera gminy typu 1/2/3. NFZ podaje też typy 4/5 (miasto/obszar wiejski
    w gminie miejsko-wiejskiej) oraz 8/9 (dzielnice Warszawy, delegatury).
    """
    t = (teryt7 or "").strip()
    if not re.fullmatch(r"\d{7}", t):
        return []
    typ = t[6]
    kandydaci = [t]
    if typ in "45":
        kandydaci.append(t[:6] + "3")
    if typ in "89":
        kandydaci.append(t[:4] + "011")
    return kandydaci


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def zapisz_csv(sciezka: Path, wiersze: Iterable[Mapping[str, object]], kolumny: Sequence[str]) -> int:
    """Zapisuje CSV (UTF-8, przecinek, \\n). Plik .gz jest kompresowany
    deterministycznie (mtime=0), żeby git widział zmiany tylko w treści."""
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    bufor = io.StringIO()
    w = csv.DictWriter(bufor, fieldnames=list(kolumny), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    n = 0
    for wiersz in wiersze:
        w.writerow({k: _na_tekst(wiersz.get(k)) for k in kolumny})
        n += 1
    dane = bufor.getvalue().encode("utf-8")
    tmp = sciezka.with_suffix(sciezka.suffix + ".tmp")
    if sciezka.suffix == ".gz":
        with open(tmp, "wb") as f:
            with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as gz:
                gz.write(dane)
    else:
        tmp.write_bytes(dane)
    os.replace(tmp, sciezka)
    return n


def wczytaj_csv(sciezka: Path) -> list[dict[str, str]]:
    if not sciezka.exists():
        return []
    if sciezka.suffix == ".gz":
        with gzip.open(sciezka, "rt", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    with open(sciezka, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _na_tekst(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return str(v)
