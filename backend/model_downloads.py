import os
import shutil
import threading
import time
from datetime import datetime

from backend.config import MODEL_DIR
from backend.database import connect
from backend.model_manager import (
    ModelDownloadCancelled,
    PRIMARY_MLX_MODEL,
    RETRY_MLX_MODEL,
    ensure_model,
    estimate_model_size,
    model_is_cached,
)


MODELS = {
    "medium": PRIMARY_MLX_MODEL,
    "large-v3": RETRY_MLX_MODEL,
}
_threads = {}
_lock = threading.Lock()


def _now():
    return datetime.now().isoformat()


def _write_state(model_key, **values):
    allowed = {
        "state",
        "downloaded_bytes",
        "total_bytes",
        "files_completed",
        "files_total",
        "eta_seconds",
        "error",
        "control",
        "started_at",
        "completed_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    values["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with connect() as connection:
        connection.execute(
            f"UPDATE model_downloads SET {assignments} WHERE model_key = ?",
            [*values.values(), model_key],
        )
        connection.commit()


def _control(model_key):
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT control FROM model_downloads WHERE model_key = ?",
            (model_key,),
        ).fetchone()
    return row["control"] if row else "cancel"


def initialize_model_states():
    now = _now()
    with connect() as connection:
        for key, repo_id in MODELS.items():
            cached = model_is_cached(repo_id, MODEL_DIR)
            connection.execute(
                """
                INSERT INTO model_downloads(
                    model_key, repo_id, state, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(model_key) DO UPDATE SET
                    repo_id = excluded.repo_id,
                    state = CASE
                        WHEN excluded.state = 'ready' THEN 'ready'
                        WHEN model_downloads.state = 'downloading' THEN 'error'
                        ELSE model_downloads.state
                    END,
                    error = CASE
                        WHEN model_downloads.state = 'downloading'
                             AND excluded.state != 'ready'
                        THEN 'Download was interrupted; retry to resume'
                        ELSE model_downloads.error
                    END,
                    updated_at = excluded.updated_at,
                    completed_at = coalesce(excluded.completed_at, model_downloads.completed_at)
                """,
                (key, repo_id, "ready" if cached else "not_installed", now, now if cached else None),
            )
        connection.commit()


def _download(model_key):
    repo_id = MODELS[model_key]
    started = time.monotonic()
    last = {"bytes": 0, "at": started}
    try:
        estimate = estimate_model_size(repo_id, MODEL_DIR)
        required = estimate["bytes"]
        free = shutil.disk_usage(MODEL_DIR).free
        reserve = max(2 * 1024**3, int(required * 0.1))
        if required and free < required + reserve:
            raise RuntimeError(
                f"Not enough free disk space: {required + reserve:,} bytes required"
            )
        _write_state(
            model_key,
            state="downloading",
            total_bytes=required,
            files_total=estimate["files"],
            error="",
            started_at=_now(),
            completed_at=None,
        )

        def progress(downloaded, total, _description):
            now = time.monotonic()
            elapsed = max(0.001, now - last["at"])
            speed = max(0, downloaded - last["bytes"]) / elapsed
            effective_total = total or required
            eta = int(max(0, effective_total - downloaded) / speed) if speed else None
            last.update({"bytes": downloaded, "at": now})
            _write_state(
                model_key,
                state="paused" if _control(model_key) == "pause" else "downloading",
                downloaded_bytes=downloaded,
                total_bytes=effective_total,
                eta_seconds=eta,
            )

        ensure_model(
            repo_id,
            MODEL_DIR,
            progress_callback=progress,
            control_callback=lambda: _control(model_key),
        )
        _write_state(
            model_key,
            state="ready",
            control="run",
            downloaded_bytes=required,
            total_bytes=required,
            files_completed=estimate["files"],
            eta_seconds=0,
            error="",
            completed_at=_now(),
        )
    except ModelDownloadCancelled:
        _write_state(model_key, state="not_installed", control="run", error="Download cancelled")
    except Exception as exc:
        _write_state(model_key, state="error", control="run", error=str(exc)[:1000])
    finally:
        with _lock:
            _threads.pop(model_key, None)


def start_download(model_key):
    if model_key not in MODELS:
        raise ValueError("Unknown model")
    if model_is_cached(MODELS[model_key], MODEL_DIR):
        _write_state(model_key, state="ready", control="run", error="", completed_at=_now())
        return get_model_status(model_key)
    with _lock:
        existing = _threads.get(model_key)
        if existing and existing.is_alive():
            _write_state(model_key, control="run", state="downloading", error="")
            return get_model_status(model_key)
        _write_state(model_key, control="run", state="downloading", error="")
        thread = threading.Thread(target=_download, args=(model_key,), daemon=True)
        _threads[model_key] = thread
        thread.start()
    return get_model_status(model_key)


def control_download(model_key, action):
    if model_key not in MODELS:
        raise ValueError("Unknown model")
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise ValueError("Unknown model action")
    if action in {"resume", "retry"}:
        return start_download(model_key)
    _write_state(model_key, control=action, state="paused" if action == "pause" else "downloading")
    return get_model_status(model_key)


def get_model_status(model_key):
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM model_downloads WHERE model_key = ?",
            (model_key,),
        ).fetchone()
    if row is None:
        raise ValueError("Unknown model")
    payload = dict(row)
    payload["ready"] = model_is_cached(payload["repo_id"], MODEL_DIR)
    payload["progress"] = (
        round(payload["downloaded_bytes"] / payload["total_bytes"], 4)
        if payload["total_bytes"]
        else None
    )
    payload["free_disk_bytes"] = shutil.disk_usage(MODEL_DIR).free
    return payload


def all_model_statuses():
    return [get_model_status(key) for key in MODELS]


initialize_model_states()
