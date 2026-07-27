#!/bin/bash
set -euo pipefail

# Move back to the project root directory
cd "$(dirname "$0")/.."

echo "========================================"
echo "  RADIO COMMAND CENTER - INSTALLER"
echo "========================================"

echo "[1/5] Creating data directories..."
mkdir -p data/recordings/2026 data/databases data/logs data/runtime models

echo "[2/5] Creating Python Virtual Environment..."
PYTHON_BIN="${RADIO_PYTHON:-}"

if [ -z "$PYTHON_BIN" ]; then
    for candidate in \
        /opt/homebrew/opt/python@3.12/bin/python3.12 \
        /usr/local/opt/python@3.12/bin/python3.12 \
        /opt/homebrew/bin/python3.12 \
        /usr/local/bin/python3.12
    do
        if [ -x "$candidate" ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "$PYTHON_BIN" ] && command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.12)"
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    if command -v brew >/dev/null 2>&1; then
        echo "Python 3.12 is not installed. Installing it with Homebrew..."
        brew install python@3.12
        PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
    else
        echo "ERROR: Python 3.12 and Homebrew were not found."
        echo "Install Homebrew from https://brew.sh, then run this installer again."
        exit 1
    fi
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$PYTHON_VERSION" != "3.12" ]; then
    echo "ERROR: $PYTHON_BIN is Python $PYTHON_VERSION; Python 3.12 is required."
    echo "Set RADIO_PYTHON to a Python 3.12 executable and try again."
    exit 1
fi

echo "Using $PYTHON_BIN ($("$PYTHON_BIN" --version))"
"$PYTHON_BIN" -m venv venv

echo "[3/5] Installing dependencies (this may take a minute)..."
./venv/bin/pip install --upgrade pip
REQUIREMENTS_FILE="requirements.lock"
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    REQUIREMENTS_FILE="requirements.txt"
fi
./venv/bin/pip install -r "$REQUIREMENTS_FILE"

# Confirm that compiled dependencies load before reporting success.
./venv/bin/python -c "import fastapi, uvicorn, faster_whisper, mlx_whisper, av, numpy, watchdog, requests, pydantic, aiofiles"

echo "[4/5] Building the Mac launcher app..."
chmod +x scripts/build_app.command
./scripts/build_app.command

echo "[5/5] Setting launcher permissions..."
chmod +x scripts/start.command scripts/service_control.py scripts/supervisor.py

echo "========================================"
echo " INSTALLATION COMPLETE!"
echo " Open 'dist/Radio Command Center.app' to choose a recording folder and launch."
echo "========================================"
