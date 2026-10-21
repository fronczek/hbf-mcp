# Instalacja i aktualizacja HBF MCP

## Pierwsza instalacja

Na serwerze docelowym, jako root, po scaleniu PR:

```bash
git clone https://github.com/fronczek/hbf-mcp.git
cd hbf-mcp
bash ./install.sh
```

## Aktualizacja

W tym samym katalogu, jako root:

```bash
git pull --ff-only
bash ./install.sh
```

Instalator sam:
- zapisuje kopię istniejącej instalacji, venv, jednostki systemd, konfiguracji
  /etc/hbf-mcp.env i helperów Zabbixa oraz bojlera w /var/backups/hbf-mcp;
- instaluje wymagane pakiety i zależności;
- sprawdza zależności i składnię źródeł przed podmianą kodu;
- aktualizuje moduły, oba helpery i jednostkę systemd;
- zachowuje treść istniejącego /etc/hbf-mcp.env;
- pozostawia credentials i stan operacji w /home/gptagent;
- restartuje hbf-mcp i sprawdza aktywność usługi.

Można ponownie uruchamiać ten sam skrypt. Każdy upgrade tworzy osobny backup
z datą i unikalnym sufiksem. Instalator wypisuje jego dokładną ścieżkę.
Nie uruchamia testowych zapisów do Gitea/YouTrack ani zadań DSH.
Zależności opcjonalnych istniejących projektów (np. Playwright) pozostają
zarządzane przez te projekty.

Restart może przerwać aktywne wywołanie MCP. Instalacja nie jest atomowa:
backup nie cofa automatycznie zmian pakietów apt. Jednostka systemd jest
zarządzana przez repo; lokalne dostosowania trzymaj w drop-inach systemd.

Helper kuk-boiler-watch jest dołączony do repo. Korzysta z systemowego Pythona
i pakietu python3-yaml instalowanego przez install.sh. W istniejącym
/home/gptagent/credentials/influxdb.yml oczekuje pól influx_url, influx_org,
influx_bucket i influx_token. Instalator nie tworzy ani nie nadpisuje tego pliku.

## Odświeżenie ChatGPT

Po wdrożeniu: otwórz połączenie hbfplhmcpu01 w Plugins, wybierz Refresh
i rozpocznij nową rozmowę. Odświeżenie dotyczy zmian nazw, opisów, schematów
i adnotacji narzędzi — git pull ani restart systemd nie odświeża ich w ChatGPT.

## Opcjonalna diagnostyka

```bash
systemctl status hbf-mcp --no-pager
journalctl -u hbf-mcp -n 30 --no-pager
sudo -u gptagent /opt/hbf-mcp/.venv/bin/python /opt/hbf-mcp/test_client.py
```

Klient sprawdza endpoint, wyświetla adnotacje narzędzi i potwierdza zgodność
hashy kodu wczytanego przez proces z plikami na dysku. Błąd daje kod niezerowy.

## Przywrócenie poprzedniej wersji

Jako root ustaw backup_dir na dokładną ścieżkę wypisaną przez instalator.
Poniższy przykład dotyczy aktualizacji istniejącej instalacji:

```bash
backup_dir=/var/backups/hbf-mcp/SCIEZKA-WYPISANA-PRZEZ-INSTALATOR
test -d "$backup_dir/runtime/.venv" || exit 1
systemctl stop hbf-mcp
cp -a "$backup_dir/runtime/." /opt/hbf-mcp/
if [[ -f "$backup_dir/hbf-mcp.service" ]]; then
  cp -a "$backup_dir/hbf-mcp.service" /etc/systemd/system/hbf-mcp.service
fi
if [[ -f "$backup_dir/hbf-mcp.env" ]]; then
  cp -a "$backup_dir/hbf-mcp.env" /etc/hbf-mcp.env
fi
for helper in zabbix-watch kuk-boiler-watch; do
  if [[ -f "$backup_dir/$helper" ]]; then
    cp -a "$backup_dir/$helper" "/home/gptagent/bin/$helper"
  fi
done
systemctl daemon-reload
systemctl start hbf-mcp
systemctl is-active --quiet hbf-mcp
```

Przywrócenie kopii nie usuwa nowych plików nieobecnych w starszej wersji
i nie cofa pakietów systemowych. Po rollbacku odśwież narzędzia w ChatGPT.

## DEV i publikacja

[Przegląd przed wdrożeniem](preinstall-review.pl.md) opisuje osobno publikację
prywatną do workspace. Nie daje ona gwarancji usunięcia odmów automatycznego
zatwierdzania i nie jest wymagana do aktualizacji serwera.

Źródło odświeżania metadanych:
https://developers.openai.com/plugins/deploy/connect-chatgpt
