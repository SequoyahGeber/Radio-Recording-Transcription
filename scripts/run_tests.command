#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="./venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="${RADIO_PYTHON:-python3}"
fi

PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/radio-command-center-tests" \
    "$PYTHON_BIN" -m unittest discover -s tests -v

if "$PYTHON_BIN" -c "import fastapi, httpx" 2>/dev/null; then
    PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/radio-command-center-tests" \
        "$PYTHON_BIN" tests/api_integration.py
else
    echo "API integration test skipped (FastAPI is not installed in this interpreter)."
fi

if [ "${RADIO_SKIP_BROWSER_TESTS:-0}" = "1" ]; then
    echo "Browser regression tests skipped by RADIO_SKIP_BROWSER_TESTS=1."
else
    scripts/run_browser_tests.command
fi
