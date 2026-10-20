# Gdzie w Polsce czeka się najkrócej?

Raport Power BI pokazujący dostępność leczenia w ramach NFZ w 380 powiatach: gdzie kolejki są najkrótsze, gdzie popyt przerasta podaż i gdzie brakuje placówek. Dane zbiera co tydzień GitHub Actions z publicznego API NFZ „Terminy leczenia”, a raport czyta je bezpośrednio z tego repozytorium. Dzięki temu odświeżanie w usłudze Power BI działa bez bramy, a strona „Publikuj w sieci Web” jest zawsze aktualna.

Link do raportu: _(wstaw po opublikowaniu)_ · Kod i dane: https://github.com/Proclever/gdzie-czeka-sie-najkrocej

```mermaid
flowchart LR
    A[API NFZ v1.4<br/>Terminy leczenia] -->|co tydzień| B(GitHub Actions<br/>scripts/nfz_snapshot.py)
    C[API NFZ v1.3<br/>współrzędne] -->|raz w miesiącu| B
    D[GUS BDL<br/>ludność, wynagrodzenia] --> B
    B -->|CSV.gz + manifest| E[(repozytorium<br/>data/)]
    F[PRG GUGiK<br/>granice powiatów] -->|mapshaper| E
    E -->|Web.Contents<br/>raw.githubusercontent.com| G[Power BI<br/>model + raport]
    G --> H[Publikuj w sieci Web]
```

## Co zmieniło się względem założeń (październik 2026)

Plan z pliku `założenia.md` powstał przy założeniu, że API zwraca **pierwszy wolny termin**. Tymczasem od **1 lipca 2026** NFZ go nie publikuje i zastąpił go **prognozowanym czasem oczekiwania (PCUŚ)**. Najważniejsze konsekwencje:

| Założenie | Stan faktyczny | Rozwiązanie w projekcie |
|---|---|---|
| `api.nfz.gov.pl/app-itl-api` (v1.3) | NFZ oznaczył v1.3 jako nieaktualne; od 10.07.2026 działa **v1.4** (`apinfz.nfz.gov.pl/app-itl-api-pcus`) | Snapshot pobierany z v1.4 |
| `DniDoTerminu = PierwszyTermin − StanNaDzien` | pole `dates.date` jest puste; v1.4 podaje `dates.pcus`, np. „1 mies. 3 tyg.” | Główna miara to **PCUŚ w dniach** (miesiąc = 30 dni, tydzień = 7). Dodatkowe metryki: średni czas deklarowany przez placówki i czas rozładowania kolejki |
| Szer./dł. geogr. z rekordu | w v1.4 większość rekordów nie ma współrzędnych, a część ma je zamienione | Walidacja i zamiana; uzupełnianie z API v1.3 (raz w miesiącu), a w ostateczności środkiem gminy wg TERYT. v1.3 poprzedza adres cechą ulicy („UL. DŁUGA 5”), więc dopasowanie sprawdza też taki wariant klucza miejsca, a potem ten sam adres w gminie i tę samą komórkę świadczeniodawcy (kody resortowe VII i VIII). Źródło lokalizacji jest widoczne w raporcie |
| PCUŚ opisuje czas oczekiwania w każdej kolejce | ok. 30% kolejek stabilnych i ponad połowa pilnych nie ma pacjentów (0 oczekujących i 0 obsłużonych w ostatnim miesiącu), a NFZ podaje dla nich PCUŚ 0 dni | Flaga `KolejkaAktywna`. Mediany, mapa i rankingi pomijają puste kolejki (mediana dla przypadków stabilnych w październiku 2026: 51 dni zamiast 21). Strona „Dla pacjenta” pokazuje wszystkie placówki |
| Wszystkie kolejki mają termin | kolejki w **centralnej e-rejestracji** (`queue-in-cer = Y`) nie mają PCUŚ ani statystyk | Flaga `WCeR`; udział takich kolejek na stronie „O danych” |
| Wymiary przez `Table.Distinct` z zapytań referencyjnych | w usłudze każde zapytanie referencyjne ponownie pobiera wszystkie snapshoty, a łączenie zapytań z dostępem do sieci uruchamia zaporę prywatności | Wymiary (`miejsca.csv.gz`, `swiadczenia.csv`) buduje skrypt. Power Query czyta każdy plik w jednym samodzielnym zapytaniu, a kategoria świadczenia nadal powstaje w Power Query |
| Miary w filtrach `CALCULATE` | DAX nie pozwala na miarę w filtrze logicznym, a `[W promieniu]` traktowało brak odległości jak 0 km | Miary przepisane na zmienne, a filtr promienia pomija brak współrzędnych |

## Struktura repozytorium

