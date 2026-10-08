#!/bin/bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/hbf-mcp"
SERVICE_USER="gptagent"

apt-get update
apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  git \
  curl \
  jq \
  openssh-client \
  ca-certificates

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${SERVICE_USER}"
fi

# The service account is deliberately unprivileged. This installer must never
# grant it sudo, and it does not create or modify /etc/sudoers.d/gptagent.
if [[ -e /etc/sudoers.d/gptagent ]]; then
  echo "WARNING: /etc/sudoers.d/gptagent exists." >&2
  echo "         The MCP service expects an unprivileged account; review and remove it." >&2
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}"

install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0755 \
  "${SOURCE_DIR}/server.py" \
  "${INSTALL_DIR}/server.py"

install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0755 \
  "${SOURCE_DIR}/test_client.py" \
  "${INSTALL_DIR}/test_client.py"

install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0644 \
  "${SOURCE_DIR}/requirements.txt" \
  "${INSTALL_DIR}/requirements.txt"

if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi

"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

if [[ ! -f /etc/hbf-mcp.env ]]; then
  install -o root -g root -m 0600 \
    "${SOURCE_DIR}/hbf-mcp.env.example" \
    /etc/hbf-mcp.env
else
  chown root:root /etc/hbf-mcp.env
  chmod 0600 /etc/hbf-mcp.env
fi

install -o root -g root -m 0644 \
  "${SOURCE_DIR}/systemd/hbf-mcp.service" \
  /etc/systemd/system/hbf-mcp.service

systemctl daemon-reload
systemctl enable --now hbf-mcp

echo
echo "Installed HBF MCP."
echo
echo "Service:"
echo "  systemctl status hbf-mcp --no-pager"
echo
echo "Logs:"
echo "  journalctl -u hbf-mcp -f"
echo
echo "Listening socket:"
echo "  ss -lntp | grep 8765"
echo
echo "Local MCP test:"
echo "  sudo -u gptagent ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/test_client.py"
echo
echo "Endpoint:"
echo "  http://127.0.0.1:8765/mcp"
