#!/usr/bin/env python3
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
    PROJECT_ROOT,
    RADIO_HOST,
    RADIO_PORT,
    RUNTIME_DIR,
    TLS_CERT_PATH,
    TLS_KEY_PATH,
)


PID_PATH = os.path.join(RUNTIME_DIR, "supervisor.pid")
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
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


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
    environment.setdefault("HF_HOME", os.path.join(PROJECT_ROOT, "models", "hf-mlx"))
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

    try:
        while not STOPPING:
            for process in processes:
                process.poll_and_restart_if_needed()
            time.sleep(1)
    finally:
        for process in reversed(processes):
            process.stop()
        remove_pid()


if __name__ == "__main__":
    main()
