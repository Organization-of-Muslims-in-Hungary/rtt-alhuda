#!/usr/bin/env bash
# =============================================================================
# Khutba Raspberry Pi setup (rtt-alhuda + Khutba-app-frontend)
#
# Run on the Pi with the frontend available. Preferred layout (git submodule):
#   .../khutba-app/rtt-alhuda/                    ← this tree (scripts/pi-setup.sh)
#   .../khutba-app/rtt-alhuda/Khutba-app-frontend/ ← submodule (see docs/frontend-submodule.md)
# Legacy layout still supported:
#   .../khutba-app/Khutba-app-frontend/
#
# Usage:
#   sudo ./scripts/pi-setup.sh --with-apt              # OS deps + venv + npm build + Pi .env
#   sudo ./scripts/pi-setup.sh --with-apt --with-node-repo   # if Node < 20 (Vite 8)
#   ./scripts/pi-setup.sh                            # idempotent: pip + npm build + .env
#   sudo ./scripts/pi-setup.sh --with-systemd --start-service  # after first full setup
#
# Overrides:
#   KHUTBA_FRONTEND_DIR=/path/to/Khutba-app-frontend
#   VITE_BACKEND_ORIGIN=http://127.0.0.1:3000   # only if UI cannot proxy /stream (see nginx example)
# =============================================================================
set -euo pipefail

WITH_APT=0
WITH_SYSTEMD=0
START_SERVICE=0
WITH_NODE_REPO=0

usage() {
  sed -n '1,22p' "$0" | tail -n +2
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-apt) WITH_APT=1 ;;
    --with-systemd) WITH_SYSTEMD=1 ;;
    --start-service) START_SERVICE=1 ;;
    --with-node-repo) WITH_NODE_REPO=1 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
  shift
done

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RTT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

die() { echo "ERROR: $*" >&2; exit 1; }

if [[ -n "${KHUTBA_FRONTEND_DIR:-}" ]]; then
  FRONTEND_DIR="$(CDPATH= cd -- "${KHUTBA_FRONTEND_DIR}" && pwd)"
elif [[ -f "${RTT_DIR}/Khutba-app-frontend/package.json" ]]; then
  FRONTEND_DIR="$(CDPATH= cd -- "${RTT_DIR}/Khutba-app-frontend" && pwd)"
else
  FRONTEND_DIR="$(CDPATH= cd -- "${RTT_DIR}/../Khutba-app-frontend" && pwd)"
fi

[[ -f "${RTT_DIR}/requirements.txt" ]] || die "requirements.txt not found (${RTT_DIR})"
[[ -f "${RTT_DIR}/main.py" ]] || die "main.py not found (${RTT_DIR})"
[[ -f "${FRONTEND_DIR}/package.json" ]] || die "Khutba frontend not found (${FRONTEND_DIR}); set KHUTBA_FRONTEND_DIR"

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1 (install with --with-apt or your package manager)"; }

if [[ "${WITH_APT}" -eq 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || die "--with-apt must be run as root (try: sudo $0 --with-apt ...)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y \
    python3 python3-venv python3-dev python3-pip \
    build-essential pkg-config \
    libportaudio2 portaudio19-dev \
    curl ca-certificates \
    git \
    nodejs npm
fi

if [[ "${WITH_NODE_REPO}" -eq 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || die "--with-node-repo must be run as root"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

need_cmd python3
need_cmd node
need_cmd npm

node_major="$(node -v | sed -n 's/^v\([0-9][0-9]*\).*/\1/p')"
if [[ "${node_major:-0}" -lt 20 ]]; then
  echo "WARNING: Node $(node -v) is below 20; Vite 8 may fail to build."
  echo "         Fix: sudo $0 --with-apt --with-node-repo"
fi

# --- Backend .env (LAN + QR: bind all interfaces) ---
ENV_FILE="${RTT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${RTT_DIR}/.env.example" "${ENV_FILE}"
  echo "Created ${ENV_FILE} from .env.example — set OPENROUTER_API_KEY for live transcription."
fi

upsert_env() {
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*${key}=" "${ENV_FILE}"; then
    sed -i.bak "s|^[[:space:]]*${key}=.*|${key}=${val}|" "${ENV_FILE}" && rm -f "${ENV_FILE}.bak"
  else
    printf '\n# Added by scripts/pi-setup.sh (Pi LAN + QR)\n%s=%s\n' "${key}" "${val}" >> "${ENV_FILE}"
  fi
}

upsert_env RTT_ALHUDA_LISTEN_HOST 0.0.0.0
upsert_env RTT_ALHUDA_LISTEN_PORT 3000

# --- Python venv ---
if [[ ! -d "${RTT_DIR}/venv" ]]; then
  python3 -m venv "${RTT_DIR}/venv"
fi
"${RTT_DIR}/venv/bin/pip" install --upgrade pip
"${RTT_DIR}/venv/bin/pip" install -r "${RTT_DIR}/requirements.txt"

# --- Frontend (devDependencies required for vite build) ---
export VITE_BACKEND_ORIGIN="${VITE_BACKEND_ORIGIN:-}"
(
  cd "${FRONTEND_DIR}"
  npm install
  npm run build
)

# --- Optional systemd ---
SERVICE_NAME="khutba-rtt-alhuda.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
if [[ "${WITH_SYSTEMD}" -eq 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || die "--with-systemd must be run as root"
  RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
  RUN_USER="${RUN_USER:-pi}"
  cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=Khutba rtt-alhuda (transcription /stream + /api/lan-ipv4)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${RTT_DIR}
Environment=PATH=${RTT_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${RTT_DIR}/venv/bin/python ${RTT_DIR}/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "${SERVICE_PATH}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  echo "Installed ${SERVICE_PATH} (enabled on boot)."
fi

if [[ "${START_SERVICE}" -eq 1 ]]; then
  [[ "${EUID}" -eq 0 ]] || die "--start-service must be run as root"
  [[ -f "${SERVICE_PATH}" ]] || die "--start-service requires ${SERVICE_PATH} (run once with --with-systemd)"
  systemctl restart "${SERVICE_NAME}"
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
fi

echo ""
echo "=== Khutba Pi setup done ==="
echo "rtt-alhuda:     ${RTT_DIR}"
echo "frontend dist:  ${FRONTEND_DIR}/dist"
echo ""
echo "Backend listens on 0.0.0.0:3000 (see ${ENV_FILE})"
echo "Phones on same Wi‑Fi: http://<this-pi-ip>:3000/  (templates) or use nginx example on :8080 for React + QR."
echo ""
echo "Nginx example (same origin → QR + /stream):"
echo "  ${RTT_DIR}/scripts/nginx-khutba-frontend.example.conf"
echo ""
if command -v hostname >/dev/null 2>&1; then
  LAN_FIRST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "${LAN_FIRST}" ]]; then
    echo "First LAN address (hostname -I): ${LAN_FIRST}"
    echo "  e.g. http://${LAN_FIRST}:3000/  and after nginx: http://${LAN_FIRST}:8080/"
  fi
fi
echo "LAN hint JSON: GET http://127.0.0.1:3000/api/lan-ipv4"
echo ""
