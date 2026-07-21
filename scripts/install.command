#!/bin/bash
# Move back to the project root directory
cd "$(dirname "$0")/.."

echo "========================================"
echo "  RADIO COMMAND CENTER - INSTALLER"
echo "========================================"

echo "[1/4] Creating data directories..."
# Update this so it creates the data folder
mkdir -p data/live_audio

echo "[2/4] Creating Python Virtual Environment..."
python3 -m venv venv

echo "[3/4] Installing dependencies (this may take a minute)..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "[4/4] Setting permissions so the start button works..."
# Fix permissions on the script folder
chmod +x scripts/start.command

echo "========================================"
echo " INSTALLATION COMPLETE!"
echo " You can now close this window and double-click 'start.command'"
echo "========================================"