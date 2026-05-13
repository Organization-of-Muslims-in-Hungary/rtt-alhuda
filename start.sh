#!/bin/bash
# ============================================================
# KhutbaPi startup script
# Run with: sudo bash /home/pi/.local/Juma/Juma/start.sh
# ============================================================

JUMA_DIR="/home/pi/.local/Juma/Juma"
VENV_PYTHON="$JUMA_DIR/.venv/bin/python3"
LOG_FILE="/root/juma.log"
PI_DISPLAY=":0"

cd "$JUMA_DIR" || exit 1

# ── Kill any existing server on port 80 ──
fuser -k 80/tcp 2>/dev/null
# Wait until port 80 is actually free (up to 10s)
for i in $(seq 1 10); do
  if ! ss -tlnp | grep -q ':80'; then break; fi
  echo "[KhutbaPi] Waiting for port 80 to be free... ($i/10)"
  sleep 1
done

# ── Start the server ──
echo "[KhutbaPi] Starting server..."
: > "$LOG_FILE"   # truncate / create log file as current user (root)
PYTHONUNBUFFERED=1 "$VENV_PYTHON" main.py >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "[KhutbaPi] Server PID: $SERVER_PID"

# ── Wait for server to be ready (HTTP health check) ──
for i in $(seq 1 20); do
  if curl -sf http://localhost/ -o /dev/null 2>/dev/null; then
    echo "[KhutbaPi] Server ready!"
    break
  fi
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "[KhutbaPi] ERROR: Server process died! Log:"
    cat "$LOG_FILE"
    exit 1
  fi
  echo "[KhutbaPi] Waiting for server... ($i/20)"
  sleep 1
done

# ── Get local IP ──
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo "[KhutbaPi] Running at:"
echo "  Projector display : http://khutba-pi.local/app"
echo "  TV view           : http://khutba-pi.local/tv"
echo "  Phone control     : http://$LOCAL_IP/control"
echo "  Operator panel    : http://khutba-pi.local/"

# ── Open Chromium in kiosk mode on the HDMI display ──
# When run via 'sudo bash', root needs access to the pi user's X session.
XAUTH_FILE="/home/pi/.Xauthority"
BROWSER_CMD=""
if command -v chromium-browser &>/dev/null; then
  BROWSER_CMD="chromium-browser"
elif command -v chromium &>/dev/null; then
  BROWSER_CMD="chromium"
fi

if [[ -n "$BROWSER_CMD" ]]; then
  # Allow root to connect to pi's X display, then open as pi user
  if [[ -f "$XAUTH_FILE" ]]; then
    sudo -u pi DISPLAY=$PI_DISPLAY XAUTHORITY="$XAUTH_FILE" \
      "$BROWSER_CMD" \
      --kiosk \
      --noerrdialogs \
      --disable-infobars \
      --no-first-run \
      "http://localhost/app" \
      &>/dev/null &
  else
    # No Xauthority — try directly (works if script is run from desktop terminal)
    DISPLAY=$PI_DISPLAY "$BROWSER_CMD" \
      --kiosk \
      --noerrdialogs \
      --disable-infobars \
      --no-first-run \
      "http://localhost/app" \
      &>/dev/null &
  fi
  echo "[KhutbaPi] Chromium launched in kiosk mode (http://localhost/)"
else
  echo "[KhutbaPi] Chromium not found — open http://localhost/ manually in the browser"
fi

echo ""
echo "══════════════════════════════════════════"
echo "  PHONE CONTROL: http://$LOCAL_IP/control"
echo "══════════════════════════════════════════"
echo ""
echo "Press Ctrl+C to stop everything."

# ── Wait and clean up on exit ──
trap "echo '[KhutbaPi] Stopping...'; kill $SERVER_PID 2>/dev/null; fuser -k 80/tcp 2>/dev/null; exit 0" INT TERM

wait $SERVER_PID
