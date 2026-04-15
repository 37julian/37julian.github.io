"""SQLite-Datenbank: Schema + einfache Query-Helfer.

Schema-Designprinzip: Wir speichern jeden bekannten Garmin-Datenpunkt in eigenen
Spalten UND zusätzlich das komplette Roh-JSON in einer ``raw_json``-Spalte. So
geht garantiert nichts verloren, auch wenn Garmin in Zukunft neue Felder ergänzt
oder ich beim Schema etwas vergessen habe.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activities (
    garmin_id INTEGER PRIMARY KEY,
    name TEXT,
    type TEXT,
    sub_type TEXT,
    start_time TEXT,
    start_time_local TEXT,
    timezone TEXT,
    duration_seconds REAL,
    moving_duration_seconds REAL,
    elapsed_duration_seconds REAL,
    distance_m REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    avg_speed_ms REAL,
    max_speed_ms REAL,
    avg_pace_min_per_km REAL,
    avg_hr INTEGER,
    max_hr INTEGER,
    calories REAL,
    avg_cadence REAL,
    max_cadence REAL,
    avg_power REAL,
    max_power REAL,
    normalized_power REAL,
    training_effect_aerobic REAL,
    training_effect_anaerobic REAL,
    training_effect_label TEXT,
    training_load REAL,
    vo2max REAL,
    avg_temperature REAL,
    max_temperature REAL,
    min_temperature REAL,
    avg_running_cadence REAL,
    avg_stride_length REAL,
    avg_vertical_oscillation REAL,
    avg_vertical_ratio REAL,
    avg_ground_contact_time REAL,
    location_name TEXT,
    fit_file_path TEXT,
    raw_json TEXT NOT NULL,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time_local DESC);
CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(type);

CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(garmin_id) ON DELETE CASCADE,
    lap_index INTEGER NOT NULL,
    start_time TEXT,
    duration_seconds REAL,
    distance_m REAL,
    avg_speed_ms REAL,
    max_speed_ms REAL,
    avg_hr INTEGER,
    max_hr INTEGER,
    avg_cadence REAL,
    avg_power REAL,
    max_power REAL,
    normalized_power REAL,
    elevation_gain_m REAL,
    elevation_loss_m REAL,
    calories REAL,
    avg_temperature REAL,
    avg_stride_length REAL,
    avg_vertical_oscillation REAL,
    intensity TEXT,
    lap_trigger TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(activity_id, lap_index)
);

CREATE INDEX IF NOT EXISTS idx_laps_activity ON laps(activity_id, lap_index);

-- Pro-Sekunde-Datenpunkte aus dem FIT-File
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(garmin_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    timestamp TEXT,
    elapsed_seconds REAL,
    distance_m REAL,
    latitude REAL,
    longitude REAL,
    altitude_m REAL,
    enhanced_altitude_m REAL,
    speed_ms REAL,
    enhanced_speed_ms REAL,
    heart_rate INTEGER,
    cadence INTEGER,
    fractional_cadence REAL,
    power INTEGER,
    temperature REAL,
    grade REAL,
    vertical_oscillation REAL,
    vertical_ratio REAL,
    stance_time REAL,
    stance_time_balance REAL,
    step_length REAL,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_records_activity ON records(activity_id, seq);

CREATE TABLE IF NOT EXISTS sleep_days (
    date TEXT PRIMARY KEY,
    duration_seconds INTEGER,
    deep_seconds INTEGER,
    light_seconds INTEGER,
    rem_seconds INTEGER,
    awake_seconds INTEGER,
    sleep_score INTEGER,
    sleep_score_quality TEXT,
    avg_overnight_hrv REAL,
    hrv_status TEXT,
    avg_respiration REAL,
    lowest_respiration REAL,
    highest_respiration REAL,
    avg_spo2 INTEGER,
    lowest_spo2 INTEGER,
    body_battery_change INTEGER,
    restless_moments INTEGER,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_days (
    date TEXT PRIMARY KEY,
    resting_hr INTEGER,
    max_hr INTEGER,
    min_hr INTEGER,
    avg_stress INTEGER,
    max_stress INTEGER,
    rest_stress_duration INTEGER,
    low_stress_duration INTEGER,
    medium_stress_duration INTEGER,
    high_stress_duration INTEGER,
    body_battery_high INTEGER,
    body_battery_low INTEGER,
    body_battery_charged INTEGER,
    body_battery_drained INTEGER,
    hrv_weekly_avg REAL,
    hrv_last_night INTEGER,
    hrv_status TEXT,
    avg_spo2 INTEGER,
    avg_respiration REAL,
    steps INTEGER,
    floors_climbed INTEGER,
    intensity_minutes_moderate INTEGER,
    intensity_minutes_vigorous INTEGER,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_status (
    date TEXT PRIMARY KEY,
    vo2max_running REAL,
    vo2max_cycling REAL,
    training_status TEXT,
    training_status_feedback TEXT,
    acute_load REAL,
    chronic_load REAL,
    load_ratio REAL,
    fitness_age REAL,
    recovery_time_hours INTEGER,
    race_predictor_5k REAL,
    race_predictor_10k REAL,
    race_predictor_half REAL,
    race_predictor_marathon REAL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    activities_added INTEGER DEFAULT 0,
    health_days_added INTEGER DEFAULT 0,
    sleep_days_added INTEGER DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_msgs_session ON chat_messages(session_id, id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Schema anlegen (idempotent)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


# ─────────────────────────────────────────────────────────────────────────────
# Generic upsert helper
# ─────────────────────────────────────────────────────────────────────────────


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], pk: str) -> None:
    """Insert oder Update einer Zeile basierend auf Primary Key."""
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    col_list = ",".join(cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def existing_activity_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT garmin_id FROM activities").fetchall()
    return {r["garmin_id"] for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# JSON helper
# ─────────────────────────────────────────────────────────────────────────────


def to_json(obj: Any) -> str:
    """Sicher zu JSON serialisieren (mit defaults für nicht-serialisierbare Typen)."""
    return json.dumps(obj, default=str, ensure_ascii=False)