```
.github/workflows/snapshot-nfz.yml   cotygodniowy snapshot (cron) + uruchomienie ręczne
scripts/
  nfz_api.py                 klient API (wspólne tempo, pauza po 429, dogrywka) i spłaszczanie JSON
  nfz_snapshot.py            16 oddziałów × 2 przypadki, stronicowanie → snapshot
  nfz_wspolrzedne_v13.py     współrzędne placówek z API v1.3
  gus_powiaty.py             GUS BDL: ludność, ludność 65+, przeciętne wynagrodzenie
  geo_build.py               granice powiatów (PRG) → TopoJSON + środki gmin
  ustaw_repo.py              wpisuje adres Twojego repozytorium w projekcie Power BI
  zapisz_dane.sh             commit i push folderu data/ (krok workflow)
  common.py                  wspólne funkcje (TERYT, PCUŚ, CSV)
tests/                       testy jednostkowe i end-to-end na odpowiedziach o strukturze API
data/
  nfz/manifest.csv           lista snapshotów (czyta ją Power BI)
  nfz/snapshots/RRRR-MM-DD.csv.gz
  nfz/miejsca.csv.gz         wymiar miejsc udzielania świadczeń
  nfz/swiadczenia.csv        wymiar świadczeń
  gus/powiaty_gus.csv
  geo/powiaty.topojson       mapa do Shape Map (380 powiatów)
  geo/powiaty.csv            lista powiatów z kodami TERYT
  geo/gminy_centroidy.csv    zapasowa lokalizacja placówek
powerbi/
  GdzieNajkrocej.pbip        ← otwierasz ten plik w Power BI Desktop
  GdzieNajkrocej.SemanticModel/   model (TMDL)
  GdzieNajkrocej.Report/          raport (PBIR)
```

## Uruchomienie krok po kroku

**Wymagania:** konto GitHub, Power BI Desktop z **września 2026 lub nowszy** (format PBIP/PBIR jest od tej wersji domyślny), opcjonalnie Python 3.10+.

1. **Utwórz publiczne repozytorium** na GitHubie, np. `gdzie-czeka-sie-najkrocej`, i wgraj do niego zawartość tego folderu (GitHub Desktop: *Add local repository* → *Publish*, z odznaczonym „Keep this code private”).
2. **Wpisz adres repozytorium w projekcie** (przy zamkniętym Power BI Desktop):
   ```
   python scripts/ustaw_repo.py TWOJ-LOGIN/gdzie-czeka-sie-najkrocej
   ```
   Skrypt ustawia parametry Power Query, adres mapy powiatów i linki. Bez Pythona: w Power BI *Przekształć dane → Parametry* oraz Format mapy → *Ustawienia mapy → Adres URL*. Wypchnij zmianę na GitHuba.
3. **Zbierz pierwszy snapshot:** zakładka *Actions* → „Snapshot kolejek NFZ” → *Run workflow* (zaznacz „Odśwież też dane GUS”). API NFZ v1.4 przyjmuje najwyżej 10 zapytań na 10 sekund, więc pierwszy przebieg trwa do ok. 2,5 godziny, a cotygodniowy ok. 2 godzin. Wynik sprawdzisz w `data/nfz/ostatni_przebieg.json`. Kolejne uruchomienia odbywają się automatycznie w poniedziałki rano.
   - Lokalnie zamiast Actions: `pip install -r requirements.txt`, potem `python scripts/gus_powiaty.py`, `python scripts/nfz_wspolrzedne_v13.py`, `python scripts/nfz_snapshot.py` i wypchnięcie folderu `data/`.
4. **Otwórz `powerbi/GdzieNajkrocej.pbip`** i kliknij *Odśwież*. Przy pytaniu o dostęp do `https://raw.githubusercontent.com` wybierz *Anonimowy*.
5. **Sprawdź mapę na stronie „Mapa dostępności”.** Jeśli powiaty się nie rysują, wybierz w formacie mapy *Ustawienia mapy → Typ mapy: mapa niestandardowa* i wskaż plik `data/geo/powiaty.topojson`, a następnie zapisz projekt. Mapa zostanie wtedy osadzona w raporcie i nie będzie zależna od GitHuba.
6. **Opublikuj:** *Publikuj → Mój obszar roboczy*. W ustawieniach modelu semantycznego ustaw poświadczenia źródła na *Anonimowe* z poziomem prywatności *Publiczny* i zaznacz *Pomiń test połączenia*. Zaplanuj odświeżanie (np. poniedziałki 10:00, po workflow). Potem *Plik → Osadź raport → Publikuj w sieci Web*.

> Publikowanie w sieci Web i wizualizację Azure Maps musi dopuszczać administrator dzierżawy Power BI. W kontach uczelnianych bywa to wyłączone. Strona „Dla pacjenta” działa wtedy dalej dzięki tabeli, bez mapy.

## Model danych

Schemat gwiazdy (import):

