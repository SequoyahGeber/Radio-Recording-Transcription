"""The dashboard's settings surface.

Every tunable value the services read is declared here once: its label, type,
safe range, the environment variable that can override it, and whether changing
it needs a service restart. The dashboard renders this description directly, so
a new setting never needs matching hand-written interface code.
"""

import os
import shutil

from backend.config import (
    AUDIO_DIR,
    DATA_DIR,
    DB_NAME,
    LOG_DIR,
    MODEL_DIR,
    SETTINGS_PATH,
    live_settings,
    save_settings,
    setting_is_managed_by_environment,
)
from backend.readiness import validate_source


GIGABYTE = 1024**3

# restart values: "none" applies immediately, "services" needs the supervisor to
# restart its children, "worker" needs only the transcription worker restarted.
SETTINGS_GROUPS = [
    {
        "id": "recording",
        "title": "Recording source",
        "summary": "Where recordings are collected from and how quickly they are copied.",
        "fields": [
            {
                "key": "source_dir",
                "label": "Recording folder",
                "help": "Local folder or mounted network share that the recorder writes into.",
                "type": "path",
                "default": "/Volumes/Active Recording",
                "environment": "RADIO_SOURCE_DIR",
                "restart": "none",
            },
            {
                "key": "sync_poll_seconds",
                "label": "Scan interval",
                "help": "How often the source folder is checked for new recordings.",
                "type": "integer",
                "default": 15,
                "minimum": 5,
                "maximum": 600,
                "unit": "seconds",
                "environment": "RADIO_SYNC_POLL_SECONDS",
                "restart": "none",
            },
            {
                "key": "sync_settle_seconds",
                "label": "Settle time",
                "help": "How long a recording must stop changing before it is copied.",
                "type": "integer",
                "default": 10,
                "minimum": 2,
                "maximum": 300,
                "unit": "seconds",
                "environment": "RADIO_SYNC_SETTLE_SECONDS",
                "restart": "none",
            },
            {
                "key": "live_window_minutes",
                "label": "Live window",
                "help": "Recordings newer than this are copied and transcribed before the backlog.",
                "type": "integer",
                "default": 30,
                "minimum": 1,
                "maximum": 1440,
                "unit": "minutes",
                "environment": "RADIO_LIVE_WINDOW_MINUTES",
                "restart": "worker",
            },
            {
                "key": "sync_batch_size",
                "label": "Backlog batch size",
                "help": "Historical recordings copied per pass before rechecking for live traffic.",
                "type": "integer",
                "default": 250,
                "minimum": 1,
                "maximum": 5000,
                "unit": "files",
                "environment": "RADIO_SYNC_BATCH_SIZE",
                "restart": "none",
            },
            {
                "key": "sync_verify_sha256",
                "label": "Verify every copy with SHA-256",
                "help": "Slower, but proves each local copy matches the source byte for byte.",
                "type": "boolean",
                "default": True,
                "environment": "RADIO_SYNC_VERIFY_SHA256",
                "restart": "none",
            },
        ],
    },
    {
        "id": "transcription",
        "title": "Transcription",
        "summary": "Which engine and models turn recordings into transcripts.",
        "fields": [
            {
                "key": "transcription_enabled",
                "label": "Run transcription",
                "help": "Turn off to keep the dashboard and recording sync running without transcribing.",
                "type": "boolean",
                "default": True,
                "restart": "none",
            },
            {
                "key": "transcription_engine",
                "label": "Engine",
                "help": "Apple MLX uses the Mac's GPU. faster-whisper runs on the CPU anywhere.",
                "type": "choice",
                "default": "mlx",
                "options": [
                    {"value": "mlx", "label": "Apple MLX (Apple silicon GPU)"},
                    {"value": "faster-whisper", "label": "faster-whisper (CPU)"},
                ],
                "environment": "RADIO_TRANSCRIPTION_ENGINE",
                "restart": "services",
            },
            {
                "key": "mlx_primary_model",
                "label": "First-pass model",
                "help": "Used for every recording. Medium is the balanced default.",
                "type": "choice",
                "default": "mlx-community/whisper-medium-mlx",
                "options": [
                    {"value": "mlx-community/whisper-small-mlx", "label": "Small (fastest)"},
                    {"value": "mlx-community/whisper-medium-mlx", "label": "Medium (recommended)"},
                    {"value": "mlx-community/whisper-large-v3-mlx", "label": "Large V3 (slowest, most accurate)"},
                ],
                "environment": "RADIO_MLX_MODEL",
                "restart": "services",
            },
            {
                "key": "model_size",
                "label": "CPU model size",
                "help": "Only used when the engine is faster-whisper.",
                "type": "choice",
                "default": "medium",
                "options": [
                    {"value": "tiny", "label": "tiny"},
                    {"value": "base", "label": "base"},
                    {"value": "small", "label": "small"},
                    {"value": "medium", "label": "medium"},
                    {"value": "large-v3", "label": "large-v3"},
                ],
                "environment": "RADIO_MODEL_SIZE",
                "restart": "services",
            },
            {
                "key": "rescue_enabled",
                "label": "Retry weak transcripts with Large V3",
                "help": "Low-confidence or repetitive results get a second pass when the queue is idle.",
                "type": "boolean",
                "default": True,
                "restart": "none",
            },
            {
                "key": "rescue_max_duration_seconds",
                "label": "Longest recording eligible for retry",
                "help": "Longer recordings skip the expensive second pass.",
                "type": "integer",
                "default": 180,
                "minimum": 15,
                "maximum": 3600,
                "unit": "seconds",
                "environment": "RADIO_RETRY_MAX_DURATION_SECONDS",
                "restart": "none",
            },
            {
                "key": "rescue_backlog_limit",
                "label": "Retry backlog limit",
                "help": "How many older weak transcripts are re-examined at startup.",
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 500,
                "unit": "transcripts",
                "environment": "RADIO_RESCUE_BACKLOG_LIMIT",
                "restart": "none",
            },
            {
                "key": "max_rescue_retries",
                "label": "Retry attempts before giving up",
                "type": "integer",
                "default": 3,
                "minimum": 0,
                "maximum": 10,
                "unit": "attempts",
                "environment": "RADIO_MAX_RESCUE_RETRIES",
                "restart": "none",
            },
        ],
    },
    {
        "id": "access",
        "title": "Network and sign-in",
        "summary": "How this Mac serves the dashboard and how people sign in to it.",
        "fields": [
            {
                "key": "host",
                "label": "Dashboard availability",
                "help": "Allow other devices on the network to open the dashboard, or keep it on this Mac.",
                "type": "choice",
                "default": "127.0.0.1",
                "options": [
                    {"value": "127.0.0.1", "label": "This Mac only"},
                    {"value": "0.0.0.0", "label": "This Mac and the local network"},
                ],
                "environment": "RADIO_HOST",
                "restart": "services",
            },
            {
                "key": "port",
                "label": "Dashboard port",
                "type": "integer",
                "default": 8000,
                "minimum": 1024,
                "maximum": 65535,
                "environment": "RADIO_PORT",
                "restart": "services",
            },
            {
                "key": "app_auto_sign_in",
                "label": "Sign in automatically in the Mac app",
                "help": "The app on this Mac opens straight into the dashboard. Browsers always sign in.",
                "type": "boolean",
                "default": True,
                "environment": "RADIO_APP_AUTO_SIGN_IN",
                "restart": "none",
            },
            {
                "key": "session_lifetime_hours",
                "label": "Browser sign-in lasts",
                "help": "A signed-in browser stays signed in for this long, renewing while it is in use.",
                "type": "integer",
                "default": 720,
                "minimum": 1,
                "maximum": 8760,
                "unit": "hours",
                "environment": "RADIO_SESSION_LIFETIME_HOURS",
                "restart": "none",
            },
            {
                "key": "login_max_failures",
                "label": "Failed sign-ins before lockout",
                "type": "integer",
                "default": 5,
                "minimum": 3,
                "maximum": 50,
                "unit": "attempts",
                "restart": "none",
            },
            {
                "key": "login_window_minutes",
                "label": "Lockout length",
                "type": "integer",
                "default": 15,
                "minimum": 1,
                "maximum": 1440,
                "unit": "minutes",
                "restart": "none",
            },
        ],
    },
    {
        "id": "storage",
        "title": "Storage and history",
        "summary": "Disk safety limits and how long operational history is kept.",
        "fields": [
            {
                "key": "minimum_free_bytes",
                "label": "Keep free disk space",
                "help": "Copying pauses before free space falls below this reserve. Recordings are never deleted.",
                "type": "integer",
                "default": 10 * GIGABYTE,
                "minimum": GIGABYTE,
                "maximum": 4000 * GIGABYTE,
                "unit": "GB",
                "scale": GIGABYTE,
                "environment": "RADIO_SYNC_MIN_FREE_BYTES",
                "restart": "none",
            },
            {
                "key": "backup_interval_hours",
                "label": "Automatic backup interval",
                "help": "A verified database backup is taken this often. Set to 0 to only back up manually.",
                "type": "integer",
                "default": 24,
                "minimum": 0,
                "maximum": 720,
                "unit": "hours",
                "restart": "none",
            },
            {
                "key": "backup_retention_count",
                "label": "Backups kept",
                "help": "Older verified backups beyond this count are removed automatically.",
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 90,
                "unit": "backups",
                "restart": "none",
            },
            {
                "key": "event_retention_days",
                "label": "Live update history kept",
                "help": "The replay log that lets reconnecting dashboards catch up. 0 keeps everything.",
                "type": "integer",
                "default": 45,
                "minimum": 0,
                "maximum": 3650,
                "unit": "days",
                "restart": "none",
            },
            {
                "key": "audit_retention_days",
                "label": "Audit history kept",
                "help": "Record of who changed what. 0 keeps everything.",
                "type": "integer",
                "default": 730,
                "minimum": 0,
                "maximum": 3650,
                "unit": "days",
                "restart": "none",
            },
            {
                "key": "completed_job_retention_days",
                "label": "Finished queue records kept",
                "type": "integer",
                "default": 14,
                "minimum": 1,
                "maximum": 365,
                "unit": "days",
                "restart": "none",
            },
            {
                "key": "log_max_megabytes",
                "label": "Rotate each log at",
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 1024,
                "unit": "MB",
                "restart": "none",
            },
            {
                "key": "log_retention_days",
                "label": "Rotated logs kept",
                "type": "integer",
                "default": 30,
                "minimum": 1,
                "maximum": 3650,
                "unit": "days",
                "restart": "none",
            },
        ],
    },
]

