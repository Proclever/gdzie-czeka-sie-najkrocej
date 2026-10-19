#!/usr/bin/env bash
# Zapisuje zmiany w folderze data/ jako commit i wypycha je do repozytorium.
# Uruchamiany przez GitHub Actions na końcu przebiegu – także po błędzie albo
# przerwaniu, żeby nie przepadły dane pobrane we wcześniejszych krokach (GUS, współrzędne).
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add data
if git diff --cached --quiet; then
  echo "Brak zmian w danych."
  exit 0
fi

dzis="$(TZ=Europe/Warsaw date +%F)"
if git diff --cached --name-only | grep -q '^data/nfz/snapshots/'; then
  opis="Snapshot NFZ ${dzis}"
else
  opis="Dane pomocnicze NFZ/GUS ${dzis} (bez nowego snapshotu)"
fi
git commit -q -m "${opis}"

# Przebieg trwa ok. 2 godzin – w tym czasie ktoś mógł wypchnąć zmiany do repozytorium.
galaz="${GITHUB_REF_NAME:-main}"
for proba in 1 2 3; do
  if git pull --rebase -q origin "${galaz}" && git push -q origin "HEAD:${galaz}"; then
    echo "Wypchnięto: ${opis}"
    exit 0
  fi
  echo "Wypchnięcie nieudane (próba ${proba}/3) – ponawiam za 15 s."
  sleep 15
done
echo "Nie udało się wypchnąć danych." >&2
exit 1
