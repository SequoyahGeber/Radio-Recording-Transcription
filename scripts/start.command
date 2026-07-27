#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x "./venv/bin/python" ]; then
    echo "Radio Command Center is not installed yet."
    echo "Run scripts/install.command first."
    read -r -p "Press Return to close..."
    exit 1
fi

echo "Starting Radio Command Center supervisor..."
echo "Logs are stored in data/logs."
exec ./venv/bin/python scripts/supervisor.py
