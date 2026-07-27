import json
import os
import tempfile

# Base paths
BACKEND_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)

DATA_DIR = os.path.abspath(
    os.environ.get("RADIO_DATA_DIR", os.path.join(PROJECT_ROOT, "data"))
)
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
AUDIO_DIR = os.path.abspath(
    os.environ.get(
        "RADIO_AUDIO_DIR",
        SETTINGS.get(
            "audio_dir",
            os.path.join(DATA_DIR, "recordings", RECORDING_YEAR),
        ),
    )
)
DB_NAME = os.path.abspath(
    os.environ.get(
        "RADIO_DB_PATH",
        os.path.join(DATA_DIR, "databases", f"festival_radio_{RECORDING_YEAR}.db"),
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
RADIO_HOST = os.environ.get("RADIO_HOST", SETTINGS.get("host", "127.0.0.1"))
RADIO_PORT = int(os.environ.get("RADIO_PORT", SETTINGS.get("port", 8000)))

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
os.makedirs(SECURITY_DIR, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
