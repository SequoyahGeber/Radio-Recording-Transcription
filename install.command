#!/bin/bash
# This magic line ensures the script runs in the folder it's located in
cd "$(dirname "$0")"

echo "========================================"
echo "  RADIO COMMAND CENTER - INSTALLER"
echo "========================================"

echo "[1/4] Creating directories..."
mkdir -p live_audio

echo "[2/4] Creating Python Virtual Environment..."
python3 -m venv venv

echo "[3/4] Installing dependencies (this may take a minute)..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "[4/4] Setting permissions so the start button works..."
chmod +x start.command

echo "========================================"
echo " INSTALLATION COMPLETE!"
echo " You can now close this window and double-click 'start.command'"
echo "========================================"