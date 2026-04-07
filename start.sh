#!/bin/bash

echo "========================================"
echo " Starting RTT-Alhuda Translation System "
echo "========================================"

# 1. Navigate to the correct folder
cd ~/.local/Juma/Juma

# 2. Check if the virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Error: Virtual environment not found. Please run setup first."
    exit 1
fi

# 3. Activate the virtual environment
source .venv/bin/activate

# 4. Start the Python server with sudo privileges
# (The -E flag ensures your API key variables are passed through)
echo "Starting server... Open http://khutba-pi.local on your phone."
sudo -E .venv/bin/python3 web_app.py