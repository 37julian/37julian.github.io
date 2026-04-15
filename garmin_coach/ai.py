"""Claude-AI-Integration mit Tool Use.

Claude bekommt eine Reihe von Tools, mit denen es gezielt SQL-Queries gegen
die lokale DB ausführen kann — statt alle Daten in den Kontext zu pumpen.

Architektur:
* Manueller Agentic Loop (statt Tool Runner) → erlaubt Streaming &
  Tool-Call-Anzeige in der UI.
* Prompt Caching auf System-Prompt + Tool-Definitionen (stabil → cache hits).
* Adaptive Thinking: Claude entscheidet selbst, wie tief es nachdenkt.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Iterator

import anthropic

from . import config
from .db import get_conn

log = logging.getLogger("garmin_coach.ai")

# ─────────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_activities",
        "description": (
            "Listet Aktivitäten zwischen zwei Daten auf. Gibt Übersichts-Felder zurück "
            "(Datum, Name, Typ, Distanz, Dauer, Pace, Avg HF, Höhenmeter, Training Effect). "
            "Nutze das, um zu sehen welche Aktivitäten in einem Zeitraum stattfanden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD, inklusive. Optional."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, inklusive. Optional."},
                "activity_type": {
                    "type": "string",
                    "description": "Optional: 'running', 'cycling', 'swimming', 'strength_training' etc.",
                },
                "limit": {"type": "integer", "description": "Maximale Anzahl Ergebnisse. Default 50, max 200."},
            },
        },
    },
    {
        "name": "get_activity",
        "description": (
            "Liefert komplette Detail-Daten EINER Aktivität: alle Metadaten, "
            "Trainings-Effekt, VO2max-Beitrag, Power-Werte, Laufdynamik. "
            "Enthält NICHT die Sekunden-Records oder Laps — dafür separate Tools nutzen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"garmin_id": {"type": "integer"}},
            "required": ["garmin_id"],
        },
    },
    {
        "name": "get_activity_laps",
        "description": (
            "Liefert alle Laps einer Aktivität (Lap = Runde/Abschnitt). "
            "Pro Lap: Distanz, Dauer, Pace, HF, Power, Höhenmeter. "
            "Wichtig für Intervalltrainings-Analyse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"garmin_id": {"type": "integer"}},
            "required": ["garmin_id"],
        },
    },
    {
        "name": "get_activity_records",
        "description": (
            "Liefert die Sekunden-genauen Sensor-Datenpunkte einer Aktivität "
            "(HF, GPS, Cadence, Power, Geschwindigkeit, Höhe, Temperatur, "
            "Vertical Oscillation, Stride Length etc.). "
            "ACHTUNG: Datasets sind groß (oft 5000+ Punkte). Nutze IMMER "
            "downsample_to_n um die Datenmenge zu begrenzen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "garmin_id": {"type": "integer"},
                "downsample_to_n": {
                    "type": "integer",
                    "description": "Auf wie viele Punkte downsamplen. Empfohlen: 100-500.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: nur diese Felder. Verfügbar: "
                        "elapsed_seconds, distance_m, heart_rate, cadence, power, "
                        "speed_ms, altitude_m, temperature, grade, latitude, longitude, "
                        "vertical_oscillation, step_length, stance_time."
                    ),
                },
            },
            "required": ["garmin_id"],
        },
    },
    {
        "name": "get_sleep",
        "description": (
            "Schlafdaten für einen Datumsbereich. Pro Tag: Gesamtdauer, "
            "Tiefschlaf/Light/REM/Awake, Sleep Score, Overnight HRV, Atemfrequenz, SpO2."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_health",
        "description": (
            "Tägliche Health-Metriken: Resting HF, Stress, Body Battery, HRV, SpO2, Steps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_training_status",
        "description": (
            "Aktueller Trainings-Status: VO2max (Running & Cycling), Trainings-Status-Label, "
            "Acute/Chronic Load, Recovery Time, Race Predictor (5K/10K/Halb/Marathon)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_sql",
        "description": (
            "Führt ein read-only SELECT-Statement gegen die lokale SQLite-DB aus. "
            "Tabellen: activities, laps, records, sleep_days, health_days, training_status. "
            "NUR SELECT — INSERT/UPDATE/DELETE wird abgewiesen. Limit 1000 Zeilen automatisch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "Ein einzelnes SELECT-Statement."}},
            "required": ["sql"],
        },
        "cache_control": {"type": "ephemeral"},  # Cache-Breakpoint nach letztem Tool
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Tool-Implementierungen
# ─────────────────────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def tool_list_activities(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    activity_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if start_date:
        where.append("date(start_time_local) >= date(?)")
        params.append(start_date)
    if end_date:
        where.append("date(start_time_local) <= date(?)")
        params.append(end_date)
    if activity_type:
        where.append("type = ?")
        params.append(activity_type)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT garmin_id, name, type, start_time_local,
               distance_m, duration_seconds, avg_pace_min_per_km,
               avg_hr, max_hr, calories, elevation_gain_m,
               training_effect_aerobic, training_effect_anaerobic,
               training_load, vo2max
        FROM activities {where_sql}
        ORDER BY start_time_local DESC LIMIT ?
    """
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def tool_get_activity(garmin_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM activities WHERE garmin_id = ?", (garmin_id,)).fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    if d.get("raw_json"):
        try:
            d["raw_garmin_data"] = json.loads(d.pop("raw_json"))
        except Exception:
            pass
    return d


def tool_get_activity_laps(garmin_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT lap_index, start_time, duration_seconds, distance_m,
                      avg_speed_ms, max_speed_ms, avg_hr, max_hr,
                      avg_cadence, avg_power, max_power, normalized_power,
                      elevation_gain_m, elevation_loss_m, calories,
                      avg_temperature, avg_stride_length, avg_vertical_oscillation,
                      intensity, lap_trigger
               FROM laps WHERE activity_id = ? ORDER BY lap_index""",
            (garmin_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def tool_get_activity_records(
    garmin_id: int,
    downsample_to_n: int = 200,
    fields: Optional[list] = None,
) -> dict:
    n = max(10, min(int(downsample_to_n or 200), 2000))
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM records WHERE activity_id = ?", (garmin_id,)
        ).fetchone()["c"]
        if total == 0:
            return {"total": 0, "sampled": 0, "records": []}
        step = max(1, total // n)
        allowed = {
            "elapsed_seconds", "distance_m", "heart_rate", "cadence", "power",
            "speed_ms", "altitude_m", "temperature", "grade", "latitude", "longitude",
            "vertical_oscillation", "step_length", "stance_time",
        }
        if fields:
            picked = [f for f in fields if f in allowed]
            col_sql = ", ".join(picked) if picked else ", ".join(allowed)
        else:
            col_sql = ", ".join(allowed)
        rows = conn.execute(
            f"SELECT {col_sql} FROM records WHERE activity_id = ? AND seq % ? = 0 ORDER BY seq",
            (garmin_id, step),
        ).fetchall()
    return {
        "total_records_in_db": total,
        "sampled": len(rows),
        "downsample_step": step,
        "records": [_row_to_dict(r) for r in rows],
    }


def tool_get_sleep(start_date: str, end_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date, duration_seconds, deep_seconds, light_seconds,
                      rem_seconds, awake_seconds, sleep_score, avg_overnight_hrv,
                      hrv_status, avg_respiration, avg_spo2, body_battery_change
               FROM sleep_days WHERE date BETWEEN ? AND ? ORDER BY date DESC""",
            (start_date, end_date),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def tool_get_health(start_date: str, end_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date, resting_hr, max_hr, avg_stress, max_stress,
                      body_battery_high, body_battery_low,
                      hrv_weekly_avg, hrv_last_night, hrv_status,
                      avg_spo2, avg_respiration, steps,
                      intensity_minutes_moderate, intensity_minutes_vigorous
               FROM health_days WHERE date BETWEEN ? AND ? ORDER BY date DESC""",
            (start_date, end_date),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def tool_get_training_status() -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM training_status ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def tool_query_sql(sql: str) -> dict:
    s = sql.strip().rstrip(";")
    lower = s.lower()
    if not lower.startswith("select") and not lower.startswith("with"):
        return {"error": "Nur SELECT/WITH erlaubt."}
    forbidden = ("insert", "update", "delete", "drop", "alter", "create", "attach", "pragma")
    if any(f"{kw} " in lower for kw in forbidden):
        return {"error": "Schreibende Statements sind nicht erlaubt."}
    if " limit " not in lower:
        s = f"{s} LIMIT 1000"
    try:
        with get_conn() as conn:
            rows = conn.execute(s).fetchall()
        return {"row_count": len(rows), "rows": [_row_to_dict(r) for r in rows]}
    except Exception as e:
        return {"error": str(e)}


TOOL_IMPLS = {
    "list_activities": tool_list_activities,
    "get_activity": tool_get_activity,
    "get_activity_laps": tool_get_activity_laps,
    "get_activity_records": tool_get_activity_records,
    "get_sleep": tool_get_sleep,
    "get_health": tool_get_health,
    "get_training_status": tool_get_training_status,
    "query_sql": tool_query_sql,
}


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────────────────────────────────────


def build_system_prompt() -> str:
    today = date.today().isoformat()
    return f"""Du bist ein erfahrener Trainings- und Gesundheits-Coach mit tiefer Expertise in Lauf-, Rad- und Ausdauersport. Du analysierst die Garmin-Daten des Users wissenschaftlich fundiert und gibst konkrete, umsetzbare Empfehlungen.

Heutiges Datum: {today}

Deine Daten-Quellen (über Tools abrufbar):
* **activities** — alle Workouts mit Distanz, Pace, HF, Power, Trainingseffekt, VO2max-Beitrag etc.
* **laps** — Lap-/Intervall-Daten pro Aktivität (essentiell für Intervall-Analyse)
* **records** — Sekunden-genaue Sensor-Daten (HF, GPS, Cadence, Power, Höhe, Temperatur, Lauftechnik)
* **sleep_days** — Schlafphasen, Sleep Score, Overnight HRV, Atemfrequenz
* **health_days** — Resting HF, Stress, Body Battery, HRV-Status, Steps
* **training_status** — VO2max, Acute/Chronic Load, Race Predictor

ARBEITSWEISE:
1. **Erst recherchieren, dann antworten.** Nutze die Tools aktiv — rate niemals.
2. **Sei sparsam mit get_activity_records** — nutze IMMER downsample_to_n (100-500) und ggf. fields-Filter.
3. **Für Trends:** Hole Daten über mehrere Wochen/Monate. Nutze list_activities mit Datumsbereich.
4. **Für Tiefenanalysen:** get_activity + get_activity_laps + get_activity_records(downsample_to_n=200).
5. **Sei konkret:** Gib Zahlen, Pace-Werte, HF-Zonen, Trends in % an.

ANTWORT-STIL:
* Auf Deutsch (außer der User schreibt Englisch)
* Klar strukturiert mit Markdown (Überschriften, Listen, Tabellen)
* Wissenschaftlich fundiert, aber zugänglich
* Bei Empfehlungen: Begründung mit den konkreten Daten

EINHEITEN: Distanz in km, Pace in min:sek/km, Power in Watt, HF in bpm, Geschwindigkeit (Rad) in km/h
"""


# ─────────────────────────────────────────────────────────────────────────────
# Streaming Chat
# ─────────────────────────────────────────────────────────────────────────────


def stream_chat(
    user_message: str,
    history: Optional[list] = None,
) -> Iterator[dict]:
    """Streamt einen Chat-Turn. Yields events:
    {"type": "thinking_delta"|"text_delta"|"tool_use"|"tool_result"|"done"|"error", ...}
    """
    if not config.ANTHROPIC_API_KEY:
        yield {"type": "error", "text": "ANTHROPIC_API_KEY nicht gesetzt."}
        return

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    messages = list(history or [])
    messages.append({"role": "user", "content": user_message})
    system_prompt = build_system_prompt()

    for _ in range(12):
        with client.messages.stream(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield {"type": "text_delta", "text": event.delta.text}
                    elif event.delta.type == "thinking_delta":
                        yield {"type": "thinking_delta", "text": event.delta.thinking}
            response = stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            yield {
                "type": "done",
                "stop_reason": response.stop_reason,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
                },
            }
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            yield {"type": "tool_use", "name": block.name, "input": block.input}
            try:
                result = TOOL_IMPLS[block.name](**block.input)
                result_str = json.dumps(result, default=str, ensure_ascii=False)
                summary = _summarize_result(block.name, result)
            except Exception as e:
                result_str = json.dumps({"error": str(e)})
                summary = f"Fehler: {e}"
                log.exception("Tool %s fehlgeschlagen", block.name)
            yield {"type": "tool_result", "name": block.name, "summary": summary}
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_str})

        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "text": "Maximale Iterationen erreicht."}


def _summarize_result(tool_name: str, result: Any) -> str:
    if result is None:
        return "Keine Daten"
    if isinstance(result, list):
        return f"{len(result)} Einträge"
    if isinstance(result, dict):
        if "error" in result:
            return f"Fehler: {result['error']}"
        if "row_count" in result:
            return f"{result['row_count']} Zeilen"
        if "sampled" in result:
            return f"{result['sampled']} von {result.get('total_records_in_db', '?')} Records"
    return "OK"
