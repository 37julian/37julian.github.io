"""FIT-File-Parser.

Liest .fit-Dateien (Garmin-Originalformat) mit ``fitparse`` und extrahiert
ALLE Datenpunkte:

* ``record``-Messages → 1 Eintrag pro Sekunde mit HR/GPS/Cadence/Power/etc.
* ``lap``-Messages   → ein Eintrag pro Lap mit Aggregaten
* ``session``-Message → Gesamt-Aggregat (verifiziert die Summary)

Jeder Datenpunkt landet in der Datenbank — sowohl in eigenen Spalten als
auch als komplettes Roh-JSON, damit nichts verloren geht.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fitparse import FitFile

# ─────────────────────────────────────────────────────────────────────────────
# Field extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

# Garmin liefert Geo-Koordinaten als "semicircles".
# Umrechnung: 1 semicircle = 180 / 2^31 Grad.
SEMICIRCLE_TO_DEG = 180.0 / (2**31)


def _msg_to_dict(msg) -> dict[str, Any]:
    """Wandelt eine FIT-Message in ein flaches Dict um (alle Felder erhalten)."""
    out: dict[str, Any] = {}
    for field in msg:
        # ``field.value`` ist bereits umgerechnet (z.B. m/s, bpm), wenn fitparse
        # die Einheit kennt. Bei unbekannten Feldern fällt es auf ``raw_value`` zurück.
        val = field.value
        if val is None:
            continue
        # Datetime → ISO-String
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[field.name] = val
    return out


def _coord(value: Any) -> float | None:
    """Wandelt Semicircle-Koordinate in Dezimalgrad um."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Heuristik: Werte > 1000 sind Semicircles, sonst sind es schon Dezimalgrad
    if abs(v) > 1000:
        return v * SEMICIRCLE_TO_DEG
    return v


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def parse_fit_file(path: Path) -> dict[str, Any]:
    """Liest ein FIT-File und gibt strukturierte Daten zurück.

    Returns dict mit Keys:
        - ``session``: Gesamt-Session-Aggregat (oder None)
        - ``laps``:    Liste der Laps
        - ``records``: Liste der per-Sekunde-Datenpunkte
        - ``device_info``: Informationen zum Gerät
    """
    fit = FitFile(str(path))
    fit.parse()

    session: dict[str, Any] | None = None
    laps: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    device_info: list[dict[str, Any]] = []

    start_time = None

    for msg in fit.get_messages():
        name = msg.name
        data = _msg_to_dict(msg)

        if name == "session":
            session = data
            start_time = data.get("start_time")
        elif name == "lap":
            laps.append(data)
        elif name == "record":
            records.append(data)
        elif name == "device_info":
            device_info.append(data)

    # Berechne elapsed_seconds für jeden Record relativ zum Start
    if start_time and records:
        from datetime import datetime

        try:
            t0 = datetime.fromisoformat(start_time)
            for r in records:
                ts = r.get("timestamp")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts)
                        r["_elapsed_seconds"] = (dt - t0).total_seconds()
                    except (ValueError, TypeError):
                        pass
        except (ValueError, TypeError):
            pass

    return {
        "session": session,
        "laps": laps,
        "records": records,
        "device_info": device_info,
    }


def record_to_db_row(activity_id: int, seq: int, rec: dict[str, Any]) -> dict[str, Any]:
    """Mappt einen FIT-Record auf eine DB-Zeile für die ``records``-Tabelle."""
    import json

    return {
        "activity_id": activity_id,
        "seq": seq,
        "timestamp": rec.get("timestamp"),
        "elapsed_seconds": rec.get("_elapsed_seconds"),
        "distance_m": rec.get("distance"),
        "latitude": _coord(rec.get("position_lat")),
        "longitude": _coord(rec.get("position_long")),
        "altitude_m": rec.get("altitude"),
        "enhanced_altitude_m": rec.get("enhanced_altitude"),
        "speed_ms": rec.get("speed"),
        "enhanced_speed_ms": rec.get("enhanced_speed"),
        "heart_rate": rec.get("heart_rate"),
        "cadence": rec.get("cadence"),
        "fractional_cadence": rec.get("fractional_cadence"),
        "power": rec.get("power"),
        "temperature": rec.get("temperature"),
        "grade": rec.get("grade"),
        "vertical_oscillation": rec.get("vertical_oscillation"),
        "vertical_ratio": rec.get("vertical_ratio"),
        "stance_time": rec.get("stance_time"),
        "stance_time_balance": rec.get("stance_time_balance"),
        "step_length": rec.get("step_length"),
        "raw_json": json.dumps(rec, default=str, ensure_ascii=False),
    }


def lap_to_db_row(activity_id: int, idx: int, lap: dict[str, Any]) -> dict[str, Any]:
    """Mappt eine FIT-Lap-Message auf eine DB-Zeile."""
    import json

    return {
        "activity_id": activity_id,
        "lap_index": idx,
        "start_time": lap.get("start_time"),
        "duration_seconds": lap.get("total_elapsed_time") or lap.get("total_timer_time"),
        "distance_m": lap.get("total_distance"),
        "avg_speed_ms": lap.get("avg_speed") or lap.get("enhanced_avg_speed"),
        "max_speed_ms": lap.get("max_speed") or lap.get("enhanced_max_speed"),
        "avg_hr": lap.get("avg_heart_rate"),
        "max_hr": lap.get("max_heart_rate"),
        "avg_cadence": lap.get("avg_cadence") or lap.get("avg_running_cadence"),
        "avg_power": lap.get("avg_power"),
        "max_power": lap.get("max_power"),
        "normalized_power": lap.get("normalized_power"),
        "elevation_gain_m": lap.get("total_ascent"),
        "elevation_loss_m": lap.get("total_descent"),
        "calories": lap.get("total_calories"),
        "avg_temperature": lap.get("avg_temperature"),
        "avg_stride_length": lap.get("avg_step_length"),
        "avg_vertical_oscillation": lap.get("avg_vertical_oscillation"),
        "intensity": lap.get("intensity"),
        "lap_trigger": lap.get("lap_trigger"),
        "raw_json": json.dumps(lap, default=str, ensure_ascii=False),
    }
