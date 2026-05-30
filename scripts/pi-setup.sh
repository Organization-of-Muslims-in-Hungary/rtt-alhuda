#!/usr/bin/env bash
# =============================================================================
# Khutba Raspberry Pi setup (rtt-alhuda + Khutba-app-frontend)
#
# Run on the Pi with both repos present. Default layout:
#   .../khutba-app/rtt-alhuda/           ← this repo (contains scripts/pi-setup.sh)
#   .../khutba-app/Khutba-app-frontend/
#
# Usage:
#   sudo ./scripts/pi-setup.sh
#
# Overrides:
#   KHUTBA_FRONTEND_DIR=/path/to/Khutba-app-frontend
#   VITE_BACKEND_ORIGIN=http://127.0.0.1:3000   # only if UI cannot proxy /stream
# =============================================================================
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

# Since everything is default now, the entire script requires root privileges
[[ "${EUID}" -eq 0 ]] || die "This script must be run as root (try: sudo $0)"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RTT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ -n "${KHUTBA_FRONTEND_DIR:-}" ]]; then
  FRONTEND_DIR="$(CDPATH= cd -- "${KHUTBA_FRONTEND_DIR}" && pwd)"
else
  FRONTEND_DIR="$(CDPATH= cd -- "${RTT_DIR}/../Khutba-app-frontend" && pwd)"
fi

[[ -f "${RTT_DIR}/requirements.txt" ]] || die "requirements.txt not found (${RTT_DIR})"
[[ -f "${RTT_DIR}/main.py" ]] || die "main.py not found (${RTT_DIR})"
[[ -f "${FRONTEND_DIR}/package.json" ]] || die "Khutba frontend not found (${FRONTEND_DIR}); set KHUTBA_FRONTEND_DIR"

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1 (Failed to install via APT)"; }

# --- 1. System Dependencies & Node.js Repo (APT) ---
export DEBIAN_FRONTEND=noninteractive
apt-get update -y

# Setup Node.js 22.x repository (Required for Vite 8)
apt-get install -y ca-certificates curl gnupg
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -

# Install all required system packages
apt-get install -y \
  python3 python3-venv python3-dev python3-pip \
  build-essential pkg-config \
  libportaudio2 portaudio19-dev \
  git \
  nodejs nginx

need_cmd python3
need_cmd node
need_cmd npm

node_major="$(node -v | sed -n 's/^v\([0-9][0-9]*\).*/\1/p')"
if [[ "${node_major:-0}" -lt 20 ]]; then
  echo "WARNING: Node $(node -v) is below 20; Vite 8 may fail to build."
fi

# --- 2. Backend .env (LAN + QR: bind all interfaces) ---
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

# --- 3. Python venv ---
if [[ ! -d "${RTT_DIR}/venv" ]]; then
  python3 -m venv "${RTT_DIR}/venv"
fi
"${RTT_DIR}/venv/bin/pip" install --upgrade pip
"${RTT_DIR}/venv/bin/pip" install -r "${RTT_DIR}/requirements.txt"

# --- 4. Frontend Build ---
export VITE_BACKEND_ORIGIN="${VITE_BACKEND_ORIGIN:-}"
(
  cd "${FRONTEND_DIR}"
  npm install
  npm run build
)

# --- 5. Systemd Service Setup & Start ---
SERVICE_NAME="khutba-rtt-alhuda.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
RUN_USER="${RUN_USER:-pi}"

cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=Khutba rtt-alhuda (transcription /stream + /api/lan-ipv4)
After=network-online.target sound.target
Wants=network-online.target sound.target

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
systemctl restart "${SERVICE_NAME}"
echo "Installed and started ${SERVICE_PATH} (enabled on boot)."

# --- 6. Nginx Setup ---
NGINX_CONF_NAME="khutba"
NGINX_CONF_AVAIL="/etc/nginx/sites-available/${NGINX_CONF_NAME}.conf"
NGINX_CONF_ENABLED="/etc/nginx/sites-enabled/${NGINX_CONF_NAME}.conf"

mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
cat >"${NGINX_CONF_AVAIL}" <<'NGINXEOF'
server {
    listen 8080 default_server;
    listen [::]:8080 default_server;

    server_name _;

    # root is substituted by pi-setup.sh at install time
    __ROOT_PLACEHOLDER__
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # SSE (must not be fully buffered)
    location /stream/text {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 1d;
    }

    location /stream {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/lan-ipv4 {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
    }
}
NGINXEOF

sed -i "s|__ROOT_PLACEHOLDER__|root ${FRONTEND_DIR}/dist;|" "${NGINX_CONF_AVAIL}"
chmod 644 "${NGINX_CONF_AVAIL}"
ln -sf "${NGINX_CONF_AVAIL}" "${NGINX_CONF_ENABLED}"
# Remove default Nginx site to prevent potential conflicts
if [[ -L "/etc/nginx/sites-enabled/default" ]]; then
  rm -f "/etc/nginx/sites-enabled/default"
fi
nginx -t
systemctl enable nginx
systemctl reload-or-restart nginx
echo "Nginx configured: ${NGINX_CONF_AVAIL}"
echo "Serving ${FRONTEND_DIR}/dist on :8080, proxying /stream* and /api/lan-ipv4 to :3000."

# --- 7. Final Output ---
echo ""
echo "=== Khutba Pi setup done ==="
echo "rtt-alhuda:     ${RTT_DIR}"
echo "frontend dist:  ${FRONTEND_DIR}/dist"
echo ""
echo "Backend listens on 0.0.0.0:3000 (see ${ENV_FILE})"
echo "Nginx serving React app on :8080 (config: ${NGINX_CONF_AVAIL})"
echo ""
if command -v hostname >/dev/null 2>&1; then
  LAN_FIRST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "${LAN_FIRST}" ]]; then
    echo "First LAN address (hostname -I): ${LAN_FIRST}"
    echo "  App Available at: http://${LAN_FIRST}:8080/"
  fi
fi
echo "LAN hint JSON: GET http://127.0.0.1:3000/api/lan-ipv4"
echo ""
