#!/usr/bin/env bash
# =============================================================================
# Khutba App Updater
# Rebuilds the frontend, updates Python packages, and restarts the backend.
# Usage: sudo ./scripts/update-app.sh
# =============================================================================
set -euo pipefail

# Require root for systemctl restarts and building (since setup was run as root)
if [[ "${EUID}" -eq 0 ]]; then
  RUN_AS_ROOT=1
else
  echo "ERROR: This script must be run as root (try: sudo $0)" >&2
  exit 1
fi

echo "Initiating Khutba App Update..."

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RTT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

# Resolve Frontend Directory
if [[ -n "${KHUTBA_FRONTEND_DIR:-}" ]]; then
  FRONTEND_DIR="$(CDPATH= cd -- "${KHUTBA_FRONTEND_DIR}" && pwd)"
else
  FRONTEND_DIR="$(CDPATH= cd -- "${RTT_DIR}/../Khutba-app-frontend" && pwd)"
fi

SERVICE_NAME="khutba-rtt-alhuda.service"

# --- 1. Update Backend Dependencies ---
echo "1/3 Checking for new Python dependencies..."
if [[ -f "${RTT_DIR}/requirements.txt" ]]; then
  "${RTT_DIR}/venv/bin/pip" install -r "${RTT_DIR}/requirements.txt"
else
  echo "No requirements.txt found. Skipping pip install."
fi

# --- 2. Rebuild Frontend ---
echo "2/3 Rebuilding React frontend..."
if [[ -d "${FRONTEND_DIR}" && -f "${FRONTEND_DIR}/package.json" ]]; then
  (
    cd "${FRONTEND_DIR}"
    npm install
    npm run build
  )
else
  echo "Frontend directory or package.json not found. Skipping UI build."
fi

# --- 3. Restart Backend Service ---
echo "3/3 Restarting backend service..."
if systemctl list-unit-files | grep -q "${SERVICE_NAME}"; then
  systemctl restart "${SERVICE_NAME}"
  echo "ervice restarted successfully."
else
  echo "Systemd service not found. Did you run pi-setup.sh first?"
fi

echo ""
echo "Update Complete! Your latest changes are now live."