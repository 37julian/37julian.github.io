#!/usr/bin/env python3
"""Manueller Sync-Trigger (CLI)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_coach.db import init_db
from garmin_coach.sync import full_sync

if __name__ == "__main__":
    init_db()
    print("Sync gestartet...")
    result = full_sync()
    print(f"Fertig: {result}")
