# Oficjalny gitea-mcp: ocena dla HBF

Sprawdzono 2026-10-21: main 1c0e601f458281ecda70e5fb48da4e555cb347cf
oraz tag v1.9.0 (9f1b7d39f3d424bcf6c7aa6239410b6ff67358e4).
Źródło pobrano świeżo przez Git z https://gitea.com/gitea/gitea-mcp.
To przegląd źródeł, nie test integracyjny z naszym git.hbf.pl.
Nie instalowano usługi, nie tworzono tokenu ani aplikacji OAuth.

## Wniosek

Warto pilotować jako osobne połączenie Gitea READ-ONLY, szczególnie dla Codex CLI.
Może ograniczyć utrzymanie własnych odczytów i rozszerzyć je o wiki, release'y,
projekty, registry i Actions. Nie zastępuje HBF MCP jako całości: nie obsługuje
naszych VM, YouTrack, DSH, diagnostyki ani lokalnego workflow Git/GPG.

Nie zakładaj, że oficjalny serwer usunie odmowy platformy.
W pkg/annotation/annotation.go funkcja Write ustawia tylko Title;
ReadOnlyHint=false, DestructiveHint i OpenWorldHint nie dostają jawnego false.
Brak destructiveHint nie jest deklaracją braku destrukcyjności.
pull_request_write łączy create/update/close/merge/update_branch/reviewerów;
nie można wyłączyć samego merge przez listę nazw narzędzi.
To różni się od naszych osobnych operacji z expected SHA i kluczem idempotencji.

## Porównanie

| Obszar | gitea-mcp | HBF MCP |
| --- | --- | --- |
| Repo, pliki, PR, commity | Tak | Tak, ograniczony zestaw |
| Wiki, release, registry, projekty | Szersze wsparcie | Brak części funkcji |
| Actions | Odczyty i operacje wykonawcze | Odczyty i zwięzłe podsumowanie CI |
| Read-only | -r ukrywa narzędzia zapisu | Osobne odczyty i zapisy |
| Filtr narzędzi | --tools / --scope | Stały zestaw funkcji |
| Lista repozytoriów | Nie znaleziono osobnej flagi allowlist repo | Dokładna allowlista |
| Tożsamość gpt | Zależy od tokenu/logowania | Weryfikowana przy zapisach |
| Podpis lokalnym GPG | Nie zastępuje naszego procesu | Weryfikowany podpis i HEAD |
| YouTrack / DSH / VM | Nie | Tak |

--scope i --tools łączą się sumą (OR), nie przecięciem.
Nie dodawaj szerokiego --scope do wąskiej listy --tools, oczekując zawężenia.
Filtr narzędzi nie jest filtrem repozytoriów. Zakres repo egzekwuj uprawnieniami
konta gpt w Gitei oraz dostępnymi ograniczeniami tokenu. Token read-only jest
drugą granicą oprócz -r.

## Minimalny pilot lokalny

Zainstaluj binarkę v1.9.0 z oficjalnych releases, weryfikując sumę, albo zbuduj
dokładnie ten tag. Projekt w tym wydaniu wymaga Go 1.27 lub nowszego.
Nie używaj @latest w stałej konfiguracji. Sprawdź gitea-mcp --version i --help.

Program obsługuje GITEA_ACCESS_TOKEN oraz GITEA_ACCESS_TOKEN_FILE.
Ten drugi musi wskazywać plik zawierający SAM token, nie nasz YAML.
Nie przekazuj sekretu przez -T ani nie zapisuj go w repozytorium/config.toml.
Istniejący bezpieczny loader YAML może ustawić GITEA_ACCESS_TOKEN przed startem
Codex; poniższa konfiguracja przekazuje tę zmienną do procesu MCP.

Do ~/.codex/config.toml, na maszynie, na której działa Codex CLI:

```toml
[mcp_servers.gitea_read]
command = "/usr/local/bin/gitea-mcp"
args = [
  "-t", "stdio",
  "-H", "https://git.hbf.pl",
  "-r",
  "--tools", "get_me,list_my_repos,get_file_contents,get_dir_contents,get_repository_tree,list_branches,list_commits,get_commit,compare_commits,get_commit_status,list_pull_requests,pull_request_read,list_issues,issue_read,actions_run_read"
]
env_vars = ["GITEA_ACCESS_TOKEN"]
startup_timeout_sec = 20
tool_timeout_sec = 60
```

Uruchom nową sesję Codex i sprawdź /mcp.
Pierwsze zadanie: wywołaj get_me, potwierdź login gpt, następnie odczytaj listę PR
secnet/ese-agent. Zweryfikuj, że lista tools nie zawiera żadnego write.
Nie włączaj obok dwóch identycznych zestawów odczytów Gitea na stałe;
po pilocie wybierz jeden, żeby model nie wybierał losowo między duplikatami.

## HTTP i ChatGPT w przeglądarce

stdio jest odpowiednie dla CLI, nie dla zdalnego klienta ChatGPT.
Dla przeglądarki potrzebna jest osobna usługa HTTP i poprawne uwierzytelnianie.
Przykładowy start procesu na loopback, z ograniczoną listą narzędzi:

```bash
gitea-mcp -t http -b 127.0.0.1 -p 8766 \
  -H https://git.hbf.pl -r \
  --tools get_me,list_my_repos,get_file_contents,list_pull_requests,pull_request_read
```

Endpoint: /mcp; health: /healthz. Powyższe polecenie nie wdraża systemd ani proxy.
Bez flagi bind domyślnie nasłuchuje na wszystkich interfejsach.

Projekt wspiera OAuth przez Giteę: -oauth -oauth-public-url https://PUBLICZNY-HOST.
Gitea nie wspiera dynamicznej rejestracji klientów, więc aplikację OAuth trzeba
zarejestrować wcześniej, a klient MCP musi obsłużyć taki sposób rejestracji.
README podaje callbacki Claude; NIE kopiuj ich do konfiguracji ChatGPT.
Użyj callbacka pokazanego przez konkretny panel OpenAI i sprawdź cały flow.

OAuth działa jako zalogowane konto Gitea. Logowanie Artura powoduje działania
jako Artur; dla agenta zachowaj uzgodnioną tożsamość gpt.
Przesyłanie PAT w Authorization opisane w README nie jest samo w sobie OAuth.
Nie zakładaj zgodności każdego takiego trybu z klientem ChatGPT bez testu.

Caddy powinien przekazywać Host, Authorization, Mcp-Protocol-Version,
Mcp-Method i Mcp-Name bez niezamierzonego przepisywania/usuwania.
Po zmianie transportu sprawdź osobno:
- dostęp i uwierzytelnienie;
- initialize i tools/list;
- get_me oraz jeden odczyt repo;
- odmowę anonimowego dostępu do prywatnych danych;
- nieobecność narzędzi zapisu przy -r.

## Kolejność dla nas

1. Wdrożyć poprawki hbf-mcp i odświeżyć listę narzędzi.
2. Pilotować oficjalny gitea-mcp przez stdio w CLI z kontem gpt i -r.
3. Porównać kompletność oraz rozmiar odpowiedzi dla naszych repo.
4. Dopiero potem przygotować osobne połączenie HTTP/OAuth do ChatGPT.
5. Zapisy pozostawić w wąskich narzędziach HBF, dopóki nie sprawdzimy
   tożsamości autora, podpisów, idempotencji i granic zatwierdzania.

## Źródła

- https://gitea.com/gitea/gitea-mcp/src/tag/v1.9.0/README.md
- https://gitea.com/gitea/gitea-mcp/src/tag/v1.9.0/pkg/annotation/annotation.go
- https://gitea.com/gitea/gitea-mcp/src/tag/v1.9.0/pkg/tool/tool.go
- https://gitea.com/gitea/gitea-mcp/src/tag/v1.9.0/operation/pull/pull.go
- https://gitea.com/gitea/gitea-mcp/src/tag/v1.9.0/cmd/cmd.go
- https://developers.openai.com/codex/mcp
