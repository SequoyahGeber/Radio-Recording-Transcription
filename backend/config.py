import json
import os
import shutil
import sys
import tempfile
from glob import glob

# Base paths
BACKEND_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
LEGACY_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def default_data_dir():
    if sys.platform == "darwin":
        return os.path.expanduser(
            os.path.join("~", "Library", "Application Support", "Radio Command Center")
        )
    return LEGACY_DATA_DIR


DATA_DIR = os.path.abspath(
    os.environ.get("RADIO_DATA_DIR", default_data_dir())
)


def migrate_legacy_data():
    """Copy an existing project-local data directory to writable app storage once."""
    if (
        os.environ.get("RADIO_DATA_DIR")
        or DATA_DIR == LEGACY_DATA_DIR
        or os.path.exists(DATA_DIR)
        or not os.path.isdir(LEGACY_DATA_DIR)
    ):
        return

    os.makedirs(os.path.dirname(DATA_DIR), exist_ok=True)
    migration_path = f"{DATA_DIR}.migrating-{os.getpid()}"
    try:
        shutil.copytree(LEGACY_DATA_DIR, migration_path)
        os.replace(migration_path, DATA_DIR)
    except Exception:
        shutil.rmtree(migration_path, ignore_errors=True)
        raise


migrate_legacy_data()

SETTINGS_PATH = os.path.abspath(
    os.environ.get("RADIO_SETTINGS_PATH", os.path.join(DATA_DIR, "settings.json"))
)


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(updates):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    settings = load_settings()
    settings.update(updates)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix="settings-",
        suffix=".json",
        dir=os.path.dirname(SETTINGS_PATH),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
        os.replace(temporary_path, SETTINGS_PATH)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    return settings


SETTINGS = load_settings()
RECORDING_YEAR = os.environ.get("RADIO_RECORDING_YEAR", "2026")
RECORDING_SOURCE_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_SOURCE_DIR",
        SETTINGS.get("source_dir", "/Volumes/Active Recording"),
    )
)
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")
LEGACY_YEAR_AUDIO_DIR = os.path.join(RECORDINGS_DIR, RECORDING_YEAR)
DEFAULT_AUDIO_DIR = (
    LEGACY_YEAR_AUDIO_DIR
    if os.path.isdir(LEGACY_YEAR_AUDIO_DIR)
    else RECORDINGS_DIR
)
AUDIO_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_AUDIO_DIR",
        SETTINGS.get("audio_dir", DEFAULT_AUDIO_DIR),
    )
)
DATABASE_DIR = os.path.join(DATA_DIR, "databases")
UNIFIED_DB_NAME = os.path.join(DATABASE_DIR, "festival_radio.db")


def default_database_path():
    if os.path.isfile(UNIFIED_DB_NAME):
        return UNIFIED_DB_NAME
    annual_databases = sorted(glob(os.path.join(DATABASE_DIR, "festival_radio_*.db")))
    if annual_databases:
        # Continue using the newest existing database in place. Database
        # initialization imports any other annual archives without renaming or
        # deleting their recoverable source files.
        return annual_databases[-1]
    return UNIFIED_DB_NAME


DB_NAME = os.path.abspath(
    os.environ.get(
        "RADIO_DB_PATH",
        SETTINGS.get("database_path", default_database_path()),
    )
)
SECURITY_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_SECURITY_DIR",
        SETTINGS.get("security_dir", os.path.join(DATA_DIR, "security")),
    )
)
SECURITY_CONFIG_PATH = os.path.join(SECURITY_DIR, "security.json")
TLS_CA_CERT_PATH = os.path.join(SECURITY_DIR, "radio-dashboard-ca.crt")
TLS_CERT_PATH = os.path.join(SECURITY_DIR, "server.crt")
TLS_KEY_PATH = os.path.join(SECURITY_DIR, "server.key")
RUNTIME_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_RUNTIME_DIR",
        SETTINGS.get("runtime_dir", os.path.join(DATA_DIR, "runtime")),
    )
)
LOG_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_LOG_DIR",
        SETTINGS.get("log_dir", os.path.join(DATA_DIR, "logs")),
    )
)
MODEL_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_MODEL_DIR",
        SETTINGS.get("model_dir", os.path.join(DATA_DIR, "models")),
    )
)
RADIO_HOST = os.environ.get("RADIO_HOST", SETTINGS.get("host", "127.0.0.1"))
RADIO_PORT = int(os.environ.get("RADIO_PORT", SETTINGS.get("port", 8000)))

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
os.makedirs(SECURITY_DIR, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)
