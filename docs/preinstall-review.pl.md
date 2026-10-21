# Przegląd przed wdrożeniem — 2026-10-21

Baza: master 3041bfa po scaleniu PR #6 i #7. PR #5 zamknięty.
Przegląd obejmuje źródła MCP, instalator, zależności, odczyty/zapisy Gitea,
diagnostykę, testy HTTP i instrukcję wdrożenia. Nie jest pełnym audytem
bezpieczeństwa ani testem każdej integracji zewnętrznej.

## Naprawione problemy

1. Instalator kopiował runtime przed zakończeniem pip install.
   Teraz najpierw instaluje zależności z pliku ŹRÓDŁOWEGO, uruchamia pip check
   i kompilację składni wszystkich modułów/helpera, a potem kopiuje kod.
   To ogranicza ryzyko pozostawienia częściowo podmienionych źródeł.
   Nie jest to wdrożenie atomowe: apt i pip mogą zmienić zależności przed błędem.
   Instalator sam tworzy prywatny backup przed zmianami, w /var/backups/hbf-mcp.
   Upgrade to git pull --ff-only i bash ./install.sh; konfiguracja i dane pozostają.
2. requirements.txt dopuszczał dowolne mcp 2.x.
   Przypięto mcp[cli]==2.3.0, czyli wersję obecną na MCP01 i sprawdzoną testami.
   Nie jest to pełny lock zależności przechodnich.
3. gitea_operation_status, oznaczony read-only, otwierał bazę przez _db(),
   co mogło tworzyć katalog, plik i schemat. Teraz brak bazy zwraca not_found
   bez zapisu, a istniejąca baza otwierana jest w trybie SQLite mode=ro.
   Uszkodzenie bazy daje błąd, nie zgodę na powtórzenie niepewnego zapisu.
4. Brak pliku runtime podczas mcp_health powodował wyjątek zamiast diagnozy.
   Teraz zwracane są runtime_sources_readable=false, pusty hash dysku
   i restart_required=true. Brak wymaganych źródeł przy samym starcie nadal
   jest błędem uruchomienia, a nie stanem zdrowym.
5. Test wdrożenia wyświetlał wynik, ale nie potwierdzał zgodności wersji.
   Teraz sprawdza błędy info/health i restart_required=false; w razie błędu
   kończy się kodem niezerowym.
6. Diagnostyka używa jawnego typu dict[str, Any], żeby SDK zwracało
   structuredContent. Test klienta przez prawdziwy HTTP wykrył wcześniejszy
   brak tej struktury.

## DEV i publikacja

Nie ma w tym repo przełącznika DEV/PROD sterującego statusem aplikacji ChatGPT.
Etykieta DEV dotyczy połączenia/wtyczki po stronie ChatGPT, nie usługi systemd
ani automatycznie serwera produkcyjnego/testowego w naszej infrastrukturze.
Nie udało się odczytać konkretnego rekordu tej wtyczki przez dostępne
wyszukiwanie zarządzania pluginami; nie zmieniano jej publikacji ani odbiorców.

Dokumentacja rozróżnia:
- bezpośrednie własne połączenie MCP: restart serwera, Plugins → Refresh,
  potwierdzenie metadanych, nowa rozmowa;
- publikację do katalogu własnego workspace: osobne uprawnienie do publikacji
  i wybór odbiorców;
- publikację publiczną: osobny proces katalogu/review OpenAI.

Dla nas ma sens stabilna wersja prywatna dostępna tylko zamierzonym osobom
w workspace. Publiczna publikacja nie jest potrzebna do naszej administracji.

Nie znaleziono w dokumentacji gwarancji łagodniejszego automatycznego
zatwierdzania po publikacji. Publikacja, OAuth, adnotacje narzędzi,
uprawnienia aplikacji i decyzja recenzenta są osobnymi rzeczami.
Publikacja może uporządkować dystrybucję i wersję narzędzi; nie zakładaj,
że sama usuwa odmowy. Nie zmieniaj adnotacji ani dostępu, żeby ukrywać skutki
operacji.

## Zalecana kolejność

1. Scalić poprawki tego przeglądu.
2. Wdrożyć aktualny master według refresh-stack.pl.md, z backupem.
3. Sprawdzić lokalnym test_client.py nową listę narzędzi i zgodność hashy.
4. W ChatGPT otworzyć obecną wtyczkę, wykonać Refresh i nową rozmowę.
5. Potwierdzić odczyty info/mcp_health/Zabbix i konta integracji.
6. Jeśli panel nadal pokazuje DEV, wejść w zarządzanie tą konkretną aplikacją
   w workspace i sprawdzić dostępną opcję Publish/Zatwierdź/udostępnij.
   Nazwy i dostępność zależą od używanego panelu i uprawnień.
   Wybrać zamierzonych odbiorców workspace, nie publiczny katalog.
7. Po publikacji potwierdzić, że używana instalacja wskazuje opublikowaną
   wersję, a nie dotychczasowe połączenie DEV. Powtórzyć testy w nowej rozmowie.

Nie trzeba przeinstalowywać systemu operacyjnego ani usuwać credentials/state.
Nie restartuj transportu Caddy tylko z powodu zmiany kodu Python.

## Weryfikacja

- 113 testów + 25 podtestów PASS, w tym rzeczywisty lokalny MCP przez HTTP
  i uruchomienie test_client.py jako osobnego procesu.
- pip check: brak konfliktów w używanym środowisku.
- bash -n install.sh i git diff --check: PASS.
- Osobno wykonany preflight z instalatora: poprawne źródła akceptowane,
  źródło z celowym SyntaxError odrzucane.
- Test instalatora w katalogu tymczasowym wykonuje dwa kolejne wdrożenia,
  drugie ze zmienionym kodem. Sprawdza backupy, zachowanie konfiguracji i danych
  oraz restart. apt/pip, uprawnienia roota i systemd są w tym teście zastąpione
  atrapami — nie jest to rzeczywista instalacja pakietów.
- Działająca usługa MCP01 nie była podmieniana ani restartowana.
- Nie wykonywano rzeczywistych zapisów Gitea/YouTrack, zadań DSH ani zmiany
  ustawień wtyczki. Testy tych zapisów są offline.

## Źródła OpenAI

Sprawdzone 2026-10-21:
- https://developers.openai.com/plugins/deploy/connect-chatgpt
- https://learn.chatgpt.com/docs/build-plugins
- https://developers.openai.com/plugins/deploy/submission
- https://learn.chatgpt.com/docs/sandboxing/auto-review

Uproszczono instalację do clone + bash ./install.sh, a aktualizację do
git pull --ff-only + bash ./install.sh. Oba helpery (Zabbix i bojler) są
wersjonowane, instalowane i kopiowane do automatycznego backupu.
Błędy helpera bojlera zwracane przez MCP nie ujawniają surowego stdout/stderr.
