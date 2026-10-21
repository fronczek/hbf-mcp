# Odświeżenie HBF MCP i narzędzi ChatGPT

## Co naprawia ta zmiana

- Nowy lokalny branch i podpisany commit są zapisami addytywnymi:
  readOnlyHint=false, destructiveHint=false.
- Staging, synchronizacja checkoutu, push, nadpisanie pliku i zmiana statusu
  zachowują destructiveHint=true: modyfikują istniejący stan.
  youtrack_append_decision również otrzymuje true: nadpisuje cały opis zgłoszenia
  i bez kontroli wersji może utracić równoległą edycję.
- Narzędzia Gitea/YouTrack i Git działające na zamkniętych listach zasobów
  mają openWorldHint=false. Odczyt sesji DSH i gate ograniczony do testowych
  hostów także mają zamknięty zakres. Shell i wykonawczy agent DSH pozostają
  openWorldHint=true.
- Gate przeglądarkowy NIE jest read-only: zapisuje i może nadpisać raporty.
- Ścieżki lokalnych repozytoriów i Playwright wskazują na ~/projects.
- Instalator restartuje działającą usługę; samo enable --now tego nie robiło.
- mcp_health zwraca loaded_source_sha256, disk_source_sha256 i restart_required.
  Porównanie obejmuje podstawowe moduły Python, nie konfigurację, zależności,
  definicje w ChatGPT ani dodatkowe moduły z innych PR.
- test_client.py pokazuje adnotacje pobrane przez prawdziwy endpoint MCP.

Te zmiany ograniczają fałszywe klasyfikacje. Nie gwarantują zatwierdzania każdej
operacji. Adnotacje muszą opisywać rzeczywiste działanie narzędzia.

## 1. Wdrożenie na hbfplhmcpu01

Wykonaj jako root w krótkim oknie bez aktywnego zadania MCP/DSH.
Restart może przerwać aktywne wywołanie. Nie powtarzaj niepewnego zapisu
ani zadania DSH bez sprawdzenia jego wyniku.

Poniższa komenda pobiera gałąź tej poprawki. Przed uruchomieniem porównaj HEAD
z commitem widocznym w PR. Po zmergowaniu można zamiast nazwy gałęzi użyć master.
Jeśli katalog docelowy już istnieje, zatrzymaj się i sprawdź jego stan; nie kasuj go.

```bash
set -euo pipefail

git clone --branch fix/mcp-approval-classification-20261021 \
  https://github.com/fronczek/hbf-mcp.git /root/hbf-mcp-approval-update
cd /root/hbf-mcp-approval-update
git log -1 --oneline

backup_dir="/root/hbf-mcp-backup-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup_dir"
cp -a /opt/hbf-mcp "$backup_dir/runtime"
cp -a /etc/systemd/system/hbf-mcp.service "$backup_dir/hbf-mcp.service"
printf 'Backup: %s\n' "$backup_dir"

bash ./install.sh

systemctl is-active hbf-mcp
journalctl -u hbf-mcp -n 30 --no-pager
sudo -u gptagent /opt/hbf-mcp/.venv/bin/python /opt/hbf-mcp/test_client.py
```

Instalator instaluje pakiety i zależności oraz jednostkę systemd; zachowuje treść
istniejącego /etc/hbf-mcp.env. Nie nadaje sudo kontu gptagent.
Przed instalacją porównaj jednostkę systemd, jeżeli masz lokalne zmiany.
Osobne PR dotyczące tunelu i Zabbixa nie są częścią tej gałęzi.
Jeśli wdrożyłeś je lokalnie, najpierw uzgodnij połączony zestaw źródeł;
nie zastępuj instalacji bez sprawdzenia różnic.

Dla aktualizacji SAMYCH źródeł z już działającymi zależnościami i jednostką
można zamiast install.sh wykonać po powyższym backupie:

