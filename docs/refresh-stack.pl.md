# Instalacja i aktualizacja instancji HBF MCP

Jako root:

```bash
git clone https://github.com/fronczek/hbf-mcp.git
cd hbf-mcp
./install
./install artur afronczek
./install mariusz mhejczyk
```

| Polecenie | Działanie |
| --- | --- |
| ./install | Lista instancji, kont, portów, stanu usług i katalogów |
| ./install artur afronczek | Instalacja lub upgrade hbf-mcp-artur jako afronczek |
| ./install mariusz mhejczyk | Instalacja lub upgrade hbf-mcp-mariusz jako mhejczyk |

Bez argumentów skrypt niczego nie instaluje ani nie restartuje.

## Katalogi i porty

Każda instancja ma własny runtime, venv i .env:
- /opt/hbf-mcp-artur/.env
- /opt/hbf-mcp-mariusz/.env

Pierwsza instancja dostaje 8765, następne 8766, 8767 itd. Porty zapisane
w innych instancjach i porty aktualnie zajęte są pomijane. Aktualizacja zachowuje
dotychczasowy port. Jednoczesne instalacje są blokowane przez flock.
Opcjonalnie administrator może jawnie wskazać port: ./install artur afronczek --port 8770.

Instalator sprawdza lub tworzy konto podane w drugim argumencie i odczytuje
jego rzeczywisty katalog domowy oraz grupę podstawową. Konto root jest odrzucane.
Jedno konto przypisane jest do jednej instancji. Zmiana konta istniejącej
instancji nie jest traktowana jako zwykły upgrade.

Ścieżki helperów, credentials, stanu Gitea, repozytoriów i testów przeglądarkowych
wynikają z katalogu domowego danego konta. Instalator nie kopiuje credentials
ani repozytoriów między użytkownikami. Integracje zewnętrzne wymagają własnych
istniejących plików w credentials danego konta.

## Upgrade

W tym samym checkout:

```bash
git pull --ff-only
./install artur afronczek
./install mariusz mhejczyk
```

Każde polecenie aktualizuje tylko wskazaną instancję:
- tworzy prywatny backup runtime/venv, lokalnego .env, jednostki i helperów
  w /var/backups/hbf-mcp-NAZWA;
- sprawdza zależności i składnię;
- instaluje kod i oba helpery;
- zachowuje treść istniejącego .env i dane w katalogu domowym;
- restartuje i sprawdza właściwą usługę.

Sam .env jest root:root, 0600; systemd wczytuje go przed uruchomieniem procesu
jako wskazany użytkownik. Jednostka systemd jest zarządzana przez instalator.
Lokalne dostosowania usługi trzymaj w drop-inach systemd.
Instalacja nie jest atomowa i nie cofa automatycznie pakietów apt.

install.sh pozostaje aliasem zgodności: bash ./install.sh artur afronczek.
Bez argumentów również tylko wyświetla instancje.

## Service i autocomplete

```bash
service hbf-mcp-artur start
service hbf-mcp-artur stop
service hbf-mcp-artur restart
service hbf-mcp-artur status
```

Instalator dodaje completion do /etc/bash_completion.d/hbf-mcp.
W nowej sesji Bash działa automatycznie; w bieżącej sesji wczytaj raz:

```bash
source /etc/bash_completion.d/hbf-mcp
```

Dla service hbf-mcp-* podpowiadane są start, stop, restart i status.
Uzupełnianie pozostałych usług jest delegowane do standardowego completion.

## Obecna nienazwana instalacja

Stara hbf-mcp jest widoczna w ./install i jej port jest uwzględniany przy
wyborze wolnego portu. Instalator nowych instancji nie wyłącza jej ani nie
przepisuje jej konfiguracji na inne konto. Jeżeli zajmuje 8765, nowa instancja
dostanie 8766.

Nowe instancje nie korzystają z /etc/hbf-mcp.env. Stary plik służy wyłącznie
do odczytu portu istniejącej starej instalacji. Przełączenie połączenia ChatGPT
lub proxy na nową usługę i późniejsze wycofanie starej instalacji to osobny
krok po sprawdzeniu nowej instancji.

## ChatGPT i diagnostyka

Po zmianie metadanych: Plugins → obecne połączenie → Refresh, potem nowa rozmowa.
Dla nowego adresu/portu skonfiguruj połączenie/proxy do odpowiedniej instancji.

```bash
journalctl -u hbf-mcp-artur -n 30 --no-pager
sudo -u afronczek env HBF_MCP_TEST_URL=http://127.0.0.1:8765/mcp \
  /opt/hbf-mcp-artur/.venv/bin/python /opt/hbf-mcp-artur/test_client.py
```

Użyj portu pokazanego przez ./install. Klient sprawdza zgodność kodu procesu
z plikami na dysku i zwraca błąd dla nieaktualnego runtime.

## Rollback

Instalator wypisuje dokładny katalog kopii. Dla instancji artur, jako root:

```bash
backup_dir=/var/backups/hbf-mcp-artur/KOPIA-WYPISANA-PRZEZ-INSTALATOR
test -d "$backup_dir/runtime/.venv" || exit 1
service hbf-mcp-artur stop
cp -a "$backup_dir/runtime/." /opt/hbf-mcp-artur/
cp -a "$backup_dir/hbf-mcp.service" /etc/systemd/system/hbf-mcp-artur.service
for helper in zabbix-watch kuk-boiler-watch; do
  if [[ -f "$backup_dir/$helper" ]]; then
    cp -a "$backup_dir/$helper" "/home/afronczek/bin/$helper"
  fi
done
systemctl daemon-reload
service hbf-mcp-artur start
```

Dostosuj home do konta pokazanego przez getent passwd afronczek.
Rollback nie usuwa dodatkowych nowych plików ani zmian pakietów systemowych.
