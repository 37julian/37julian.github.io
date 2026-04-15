"""Zentrale Konfiguration aus .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

# Datenverzeichnisse
DATA_DIR = ROOT / "data"
FIT_DIR = DATA_DIR / "fit"
DB_PATH = DATA_DIR / "garmin.db"
GARTH_DIR = ROOT / ".garth"

DATA_DIR.mkdir(exist_ok=True)
FIT_DIR.mkdir(exist_ok=True)

# Garmin
GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Sync-Optionen
HEALTH_SYNC_DAYS = int(os.environ.get("HEALTH_SYNC_DAYS", "90"))
ACTIVITY_SYNC_LIMIT = int(os.environ.get("ACTIVITY_SYNC_LIMIT", "50"))

# Server
PORT = int(os.environ.get("PORT", "8000"))
