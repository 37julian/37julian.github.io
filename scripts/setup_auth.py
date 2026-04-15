#!/usr/bin/env python3
"""Einmalige Garmin-Authentifizierung.

Speichert das Auth-Token in .garth/ — danach kein Passwort mehr nötig.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_coach.sync import login

if __name__ == "__main__":
    print("Garmin-Authentifizierung\n" + "=" * 30)
    api = login(interactive=True)
    print(f"\nErfolgreich! Token gespeichert in .garth/")
    print("Starte jetzt die App: python -m garmin_coach")
