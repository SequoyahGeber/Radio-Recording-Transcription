#!/usr/bin/env python3
import json
import os
import signal
import fcntl
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

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
    live_settings,
    number_setting,
)


PID_PATH = os.path.join(RUNTIME_DIR, "supervisor.pid")
LOCK_PATH = os.path.join(RUNTIME_DIR, "supervisor.lock")
STATUS_PATH = os.path.join(RUNTIME_DIR, "service-status.json")
STOPPING = False
DEFAULT_MAX_LOG_BYTES = 10 * 1024 * 1024
KEPT_LOG_GENERATIONS = 3


def max_log_bytes():
    megabytes = number_setting(
        "log_max_megabytes",
        default=DEFAULT_MAX_LOG_BYTES // (1024 * 1024),
        minimum=1,
        maximum=1024,
    )
    return megabytes * 1024 * 1024


def rotate_log(path):
    """Move a full log aside, keeping a few previous generations."""
    oldest = f"{path}.{KEPT_LOG_GENERATIONS}"
    if os.path.exists(oldest):
        os.unlink(oldest)
    for index in range(KEPT_LOG_GENERATIONS - 1, 0, -1):
        source = f"{path}.{index}"
        if os.path.exists(source):
            os.replace(source, f"{path}.{index + 1}")
    if os.path.exists(path):
        os.replace(path, f"{path}.1")


def acquire_single_instance_lock():
    """Hold an exclusive lock for as long as this supervisor lives.

    A pid file alone cannot answer "is it still running": after a power cut the
    recorded number is eventually reissued to an unrelated process, which made
    the app report a dead installation as healthy. It also could not stop a
    second supervisor from starting and applying a queued database restore
    underneath the first one's live services. The kernel releases this lock when
    the process dies, however it dies.
    """
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def another_supervisor_is_running():
    """Whether a live supervisor holds the lock right now."""
    try:
        with open(LOCK_PATH, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


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
    return bool(live_settings().get("transcription_enabled", True))


def worker_stall_seconds():
    return number_setting(
        "worker_stall_restart_seconds", default=600, minimum=0, maximum=86400
    )


def heartbeat_age_seconds(service):
    """Seconds since a service last reported in, or None if it never has."""
    try:
        from backend.database import connect

        with connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT last_seen FROM service_heartbeats WHERE service = ?",
                (service,),
            ).fetchone()
        if row is None:
            return None
        return max(
            0.0,
            (datetime.now() - datetime.fromisoformat(row["last_seen"])).total_seconds(),
        )
    except Exception:
        # A database that cannot be read is not evidence that a service stalled.
        return 0.0


def handle_signal(signum, frame):
    del signum, frame
    global STOPPING
    STOPPING = True


class ManagedProcess:
    """One supervised service, with its output written to a rotating log.

    The supervisor reads each child's output itself rather than handing the
    child a file descriptor. That is what makes rotation possible while the
    service keeps running, so an installation left on for months cannot fill
    the disk with a single enormous log file.
    """

    def __init__(self, name, command, environment):
        self.name = name
        self.command = command
        self.environment = environment
        self.log_path = os.path.join(LOG_DIR, f"{name}.log")
        self.process = None
        self.failure_count = 0
        self.next_start = 0
        self.started_at = 0
        self.log_handle = None
        self.log_bytes = 0
        self.log_lock = threading.Lock()
        self.reader = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self):
        return self.process.pid if self.running else None

    def _open_log(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.log_handle = open(self.log_path, "a", encoding="utf-8", buffering=1)
        try:
            self.log_bytes = os.path.getsize(self.log_path)
        except OSError:
            self.log_bytes = 0

    def write_log(self, text):
        with self.log_lock:
            if self.log_handle is None:
                self._open_log()
            if self.log_bytes >= max_log_bytes():
                self.log_handle.close()
                try:
                    rotate_log(self.log_path)
                except OSError:
                    pass
                self._open_log()
            try:
                self.log_handle.write(text)
                self.log_bytes += len(text)
            except (OSError, ValueError):
                self.log_handle = None

    def _pump_output(self, stream):
        try:
            for line in stream:
                self.write_log(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def start(self):
        self.write_log(
            f"\n[{datetime.now().isoformat()}] Starting {' '.join(self.command)}\n"
        )
        self.process = subprocess.Popen(
            self.command,
            cwd=PROJECT_ROOT,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        self.reader = threading.Thread(
            target=self._pump_output,
            args=(self.process.stdout,),
            name=f"{self.name}-log",
            daemon=True,
        )
        self.reader.start()
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
        self.write_log(
            f"[{datetime.now().isoformat()}] Exited with status {return_code}\n"
        )
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
        if self.reader is not None:
            self.reader.join(timeout=5)
            self.reader = None
        with self.log_lock:
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

    def restart_if_wedged(self, silent_seconds):
        """Restart a process that is alive but has stopped reporting in.

        A crashed service exits and is restarted by the poll above. A wedged one
        does not: the transcription worker can hang forever inside a GPU or
        model import, staying 'running' while transcribing nothing. Without this
        check that state survives until a person notices.
        """
        if not self.running or not silent_seconds:
            return False
        age = heartbeat_age_seconds(self.name)
        running_for = time.monotonic() - self.started_at
        if running_for < silent_seconds:
            return False
        if age is not None and age < silent_seconds:
            return False
        self.write_log(
            f"[{datetime.now().isoformat()}] No heartbeat for "
            f"{int(running_for)}s; restarting the stalled service.\n"
        )
        self.stop()
        self.failure_count += 1
        self.next_start = time.monotonic() + min(60, 2 ** min(self.failure_count, 6))
        return True


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    lock = acquire_single_instance_lock()
    if lock is None:
        print(
            "[SUPERVISOR] Another Radio Command Center supervisor is already "
            "running; leaving it alone.",
            flush=True,
        )
        return
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

    # A queued restore replaces the archive before the dashboard, worker, or
    # sync can open it. Nothing else in the system is allowed to swap the
    # database while it is in use.
    try:
        from backend.backups import apply_pending_restore

        restore = apply_pending_restore(
            log=lambda message: print(message, flush=True)
        )
        if restore and restore["status"] != "restored":
            print(f"[RESTORE] Continuing with the existing archive: {restore['error']}")
    except Exception as exc:
        print(f"[RESTORE] Restore step skipped: {exc}", flush=True)

    # Only values that are not administrator settings are injected here. Engine,
    # model, host, and port all come from settings.json by way of backend.config,
    # so the settings screen can change them instead of an environment variable
    # silently overriding whatever the administrator chose.
    environment = dict(os.environ)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    environment.setdefault("RADIO_MODEL_DIR", MODEL_DIR)
    environment.setdefault("HF_HOME", os.path.join(MODEL_DIR, "hf-mlx"))
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
            if transcription_enabled:
                process_by_name["worker"].restart_if_wedged(worker_stall_seconds())
            write_status(processes, transcription_enabled)
            time.sleep(1)
    finally:
        for process in reversed(processes):
            process.stop()
        remove_status()
        remove_pid()


if __name__ == "__main__":
    main()
