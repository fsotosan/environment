#!/usr/bin/env bash
# Install or upgrade envmon as a native systemd service.
#
# Usage:
#   sudo ./deploy/install.sh [--no-start]
#
# The script is idempotent. It never overwrites an existing
# /etc/envmon/config.yaml or /etc/envmon/envmon.env.
set -euo pipefail

APP_DIR=/opt/envmon
CONFIG_DIR=/etc/envmon
DATA_DIR=/var/lib/envmon
BACKUP_DIR=/var/backups/envmon
LOG_DIR=/var/log/envmon
SERVICE_USER=envmon
SERVICE_FILE=/etc/systemd/system/envmon.service
START_SERVICE=1

for arg in "$@"; do
  case "$arg" in
    --no-start) START_SERVICE=0 ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "error: run this script as root (sudo ./deploy/install.sh)" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "${SRC_DIR}/pyproject.toml" || ! -d "${SRC_DIR}/src/envmon" ]]; then
  echo "error: cannot find the repository at ${SRC_DIR}" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required (sudo apt install python3 python3-venv)" >&2
  exit 1
fi

if ! python3 -c 'import venv' >/dev/null 2>&1; then
  echo "error: python3-venv is required (sudo apt install python3-venv)" >&2
  exit 1
fi

echo "==> creating service user and directories"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
# The i2c group exists on Raspberry Pi OS after enabling I2C.
if getent group i2c >/dev/null 2>&1; then
  usermod -aG i2c "${SERVICE_USER}"
else
  echo "warning: group 'i2c' not found; enable I2C in raspi-config, then re-run" >&2
fi

install -d -m 0755 "${APP_DIR}" "${CONFIG_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}" "${BACKUP_DIR}" "${LOG_DIR}"

echo "==> installing application files"
rm -rf "${APP_DIR}/src"
install -m 0644 "${SRC_DIR}/pyproject.toml" "${APP_DIR}/pyproject.toml"
install -m 0644 "${SRC_DIR}/README.md" "${APP_DIR}/README.md"
install -m 0644 "${SRC_DIR}/LICENSE" "${APP_DIR}/LICENSE"
cp -a "${SRC_DIR}/src" "${APP_DIR}/src"

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  install -m 0640 -o root -g "${SERVICE_USER}" \
    "${SRC_DIR}/config/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  echo "    created ${CONFIG_DIR}/config.yaml (review it before starting)"
else
  echo "    keeping existing ${CONFIG_DIR}/config.yaml"
fi
if [[ ! -f "${CONFIG_DIR}/envmon.env" ]]; then
  install -m 0640 -o root -g "${SERVICE_USER}" /dev/null "${CONFIG_DIR}/envmon.env"
fi

echo "==> creating virtualenv"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel >/dev/null
"${APP_DIR}/.venv/bin/pip" install "${APP_DIR}" >/dev/null
chown -R root:root "${APP_DIR}/.venv" "${APP_DIR}/src"
chmod -R u+rwX,go+rX "${APP_DIR}/.venv" "${APP_DIR}/src"

echo "==> creating database schema"
sudo -u "${SERVICE_USER}" env \
  ENVMON_CONFIG="${CONFIG_DIR}/config.yaml" \
  ENVMON_DATABASE__URL="sqlite:///${DATA_DIR}/envmon.db" \
  "${APP_DIR}/.venv/bin/envmon" init-db

echo "==> installing systemd unit"
install -m 0644 "${SRC_DIR}/deploy/envmon.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable envmon.service >/dev/null

if [[ "${START_SERVICE}" -eq 1 ]]; then
  systemctl restart envmon.service
  sleep 2
  systemctl --no-pager --lines=0 status envmon.service || true
else
  echo "    service enabled but not started (--no-start)"
fi

cat <<EOF

Installed.

  config:   ${CONFIG_DIR}/config.yaml
  overrides:${CONFIG_DIR}/envmon.env
  database: ${DATA_DIR}/envmon.db
  backups:  ${BACKUP_DIR}
  logs:     ${LOG_DIR}

Next steps:
  1. sudoedit ${CONFIG_DIR}/config.yaml     # timezone, mode, thresholds
  2. sudo systemctl restart envmon
  3. curl -s http://localhost:8080/api/status | jq
  4. journalctl -u envmon -f

Hardware: confirm \`i2cdetect -y 1\` shows 29, 69 and 04/08 before expecting
sensor data. The service user must be in the i2c group; log out/in or reboot
after adding the user. The dashboard is unauthenticated: keep it on a trusted
LAN or behind a VPN/reverse proxy.
EOF
