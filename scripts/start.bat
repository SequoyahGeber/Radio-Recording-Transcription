#!/bin/bash

echo "========================================"
echo "  STARTING RADIO COMMAND CENTER"
echo "========================================"

# Use the Python executables directly from the virtual environment
VENV_PYTHON="./venv/bin/python"
VENV_UVICORN="./venv/bin/uvicorn"

# Function to keep the Web Server alive
run_server() {
    while true; do
        echo "[SYSTEM] Starting Web Server on port 8000..."
        $VENV_UVICORN server:app --host 0.0.0.0 --port 8000
        echo "[SYSTEM] ⚠️ Web Server crashed or stopped! Restarting in 3 seconds..."
        sleep 3
    done
}

# Function to keep the AI Worker alive
run_worker() {
    while true; do
        echo "[SYSTEM] Starting AI Transcription Worker..."
        $VENV_PYTHON worker.py
        echo "[SYSTEM] ⚠️ Worker crashed or stopped! Restarting in 3 seconds..."
        sleep 3
    done
}

# Start both processes in the background
run_server &
SERVER_PID=$!

run_worker &
WORKER_PID=$!

# Trap Ctrl+C so you can shut down the whole system cleanly
trap "echo -e '\n[SYSTEM] Shutting down Command Center...'; kill $SERVER_PID $WORKER_PID; exit" SIGINT SIGTERM

# Keep the script running and wait for user to press Ctrl+C
wait