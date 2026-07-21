import os

# Base paths
BACKEND_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)

# Data paths
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
AUDIO_DIR = os.path.join(DATA_DIR, "live_audio")
DB_NAME = os.path.join(DATA_DIR, "festival_radio.db")

# Ensure data directory exists
os.makedirs(AUDIO_DIR, exist_ok=True)