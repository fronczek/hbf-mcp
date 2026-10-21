#!/usr/bin/env bash
# Prepare a dedicated tunnel service; intentionally no automatic cutover.
set +x
set -euo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ACTION=${1:-install}
SERVICE_USER=hbf-tunnel
CONFIG_DIR=/etc/hbf-mcp-tunnel
CLIENT_BIN=${HBF_TUNNEL_BINARY:-/home/gptagent/.local/bin/tunnel-client}
CLOUDFLARED_BIN=$(dirname "$CLIENT_BIN")/cloudflared
HOST_SHORT=$(hostname -s)
HOST_CONFIG="$ROOT/hosts/$HOST_SHORT.conf"

valid_id() {
  [[ "$1" =~ ^tunnel_[A-Za-z0-9_-]{16,}$ ]]
}
read_id() {
  local source=$1
  sed -n -E 's/^CONTROL_PLANE_TUNNEL_ID=(tunnel_[A-Za-z0-9_-]+)$/\1/p' "$source" | head -n 1
}
configure() {
  [[ -s "$CONFIG_DIR/tunnel.env" ]] || { echo 'Missing tunnel ID; run install first' >&2; exit 1; }
  if [[ -s "$CONFIG_DIR/runtime.key" ]]; then
    echo 'Runtime key already exists; refusing to overwrite it' >&2
    exit 1
  fi
  printf 'Restricted OpenAI runtime API key (not echoed): ' >&2
  local api_key
  IFS= read -r -s api_key
  printf '\n' >&2
  [[ ${#api_key} -ge 16 ]] || { echo 'Runtime key too short' >&2; exit 1; }
  local tmp
  tmp=$(mktemp "$CONFIG_DIR/.runtime.key.XXXXXX")
  printf '%s' "$api_key" >"$tmp"
  unset api_key
  chown root:"$SERVICE_USER" "$tmp"
  chmod 0640 "$tmp"
  mv -f -- "$tmp" "$CONFIG_DIR/runtime.key"
  echo 'Runtime key stored; next: install.sh doctor'
}
doctor() {
  [[ -s "$CONFIG_DIR/runtime.key" ]] || { echo 'Runtime key missing' >&2; exit 1; }
  local tunnel_id
  tunnel_id=$(read_id "$CONFIG_DIR/tunnel.env")
  valid_id "$tunnel_id" || { echo 'Invalid/missing tunnel ID' >&2; exit 1; }
  runuser -u "$SERVICE_USER" -- env CONTROL_PLANE_TUNNEL_ID="$tunnel_id" /usr/local/bin/tunnel-client doctor --config "$CONFIG_DIR/client.yaml" --explain
}
case "$ACTION" in
  install)
    [[ -x "$CLIENT_BIN" && -x "$CLOUDFLARED_BIN" ]] || {
      echo 'Full client package absent; run tunnel/download-client.sh first' >&2; exit 1;
    }
    [[ -r "$HOST_CONFIG" ]] || { echo "Host $HOST_SHORT has no tunnel mapping" >&2; exit 1; }
    host_id=$(read_id "$HOST_CONFIG")
    valid_id "$host_id" || { echo 'Invalid tunnel ID in host mapping' >&2; exit 1; }
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
      useradd --system --user-group --home-dir /var/lib/hbf-mcp-tunnel --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"
    if [[ "$CLIENT_BIN" != /usr/local/bin/tunnel-client ]]; then
      install -o root -g root -m 0755 "$CLIENT_BIN" /usr/local/bin/tunnel-client
    fi
    if [[ "$CLOUDFLARED_BIN" != /usr/local/bin/cloudflared ]]; then
      install -o root -g root -m 0755 "$CLOUDFLARED_BIN" /usr/local/bin/cloudflared
    fi
    install -o root -g "$SERVICE_USER" -m 0640 "$ROOT/client.yaml" "$CONFIG_DIR/client.yaml"
    if [[ -e "$CONFIG_DIR/tunnel.env" ]]; then
      old_id=$(read_id "$CONFIG_DIR/tunnel.env")
      [[ "$host_id" == "$old_id" ]] || { echo 'Existing tunnel ID differs! Inspect it.' >&2; exit 1; }
    else
      install -o root -g "$SERVICE_USER" -m 0640 "$HOST_CONFIG" "$CONFIG_DIR/tunnel.env"
    fi
    if [[ ! -e "$CONFIG_DIR/runtime.key" ]]; then
      install -o root -g "$SERVICE_USER" -m 0640 /dev/null "$CONFIG_DIR/runtime.key"
    fi
    install -o root -g root -m 0644 "$ROOT/hbf-mcp-tunnel.service" /etc/systemd/system/hbf-mcp-tunnel.service
    systemctl daemon-reload
    echo "Prepared tunnel $host_id for host $HOST_SHORT (NOT started)."
    echo 'Next: install.sh configure; install.sh doctor'
    ;;
  configure) configure ;;
  doctor) doctor ;;
  *) echo 'Usage: install.sh {install|configure|doctor}' >&2; exit 2 ;;
esac
