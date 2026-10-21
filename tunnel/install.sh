#!/usr/bin/env bash
# Safe, idempotent host preparation: installs service but does not start it.
set +x
set -euo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ACTION=${1:-install}
SERVICE_USER=hbf-tunnel
CONFIG_DIR=/etc/hbf-mcp-tunnel
CLIENT_BIN=${HBF_TUNNEL_BINARY:-/home/gptagent/.local/bin/tunnel-client}

valid_id() {
  [[ "$1" =~ ^tunnel_[a-zA-Z0-9_-]{16,}$ ]]
}
configure() {
  [[ -d $CONFIG_DIR ]] || { echo 'Run install first' >&2; exit 1; }
  printf 'Tunnel ID (from OpenAI Platform): ' >&2
  IFS= read -r tunnel_id
  valid_id "$tunnel_id" || { echo 'Invalid tunnel ID' >&2; exit 1; }
  printf 'Restricted runtime API key (not echoed): ' >&2
  IFS= read -r -s api_key
  printf '\n' >&2
  [[ ${#api_key} -ge 16 ]] || { echo 'Runtime key too short' >&2; exit 1; }
  printf 'CONTROL_PLANE_TUNNEL_ID=%s\n' "$tunnel_id" >"$CONFIG_DIR/tunnel.env"
  printf '%s' "$api_key" >"$CONFIG_DIR/runtime.key"
  unset api_key
  chown root:"$SERVICE_USER" "$CONFIG_DIR/tunnel.env" "$CONFIG_DIR/runtime.key"
  chmod 0640 "$CONFIG_DIR/tunnel.env" "$CONFIG_DIR/runtime.key"
  echo 'Saved tunnel identity and runtime key. Run: tunnel/install.sh doctor'
}
doctor() {
  [[ -s $CONFIG_DIR/runtime.key ]] || { echo 'Missing runtime key' >&2; exit 1; }
  local tunnel_id
  tunnel_id=$(sed -n -E 's/^CONTROL_PLANE_TUNNEL_ID=(tunnel_[A-Za-z0-9_-]+)$/\1/p' "$CONFIG_DIR/tunnel.env" | head -n 1)
  valid_id "$tunnel_id" || { echo 'Missing/invalid tunnel ID' >&2; exit 1; }
  runuser -u "$SERVICE_USER" -- env CONTROL_PLANE_TUNNEL_ID="$tunnel_id" /usr/local/bin/tunnel-client doctor --config "$CONFIG_DIR/client.yaml" --explain
}
case $ACTION in
  install)
    test -x "$CLIENT_BIN" || { echo "Missing verified official binary: $CLIENT_BIN" >&2; exit 1; }
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
      useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"
    install -o root -g root -m 0755 "$CLIENT_BIN" /usr/local/bin/tunnel-client
    install -o root -g "$SERVICE_USER" -m 0640 "$ROOT/client.yaml" "$CONFIG_DIR/client.yaml"
    if [[ ! -e $CONFIG_DIR/tunnel.env ]]; then
      install -o root -g "$SERVICE_USER" -m 0640 /dev/null "$CONFIG_DIR/tunnel.env"
    fi
    if [[ ! -e $CONFIG_DIR/runtime.key ]]; then
      install -o root -g "$SERVICE_USER" -m 0640 /dev/null "$CONFIG_DIR/runtime.key"
    fi
    install -o root -g root -m 0644 "$ROOT/hbf-mcp-tunnel.service" /etc/systemd/system/hbf-mcp-tunnel.service
    systemctl daemon-reload
    echo 'Tunnel service installed, NOT enabled or started.'
    echo 'Next: tunnel/install.sh configure; tunnel/install.sh doctor'
    ;;
  configure) configure ;;
  doctor) doctor ;;
  *)
    echo 'Usage: install.sh {install|configure|doctor}' >&2
    exit 2
    ;;
esac