- **FactKolejki** – ziarno: snapshot × miejsce × świadczenie × przypadek. Zawiera PCUŚ (tekst i dni), oczekujących, skreślonych, średni czas, czas rozładowania oraz flagi `BrakPCUS`, `DaneNieaktualne`, `WCeR`, `WieleMiejsc` i `KolejkaAktywna` (FAŁSZ = kolejka bez pacjentów, pomijana w miarach porównawczych).
- **DimData** – kalendarz oznaczony jako tabela dat.
- **DimPlacowka** – świadczeniodawca i miejsce, adres, telefon, udogodnienia, współrzędne ze źródłem lokalizacji, powiat i województwo.
- **DimSwiadczenie** – nazwa, kategoria (z początku nazwy, w Power Query) i flaga „dla dzieci”.
- **DimPowiat** – 380 powiatów z PRG plus GUS BDL. Relacja 1→* z DimPlacowka, więc powiaty bez placówek („pustynie”) też są widoczne.
- Tabele rozłączne: **ParamPromien** (what-if 5–200 km), **ParamStart** (miejscowości ze środkiem = średnia współrzędnych placówek) i **ParamMetryka** (parametr pól przełączający metrykę mapy i rankingu).
- **Miary** – 50+ miar w folderach wyświetlania, każda z opisem.

Kluczowe miary: `Mediana PCUŚ (aktualnie)`, `P90 PCUŚ (aktualnie)`, `Mediana średniego czasu (aktualnie)`, `Czas rozładowania kolejki (aktualnie)`, `Oczekujący na 10 tys. 65+`, `Placówki na 100 tys.`, `Odległość (km)` (haversine), `W promieniu`, `Najkrótszy PCUŚ w promieniu (dni)`, `Indeks luki`, `Zmiana m/m (dni)` oraz miary tekstowe z wnioskami.

## Strony raportu

1. **Podsumowanie** – 3 karty KPI, 3 wnioski generowane miarami, top 10 świadczeń i mediana wg województw.
2. **Mapa dostępności** – kartogram powiatów z przełączaną metryką, ranking powiatów i histogram PCUŚ.
3. **Dla pacjenta** – miejscowość i promień, tabela placówek w zasięgu od najkrótszego czasu oraz mapa bąbelkowa.
4. **Dla decydenta** – wykres punktowy powiatów (popyt × czas, rozmiar = liczba placówek) z ćwiartkami wyznaczonymi przez mediany. Przejście do strony powiatu.
5. **Dla inwestora** – ranking powiatów wg indeksu luki dla wybranej specjalizacji oraz lista „pustyń”.
6. **Trend** – mediana PCUŚ w kolejnych snapshotach (stabilne i pilne zawsze rozdzielone) i zmiana m/m wg województw.
7. **O danych** – źródła, definicje, wskaźniki jakości danych i porównanie średniej z medianą.
8. **Szczegóły powiatu** (ukryta, drill-through) – placówki i świadczenia w wybranym powiecie.

## Testy i przebudowa mapy

```
python -m unittest discover -s tests -v
python scripts/geo_build.py          # wymaga: pip install shapely, Node.js (mapshaper przez npx)
```

## Źródła danych

- NFZ, „Terminy leczenia” – API v1.4 (`apinfz.nfz.gov.pl/app-itl-api-pcus`) i v1.3 (współrzędne); dane deklarowane przez świadczeniodawców.
- GUS, Bank Danych Lokalnych – zmienne 72305, 72239, 72240 (ludność, P2137), 64428 i 64429 (wynagrodzenia, P2497).
- GUGiK, Państwowy Rejestr Granic – granice powiatów i gmin. Wersja WGS84 z repozytorium [waszkiewiczja/GeoJSON-Polska-Wojewodztwa-Powiaty-Gminy](https://github.com/waszkiewiczja/GeoJSON-Polska-Wojewodztwa-Powiaty-Gminy) (stan z sierpnia 2025), uproszczona w mapshaper.

## Ograniczenia

- PCUŚ to prognoza NFZ, a nie gwarantowany termin. Wartości powyżej 30 dni NFZ podaje w miesiącach i tygodniach, więc dokładność wynosi około tygodnia.
- Ok. 80% miejsc ma dokładne współrzędne z API NFZ (v1.4 lub v1.3). Pozostałe dostają środek gminy, więc odległości na stronie „Dla pacjenta” są dla nich przybliżone.
- Oczekujący są przypisani do powiatu placówki. Duże miasta obsługują też pacjentów z okolicy, co zawyża ich wskaźniki na 10 tys. mieszkańców.
- API NFZ v1.4 ma limit 10 zapytań na 10 sekund, a pełny snapshot to ok. 6 tys. zapytań. Skrypt trzyma równe tempo 0,9 zapytania na sekundę. Po odpowiedzi HTTP 429 wszystkie wątki robią wspólną pauzę, a strony, których nie udało się pobrać, są ponawiane na końcu. Snapshot jest zapisywany, gdy brakuje najwyżej 1% stron. Braki opisuje `data/nfz/ostatni_przebieg.json`.
- Snapshoty przyrastają o kilka MB tygodniowo. Po roku warto rozważyć archiwizację starszych plików albo odświeżanie przyrostowe.