FIELDS_BY_KEY = {
    field["key"]: field for group in SETTINGS_GROUPS for field in group["fields"]
}


def field_default(key):
    field = FIELDS_BY_KEY.get(key)
    return field.get("default") if field else None


def effective_value(field):
    """The value the services actually use, honouring environment overrides."""
    key = field["key"]
    environment_variable = field.get("environment")
    if setting_is_managed_by_environment(environment_variable):
        raw = os.environ[environment_variable]
    else:
        raw = live_settings().get(key, field.get("default"))
    try:
        return coerce_value(field, raw)
    except ValueError:
        return field.get("default")


def coerce_value(field, raw):
    kind = field.get("type", "text")
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}
    if kind == "integer":
        try:
            value = int(float(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field['label']} must be a whole number") from exc
        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if minimum is not None and value < minimum:
            raise ValueError(
                f"{field['label']} must be at least {display_number(field, minimum)}"
            )
        if maximum is not None and value > maximum:
            raise ValueError(
                f"{field['label']} must be at most {display_number(field, maximum)}"
            )
        return value
    if kind == "choice":
        value = str(raw)
        allowed = {option["value"] for option in field.get("options", [])}
        if value not in allowed:
            raise ValueError(f"{field['label']} has an unsupported value")
        return value
    if kind == "path":
        value = os.path.abspath(os.path.expanduser(str(raw or "").strip()))
        if not value or value == "/":
            raise ValueError(f"{field['label']} must be a folder path")
        return value
    return str(raw or "").strip()


def display_number(field, value):
    scale = field.get("scale")
    if scale:
        return round(value / scale, 2)
    return value


def settings_document():
    groups = []
    for group in SETTINGS_GROUPS:
        fields = []
        for field in group["fields"]:
            environment_variable = field.get("environment")
            managed = setting_is_managed_by_environment(environment_variable)
            fields.append(
                {
                    **{
                        name: field[name]
                        for name in field
                        if name not in {"environment"}
                    },
                    "value": effective_value(field),
                    "environment": environment_variable,
                    "managed_by_environment": managed,
                }
            )
        groups.append({**group, "fields": fields})
    disk = shutil.disk_usage(DATA_DIR)
    return {
        "groups": groups,
        "paths": {
            "data_dir": DATA_DIR,
            "audio_dir": AUDIO_DIR,
            "database": DB_NAME,
            "logs": LOG_DIR,
            "models": MODEL_DIR,
            "settings_file": SETTINGS_PATH,
        },
        "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
    }


def apply_settings(updates):
    """Validate and persist a settings change.

    Returns the stored values plus the restart scope the change needs, so the
    dashboard can tell the operator exactly what has to happen for it to apply.
    """
    if not isinstance(updates, dict) or not updates:
        raise ValueError("No settings were provided")

    validated = {}
    restart_scopes = set()
    warnings = []
    for key, raw in updates.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            raise ValueError(f"Unknown setting: {key}")
        if setting_is_managed_by_environment(field.get("environment")):
            raise ValueError(
                f"{field['label']} is currently set by the {field['environment']} "
                "environment variable and cannot be changed here"
            )
        value = coerce_value(field, raw)
        if key == "source_dir":
            source = validate_source(value)
            if not source["readable"]:
                raise ValueError(
                    source["error"]
                    or "The recording folder cannot be read from this Mac"
                )
            if not source["sample_files"]:
                warnings.append(
                    "No audio files were found in that folder yet. It will be used "
                    "as soon as recordings appear."
                )
        if effective_value(field) != value and field.get("restart", "none") != "none":
            restart_scopes.add(field["restart"])
        validated[key] = value

    stored = save_settings(validated)
    return {
        "settings": {key: stored.get(key) for key in validated},
        "restart_required": "services" if "services" in restart_scopes else (
            "worker" if "worker" in restart_scopes else "none"
        ),
        "warnings": warnings,
    }
