import os
import shutil
from datetime import datetime

from backend.backups import list_backups
from backend.config import DATA_DIR, load_settings
from backend.model_downloads import all_model_statuses


AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a")


def validate_source(path):
    path = os.path.abspath(os.path.expanduser(str(path or "")))
    result = {
        "path": path,
        "exists": os.path.isdir(path),
        "readable": False,
        "sample_files": [],
        "error": "",
    }
    if not result["exists"]:
        result["error"] = "The recording folder does not exist or is not mounted"
        return result
    try:
        with os.scandir(path) as entries:
            children = list(entries)
        result["readable"] = True
        candidates = [entry.path for entry in children if entry.is_file()]
        for directory in [entry.path for entry in children if entry.is_dir()][:20]:
            try:
                with os.scandir(directory) as entries:
                    candidates.extend(entry.path for entry in entries if entry.is_file())
            except OSError:
                continue
        result["sample_files"] = [
            os.path.basename(value)
            for value in candidates
            if value.lower().endswith(AUDIO_EXTENSIONS)
        ][:5]
    except OSError as exc:
        result["error"] = str(exc)
    return result


def readiness_status():
    settings = load_settings()
    source = validate_source(settings.get("source_dir", ""))
    models = all_model_statuses()
    medium = next(item for item in models if item["model_key"] == "medium")
    disk = shutil.disk_usage(DATA_DIR)
    reserve = int(settings.get("minimum_free_bytes", 10 * 1024**3))
    backups = list_backups()
    checks = [
        {"id": "source", "label": "Recording source", "ready": source["readable"]},
        {"id": "model", "label": "Medium transcription model", "ready": medium["ready"]},
        {"id": "storage", "label": "Storage safety reserve", "ready": disk.free >= reserve},
        {"id": "administrator", "label": "Administrator account", "ready": False},
        {"id": "backup", "label": "Verified recovery backup", "ready": bool(backups)},
    ]
    return {
        "step": int(settings.get("setup_step", 1)),
        "source": source,
        "models": models,
        "storage": {"free_bytes": disk.free, "minimum_free_bytes": reserve},
        "transcription_enabled": bool(settings.get("transcription_enabled", True)),
        "checks": checks,
        "updated_at": datetime.now().isoformat(),
    }
