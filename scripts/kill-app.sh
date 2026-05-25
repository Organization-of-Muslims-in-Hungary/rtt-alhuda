#!/usr/bin/env bash
# =============================================================================
# Khutba App Killer
# Forcibly stops all services and rogue processes related to the app.
# =============================================================================

echo "Initiating shutdown of Khutba application..."

# 1. Stop the official systemd service (if it's active)
SERVICE_NAME="khutba-rtt-alhuda.service"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Stopping systemd service ($SERVICE_NAME)..."
    sudo systemctl stop "$SERVICE_NAME"
else
    echo "Systemd service is not currently running."
fi

# 2. Terminate lingering Python backend processes
# This targets any process running 'main.py' or using your specific venv
echo "Hunting for stray Python processes..."
sudo pkill -f "python.*main\.py" || echo "No python main.py processes found."
sudo pkill -f "venv/bin/python" || echo "No venv python processes found."

# 3. Terminate lingering Node/Vite processes (if you left a dev server running)
echo "Hunting for stray Node/Vite processes..."
sudo pkill -f "vite" || echo "No Vite processes found."
sudo pkill -f "npm run dev" || echo "No npm dev processes found."

echo "✅ All application processes have been terminated."
