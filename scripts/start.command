#!/bin/bash
# Move back to the project root directory
cd "$(dirname "$0")/.."

echo "========================================"
echo "  STARTING RADIO COMMAND CENTER"
echo "========================================"

VENV_PYTHON="./venv/bin/python"
VENV_UVICORN="./venv/bin/uvicorn"

# Function to keep the Web Server alive
run_server() {
    while true; do
        echo "[SYSTEM] Starting Web Server on port 8000..."
        # Notice we are now pointing to backend.server:app
        $VENV_UVICORN backend.server:app --host 0.0.0.0 --port 8000
        echo "[SYSTEM] ⚠️ Web Server crashed or stopped! Restarting in 3 seconds..."
        sleep 3
    done
}

# Function to keep the AI Worker alive
run_worker() {
    while true; do
        echo "[SYSTEM] Starting AI Transcription Worker..."
        # Notice we are now pointing to backend/worker.py
        $VENV_PYTHON -m backend.worker
        echo "[SYSTEM] ⚠️ Worker crashed or stopped! Restarting in 3 seconds..."
        sleep 3
    done
}

# Function to keep the Network Sync Worker alive
run_sync() {
    while true; do
        echo "[SYSTEM] Starting Network Sync Worker..."
        $VENV_PYTHON sync.py
        echo "[SYSTEM] ⚠️ Sync Worker crashed or stopped! Restarting in 3 seconds..."
        sleep 3
    done
}

# Start all processes in the background
run_server &
SERVER_PID=$!

run_worker &
WORKER_PID=$!

run_sync &
SYNC_PID=$!

# Trap window close / Ctrl+C so it shuts down cleanly
trap "echo -e '\n[SYSTEM] Shutting down Command Center...'; kill $SERVER_PID $WORKER_PID $SYNC_PID; exit" SIGINT SIGTERM EXIT

# Keep the script running
wait