```bash
for module in server.py gitea_tools.py youtrack_tools.py git_workflow_tools.py \
              agent_tools.py diagnostics_tools.py test_client.py; do
  install -o gptagent -g gptagent -m 0644 "$module" "/opt/hbf-mcp/$module"
done
systemctl restart hbf-mcp
systemctl is-active --quiet hbf-mcp
sudo -u gptagent /opt/hbf-mcp/.venv/bin/python /opt/hbf-mcp/test_client.py
```

Wybierz jeden wariant aktualizacji. Nie restartuj Caddy ani tunelu tylko z powodu
zmiany adnotacji. Transport jest osobną warstwą.

## 2. Odświeżenie definicji narzędzi w ChatGPT

1. Po restarcie sprawdź lokalnym test_client.py, czy serwer publikuje nowe adnotacje.
2. Otwórz zarządzanie wtyczką hbfplhmcpu01 w ChatGPT/workspace.
3. Użyj dostępnego w tym panelu odświeżenia/skanowania narzędzi
   (Scan Tools / Refresh tools; nazwa zależy od panelu).
4. Jeśli edytujesz wersję roboczą wtyczki, zapisz/opublikuj aktualizację według
   opcji panelu. Sprawdź, że używana instalacja wskazuje tę wersję.
5. Otwórz nową rozmowę z włączoną wtyczką. Nowa rozmowa sama nie zastępuje skanu.
6. Wywołaj info, mcp_health i git_workflow_status dla hbf-mcp.
   W mcp_health oczekuj restart_required=false i równych hashy.
7. Potwierdź: git_workflow_branch i git_workflow_signed_commit mają
   destructiveHint=false; shell i write_file pozostają destructiveHint=true.

Brak nowych pól w mcp_health oznacza, że wciąż działa stary kod albo połączenie
wskazuje inny backend. Nowe pola, ale stare opisy/schematy w modelu, wskazują
na nieodświeżoną definicję narzędzi po stronie ChatGPT.

## 3. Ustawienia zatwierdzania

W chwili diagnozy hbfplhmcpu01 dziedziczył ustawienie
Allow low-risk actions. Poprawka nie zmienia ustawień ChatGPT ani workspace.
Pozostaw uczciwe adnotacje. Nie oznaczaj dowolnego shella jako read-only.

Odmowa automatycznego recenzenta, timeout/capacity, brak sudo, błąd MCP oraz
brak zależności to różne zdarzenia. Przy zgłoszeniu zapisz:
- dokładny komunikat i nazwę narzędzia;
- czas i rodzaj operacji, bez sekretów;
- czy serwer zarejestrował wykonanie i czy wystąpił efekt uboczny;
- wersję/hashe kodu, z którego korzysta proces.

HTTP 200 w access logu nie dowodzi sukcesu operacji MCP.
Przy odmowie nie próbuj tego samego skutku innym transportem.
Przy timeout najpierw sprawdź stan; timeout nie oznacza automatycznie odmowy.

## 4. Rollback

W tej samej sesji root zachowującej backup_dir, po zakończeniu aktywnych zadań:

```bash
test -d "$backup_dir/runtime/.venv"
systemctl stop hbf-mcp
cp -a "$backup_dir/runtime/." /opt/hbf-mcp/
cp -a "$backup_dir/hbf-mcp.service" /etc/systemd/system/hbf-mcp.service
systemctl daemon-reload
systemctl start hbf-mcp
systemctl is-active --quiet hbf-mcp
```

Ta poprawka nie dodaje nowych modułów runtime, więc odtworzenie kopii przywraca
jej poprzedni zestaw plików. Backup nie obejmuje zmian pakietów systemowych
wykonanych przez apt. Po rollbacku ponownie odśwież definicje w ChatGPT.

## Źródła

- https://developers.openai.com/plugins/plan/tools
- https://developers.openai.com/plugins/plugin-guidelines
- https://learn.chatgpt.com/docs/sandboxing/auto-review
- https://learn.chatgpt.com/docs/agent-approvals-security
