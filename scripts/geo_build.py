"""Buduje dane geograficzne projektu z granic PRG (GUGiK).

Wynik (katalog data/geo):
  powiaty.csv            – 380 powiatów: TERYT, nazwa, województwo, klucz mapy, środek
  gminy_centroidy.csv    – punkt wewnątrz każdej gminy (zapasowa lokalizacja placówek)
  powiaty.topojson       – uproszczona mapa do wizualizacji Shape Map w Power BI
  powiaty_uproszczone.geojson – ta sama geometria w GeoJSON (przypisywanie punktów do powiatów)

Źródło domyślne: repozytorium waszkiewiczja/GeoJSON-Polska-Wojewodztwa-Powiaty-Gminy
(granice z Geoportalu/PRG, sierpień 2025, przeliczone do WGS84, pola JPT_KOD_JE i JPT_NAZWA_).
Można podać własne pliki GeoJSON wyeksportowane z PRG (np. po konwersji w mapshaper):
    python scripts/geo_build.py --powiaty powiaty.geojson --gminy gminy.geojson

Wymaga: shapely oraz Node.js (mapshaper uruchamiany przez npx).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GEO_DIR, WOJEWODZTWA, zapisz_csv  # noqa: E402

ZRODLO = "https://raw.githubusercontent.com/waszkiewiczja/GeoJSON-Polska-Wojewodztwa-Powiaty-Gminy/main/"


def wczytaj_geojson(sciezka_lub_url: str) -> dict:
    if sciezka_lub_url.startswith("http"):
        with urllib.request.urlopen(sciezka_lub_url, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads(Path(sciezka_lub_url).read_text(encoding="utf-8"))


def nazwa_powiatu(jpt_nazwa: str, teryt: str) -> str:
    """'powiat kozienicki' -> 'kozienicki'; miasta na prawach powiatu -> 'm. Kraków'."""
    nazwa = jpt_nazwa.strip()
    if nazwa.lower().startswith("powiat "):
        nazwa = nazwa[7:].strip()
    if int(teryt[2:]) >= 61:
        return "m. " + nazwa
    return nazwa


def punkt_srodka(geom):
    """Środek ciężkości, a gdy wypada poza obszarem – punkt reprezentatywny."""
    c = geom.centroid
    return c if geom.contains(c) else geom.representative_point()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--powiaty", default=ZRODLO + "powiaty.json")
    p.add_argument("--gminy", default=ZRODLO + "gminy.json")
    p.add_argument("--uproszczenie", default="6%", help="parametr -simplify dla mapshaper (domyślnie 6%%)")
    a = p.parse_args()

    from shapely.geometry import shape  # import tutaj, żeby --help działało bez shapely

    GEO_DIR.mkdir(parents=True, exist_ok=True)

    # --- powiaty -----------------------------------------------------------
    pw = wczytaj_geojson(a.powiaty)
    feats = pw["features"]
    if len(feats) != 380:
        print(f"UWAGA: oczekiwano 380 powiatów, jest {len(feats)}", file=sys.stderr)

    nazwy = {f["properties"]["JPT_KOD_JE"]: nazwa_powiatu(f["properties"]["JPT_NAZWA_"], f["properties"]["JPT_KOD_JE"]) for f in feats}
    licznik = Counter(nazwy.values())

    wiersze = []
    for f in feats:
        teryt = f["properties"]["JPT_KOD_JE"]
        woj = WOJEWODZTWA[teryt[:2]]
        nazwa = nazwy[teryt]
        etykieta = nazwa if nazwa.startswith("m. ") else "pow. " + nazwa
        if licznik[nazwa] > 1:
            etykieta = f"{etykieta} ({woj})"
        g = shape(f["geometry"])
        srodek = punkt_srodka(g)
        wiersze.append({
            "TerytPowiat": teryt,
            "NazwaPowiatu": nazwa,
            "Powiat": etykieta,
            "TerytWojewodztwa": teryt[:2],
            "Wojewodztwo": woj,
            "TypPowiatu": "miasto na prawach powiatu" if int(teryt[2:]) >= 61 else "powiat ziemski",
            "KluczMapy": "PL" + teryt,
            "SzerokoscSrodka": round(srodek.y, 5),
            "DlugoscSrodka": round(srodek.x, 5),
        })
        f["properties"] = {"klucz": "PL" + teryt, "teryt": teryt, "nazwa": etykieta, "wojewodztwo": woj}
    wiersze.sort(key=lambda w: w["TerytPowiat"])
    kolumny = ["TerytPowiat", "NazwaPowiatu", "Powiat", "TerytWojewodztwa", "Wojewodztwo",
               "TypPowiatu", "KluczMapy", "SzerokoscSrodka", "DlugoscSrodka"]
    n = zapisz_csv(GEO_DIR / "powiaty.csv", wiersze, kolumny)
    assert len({w["Powiat"] for w in wiersze}) == n, "etykiety powiatów muszą być unikalne"
    print(f"powiaty.csv: {n} wierszy")

    # --- TopoJSON (mapshaper) ------------------------------------------------
    npx = shutil.which("npx")
    if not npx:
        sys.exit("Brak Node.js/npx – zainstaluj Node.js, aby zbudować TopoJSON (mapshaper).")
    with tempfile.TemporaryDirectory() as tmp:
        wej = Path(tmp) / "powiaty.json"
        wej.write_text(json.dumps(pw, ensure_ascii=False), encoding="utf-8")
        cel_topo = GEO_DIR / "powiaty.topojson"
        cel_geo = GEO_DIR / "powiaty_uproszczone.geojson"
        bazowe = [npx, "--yes", "mapshaper", "-i", str(wej), "name=powiaty",
                  "-simplify", a.uproszczenie, "weighted", "keep-shapes", "-clean"]
        subprocess.run(bazowe + ["-o", str(cel_topo), "format=topojson", "quantization=100000", "precision=0.00001"], check=True)
        subprocess.run(bazowe + ["-o", str(cel_geo), "format=geojson", "precision=0.00001"], check=True)
    print(f"powiaty.topojson: {cel_topo.stat().st_size / 1024:.0f} KB")

    # --- gminy: punkty środka ----------------------------------------------
    gm = wczytaj_geojson(a.gminy)
    wiersze_g = []
    for f in gm["features"]:
        kod = f["properties"]["JPT_KOD_JE"]
        g = shape(f["geometry"])
        s = punkt_srodka(g)
        wiersze_g.append({
            "TerytGminy": kod,
            "NazwaGminy": f["properties"].get("JPT_NAZWA_", ""),
            "TerytPowiat": kod[:4],
            "Szerokosc": round(s.y, 5),
            "Dlugosc": round(s.x, 5),
        })
    wiersze_g.sort(key=lambda w: w["TerytGminy"])
    n = zapisz_csv(GEO_DIR / "gminy_centroidy.csv", wiersze_g, ["TerytGminy", "NazwaGminy", "TerytPowiat", "Szerokosc", "Dlugosc"])
    print(f"gminy_centroidy.csv: {n} wierszy")


if __name__ == "__main__":
    main()
