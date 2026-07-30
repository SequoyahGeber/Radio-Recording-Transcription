#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_PROJECT_ROOT not in sys.path:
    sys.path.insert(0, SCRIPT_PROJECT_ROOT)

from backend.config import (
    LOG_DIR,
    MODEL_DIR,
    PROJECT_ROOT,
    RADIO_HOST,
    RADIO_PORT,
    RUNTIME_DIR,
    TLS_CERT_PATH,
    TLS_KEY_PATH,
    load_settings,
)


PID_PATH = os.path.join(RUNTIME_DIR, "supervisor.pid")
STATUS_PATH = os.path.join(RUNTIME_DIR, "service-status.json")
STOPPING = False
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_log(path):
    try:
        if os.path.getsize(path) < MAX_LOG_BYTES:
            return
    except OSError:
        return
    oldest = f"{path}.3"
    if os.path.exists(oldest):
        os.unlink(oldest)
    for index in (2, 1):
        source = f"{path}.{index}"
        if os.path.exists(source):
            os.replace(source, f"{path}.{index + 1}")
    os.replace(path, f"{path}.1")


def write_pid():
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    temporary_path = f"{PID_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    os.replace(temporary_path, PID_PATH)


def remove_pid():
    try:
        with open(PID_PATH, "r", encoding="utf-8") as handle:
            owner = int(handle.read().strip())
        if owner == os.getpid():
            os.unlink(PID_PATH)
    except (OSError, ValueError):
        pass


def write_status(processes, transcription_enabled):
    status = {
        "supervisor_pid": os.getpid(),
        "transcription_enabled": transcription_enabled,
        "processes": {
            process.name: {
                "pid": process.pid,
                "running": process.running,
            }
            for process in processes
        },
    }
    temporary_path = f"{STATUS_PATH}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(status, handle, sort_keys=True)
        os.replace(temporary_path, STATUS_PATH)
    except OSError:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass


def remove_status():
    try:
        os.unlink(STATUS_PATH)
    except OSError:
        pass


def transcription_is_enabled():
    return bool(load_settings().get("transcription_enabled", True))


def handle_signal(signum, frame):
    del signum, frame
    global STOPPING
    STOPPING = True


class ManagedProcess:
    def __init__(self, name, command, environment):
        self.name = name
        self.command = command
        self.environment = environment
        self.process = None
        self.failure_count = 0
        self.next_start = 0
        self.started_at = 0
        self.log_handle = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self):
        return self.process.pid if self.running else None

    def start(self):
        log_path = os.path.join(LOG_DIR, f"{self.name}.log")
        rotate_log(log_path)
        self.log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        self.log_handle.write(
            f"\n[{datetime.now().isoformat()}] Starting {' '.join(self.command)}\n"
        )
        self.process = subprocess.Popen(
            self.command,
            cwd=PROJECT_ROOT,
            env=self.environment,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
        )
        self.started_at = time.monotonic()

    def poll_and_restart_if_needed(self):
        if self.process is None:
            if time.monotonic() >= self.next_start:
                self.start()
            return
        return_code = self.process.poll()
        if return_code is None:
            if time.monotonic() - self.started_at > 120:
                self.failure_count = 0
            return
        if self.log_handle:
            self.log_handle.write(
                f"[{datetime.now().isoformat()}] Exited with status {return_code}\n"
            )
            self.log_handle.close()
            self.log_handle = None
        self.process = None
        self.failure_count += 1
        delay = min(60, 2 ** min(self.failure_count, 6))
        self.next_start = time.monotonic() + delay

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        self.process = None
        self.failure_count = 0
        self.next_start = 0

    def reconcile(self, enabled):
        if enabled:
            self.poll_and_restart_if_needed()
        else:
            self.stop()


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    write_pid()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    subprocess.run(
        [
            sys.executable,
            os.path.join(PROJECT_ROOT, "scripts", "setup_security.py"),
            "--ensure",
            "--host",
            RADIO_HOST,
            "--port",
            str(RADIO_PORT),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )

    environment = dict(os.environ)
    environment["RADIO_HOST"] = RADIO_HOST
    environment["RADIO_PORT"] = str(RADIO_PORT)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    environment.setdefault("RADIO_TRANSCRIPTION_ENGINE", "mlx")
    environment.setdefault("RADIO_MODEL_SIZE", "medium")
    environment.setdefault("RADIO_MODEL_DIR", MODEL_DIR)
    environment.setdefault("HF_HOME", os.path.join(MODEL_DIR, "hf-mlx"))
    environment.setdefault("RADIO_MLX_MODEL", "mlx-community/whisper-medium-mlx")
    environment.setdefault(
        "RADIO_RETRY_MLX_MODEL",
        "mlx-community/whisper-large-v3-mlx",
    )
    worker_host = "127.0.0.1" if RADIO_HOST in {"0.0.0.0", "::"} else RADIO_HOST
    environment.setdefault(
        "RADIO_SERVER_URL",
        f"https://{worker_host}:{RADIO_PORT}/api/new_transcript",
    )

    processes = [
        ManagedProcess(
            "server",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.server:app",
                "--host",
                RADIO_HOST,
                "--port",
                str(RADIO_PORT),
                "--ssl-keyfile",
                TLS_KEY_PATH,
                "--ssl-certfile",
                TLS_CERT_PATH,
                "--no-server-header",
            ],
            environment,
        ),
        ManagedProcess("worker", [sys.executable, "-m", "backend.worker"], environment),
        ManagedProcess("sync", [sys.executable, os.path.join(PROJECT_ROOT, "sync.py")], environment),
    ]
    process_by_name = {process.name: process for process in processes}

    try:
        while not STOPPING:
            transcription_enabled = transcription_is_enabled()
            process_by_name["server"].poll_and_restart_if_needed()
            process_by_name["sync"].poll_and_restart_if_needed()
            process_by_name["worker"].reconcile(transcription_enabled)
            write_status(processes, transcription_enabled)
            time.sleep(1)
    finally:
        for process in reversed(processes):
            process.stop()
        remove_status()
        remove_pid()


if __name__ == "__main__":
    main()
