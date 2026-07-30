#!/usr/bin/env python3
import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

SCRIPT_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_PROJECT_ROOT not in sys.path:
    sys.path.insert(0, SCRIPT_PROJECT_ROOT)

# A packaged control command can also be run from Terminal or by the updater,
# outside the Swift launcher that normally supplies PYTHONPATH. Reattach the
# bundled dependencies before importing the backend or spawning the supervisor.
BUNDLED_SITE_PACKAGES = os.path.join(SCRIPT_PROJECT_ROOT, "site-packages")
if os.path.isdir(BUNDLED_SITE_PACKAGES):
    if BUNDLED_SITE_PACKAGES not in sys.path:
        sys.path.insert(0, BUNDLED_SITE_PACKAGES)
    existing_python_path = os.environ.get("PYTHONPATH", "")
    python_path_parts = [
        part for part in existing_python_path.split(os.pathsep) if part
    ]
    if BUNDLED_SITE_PACKAGES not in python_path_parts:
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [BUNDLED_SITE_PACKAGES, *python_path_parts]
        )

from backend.config import (
    MODEL_DIR,
    PROJECT_ROOT,
    RADIO_HOST,
    RADIO_PORT,
    RUNTIME_DIR,
    load_settings,
    save_settings,
)
from backend.model_manager import (
    PRIMARY_MLX_MODEL,
    RETRY_MLX_MODEL,
    ensure_model,
    model_is_cached,
)


PID_PATH = os.path.join(RUNTIME_DIR, "supervisor.pid")
STATUS_PATH = os.path.join(RUNTIME_DIR, "service-status.json")
SUPERVISOR_PATH = os.path.join(PROJECT_ROOT, "scripts", "supervisor.py")


def read_pid():
    try:
        with open(PID_PATH, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def process_is_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status():
    pid = read_pid()
    running = process_is_running(pid)
    settings = load_settings()
    transcription_enabled = bool(settings.get("transcription_enabled", True))
    transcription_running = False
    if running:
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as handle:
                service_status = json.load(handle)
            if service_status.get("supervisor_pid") == pid:
                worker = service_status.get("processes", {}).get("worker", {})
                transcription_running = bool(worker.get("running"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    dashboard_host = "127.0.0.1" if RADIO_HOST in {"0.0.0.0", "::"} else RADIO_HOST
    return {
        "running": running,
        "pid": pid if running else None,
        "transcription_enabled": transcription_enabled,
        "transcription_running": transcription_running,
        "primary_model": PRIMARY_MLX_MODEL,
        "primary_model_cached": model_is_cached(PRIMARY_MLX_MODEL, MODEL_DIR),
        "retry_model": RETRY_MLX_MODEL,
        "retry_model_cached": model_is_cached(RETRY_MLX_MODEL, MODEL_DIR),
        "host": RADIO_HOST,
        "port": RADIO_PORT,
        "dashboard": f"https://{dashboard_host}:{RADIO_PORT}",
    }


def start():
    current = status()
    if current["running"]:
        return current
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    launcher_log = open(
        os.path.join(RUNTIME_DIR, "supervisor-launch.log"),
        "a",
        encoding="utf-8",
    )
    subprocess.Popen(
        [sys.executable, SUPERVISOR_PATH],
        cwd=PROJECT_ROOT,
        stdout=launcher_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = status()
        if current["running"]:
            return current
        time.sleep(0.2)
    raise RuntimeError("The service supervisor did not start")


def stop():
    pid = read_pid()
    if not process_is_running(pid):
        return status()
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            break
        time.sleep(0.2)
    if process_is_running(pid):
        raise RuntimeError("The service supervisor did not stop")
    return status()


def set_transcription(enabled):
    if (
        enabled
        and os.environ.get("RADIO_TRANSCRIPTION_ENGINE", "mlx").lower() == "mlx"
        and platform.system() == "Darwin"
        and platform.machine() == "arm64"
    ):
        ensure_model(PRIMARY_MLX_MODEL, MODEL_DIR)
    save_settings({"transcription_enabled": enabled})
    if enabled and not status()["running"]:
        start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = status()
        if current["transcription_running"] == enabled:
            return current
        if not current["running"] and not enabled:
            return current
        time.sleep(0.2)
    action = "start" if enabled else "stop"
    raise RuntimeError(f"The transcription worker did not {action}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "start",
            "stop",
            "restart",
            "status",
            "configure",
            "transcription-start",
            "transcription-stop",
        ],
    )
    parser.add_argument("--source")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    if args.action == "configure":
        updates = {}
        if args.source:
            source = os.path.abspath(args.source)
            if not os.path.isdir(source):
                raise RuntimeError("The selected recording folder is not available")
            updates["source_dir"] = source
        if args.host:
            updates["host"] = args.host
        if args.port:
            updates["port"] = args.port
        result = {"settings": save_settings(updates), "restart_required": True}
    elif args.action == "start":
        result = start()
    elif args.action == "stop":
        result = stop()
    elif args.action == "restart":
        stop()
        result = start()
    elif args.action == "transcription-start":
        result = set_transcription(True)
    elif args.action == "transcription-stop":
        result = set_transcription(False)
    else:
        result = status()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
