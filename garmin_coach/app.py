"""FastAPI-Anwendung — lokale Garmin Coach Web-UI."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config
from .ai import stream_chat
from .db import get_conn, init_db

log = logging.getLogger("garmin_coach.app")

app = FastAPI(title="Garmin Coach")
templates = Jinja2Templates(directory=str(config.ROOT / "garmin_coach" / "templates"))


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup() -> None:
    init_db()
    log.info("DB initialisiert. Port: %s", config.PORT)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_pace(min_per_km: float | None) -> str:
    if not min_per_km:
        return "—"
    total = int(min_per_km * 60)
    return f"{total // 60}:{total % 60:02d} min/km"


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _fmt_dist(meters: float | None) -> str:
    if not meters:
        return "—"
    return f"{meters / 1000:.2f} km"


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    today = date.today()
    d30 = (today - timedelta(days=30)).isoformat()

    with get_conn() as conn:
        stats = conn.execute("""
            SELECT
                COUNT(*) as total_activities,
                ROUND(SUM(distance_m)/1000.0, 1) as total_km,
                ROUND(SUM(duration_seconds)/3600.0, 1) as total_hours,
                ROUND(AVG(avg_hr)) as avg_hr,
                MAX(start_time_local) as last_sync
            FROM activities
        """).fetchone()

        recent = conn.execute("""
            SELECT garmin_id, name, type, start_time_local, distance_m,
                   duration_seconds, avg_pace_min_per_km, avg_hr, training_load
            FROM activities ORDER BY start_time_local DESC LIMIT 5
        """).fetchall()

        # Letzte 30 Tage pro Woche
        weekly = conn.execute("""
            SELECT strftime('%Y-%W', start_time_local) as week,
                   COUNT(*) as count,
                   ROUND(SUM(distance_m)/1000.0,1) as km
            FROM activities
            WHERE date(start_time_local) >= date(?)
            GROUP BY week ORDER BY week
        """, (d30,)).fetchall()

        ts = conn.execute(
            "SELECT vo2max_running, training_status, acute_load, chronic_load FROM training_status ORDER BY date DESC LIMIT 1"
        ).fetchone()

        sleep_avg = conn.execute("""
            SELECT ROUND(AVG(duration_seconds)/3600.0, 1) as avg_h,
                   ROUND(AVG(sleep_score)) as avg_score,
                   ROUND(AVG(avg_overnight_hrv), 1) as avg_hrv
            FROM sleep_days
            WHERE date >= date(?)
        """, (d30,)).fetchone()

    ctx = {
        "request": request,
        "stats": dict(stats) if stats else {},
        "recent": [dict(r) for r in recent],
        "weekly": [dict(w) for w in weekly],
        "training_status": dict(ts) if ts else {},
        "sleep_avg": dict(sleep_avg) if sleep_avg else {},
        "fmt_pace": _fmt_pace,
        "fmt_duration": _fmt_duration,
        "fmt_dist": _fmt_dist,
    }
    return templates.TemplateResponse("dashboard.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Activities
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/activities", response_class=HTMLResponse)
async def activities(
    request: Request,
    type: str = "",
    start: str = "",
    end: str = "",
    page: int = 1,
) -> HTMLResponse:
    per_page = 30
    offset = (page - 1) * per_page
    where, params = [], []
    if type:
        where.append("type = ?")
        params.append(type)
    if start:
        where.append("date(start_time_local) >= date(?)")
        params.append(start)
    if end:
        where.append("date(start_time_local) <= date(?)")
        params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) as c FROM activities {where_sql}", params).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT garmin_id, name, type, start_time_local, distance_m,
                       duration_seconds, avg_pace_min_per_km, avg_hr, max_hr,
                       elevation_gain_m, calories, training_load,
                       training_effect_aerobic, training_effect_anaerobic
                FROM activities {where_sql}
                ORDER BY start_time_local DESC LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()
        types = conn.execute("SELECT DISTINCT type FROM activities ORDER BY type").fetchall()

    return templates.TemplateResponse("activities.html", {
        "request": request,
        "activities": [dict(r) for r in rows],
        "types": [r["type"] for r in types],
        "total": total,
        "page": page,
        "per_page": per_page,
        "filter_type": type,
        "filter_start": start,
        "filter_end": end,
        "fmt_pace": _fmt_pace,
        "fmt_duration": _fmt_duration,
        "fmt_dist": _fmt_dist,
    })


@app.get("/activities/{garmin_id}", response_class=HTMLResponse)
async def activity_detail(request: Request, garmin_id: int) -> HTMLResponse:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM activities WHERE garmin_id = ?", (garmin_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Aktivität nicht gefunden")

        laps = conn.execute(
            "SELECT * FROM laps WHERE activity_id = ? ORDER BY lap_index", (garmin_id,)
        ).fetchall()

        # Records: max 500 Punkte für Charts
        total_rec = conn.execute(
            "SELECT COUNT(*) as c FROM records WHERE activity_id = ?", (garmin_id,)
        ).fetchone()["c"]
        step = max(1, total_rec // 500)
        records = conn.execute(
            """SELECT elapsed_seconds, heart_rate, speed_ms, altitude_m,
                      cadence, power, latitude, longitude
               FROM records WHERE activity_id = ? AND seq % ? = 0 ORDER BY seq""",
            (garmin_id, step),
        ).fetchall()

    activity = dict(row)
    # GPS-Track für Karte
    gps_track = [
        {"lat": r["latitude"], "lon": r["longitude"]}
        for r in records
        if r["latitude"] and r["longitude"]
    ]

    # Chart-Daten
    chart_data = {
        "elapsed": [r["elapsed_seconds"] for r in records],
        "hr": [r["heart_rate"] for r in records],
        "speed": [round(r["speed_ms"] * 3.6, 2) if r["speed_ms"] else None for r in records],
        "altitude": [r["altitude_m"] for r in records],
        "cadence": [r["cadence"] for r in records],
        "power": [r["power"] for r in records],
    }

    return templates.TemplateResponse("activity.html", {
        "request": request,
        "activity": activity,
        "laps": [dict(l) for l in laps],
        "gps_track": json.dumps(gps_track),
        "chart_data": json.dumps(chart_data),
        "has_gps": len(gps_track) > 10,
        "has_power": any(r["power"] for r in records),
        "fmt_pace": _fmt_pace,
        "fmt_duration": _fmt_duration,
        "fmt_dist": _fmt_dist,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Sleep
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/sleep", response_class=HTMLResponse)
async def sleep(request: Request, days: int = 30) -> HTMLResponse:
    start = (date.today() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date, duration_seconds, deep_seconds, light_seconds,
                      rem_seconds, awake_seconds, sleep_score, sleep_score_quality,
                      avg_overnight_hrv, hrv_status, avg_respiration,
                      avg_spo2, body_battery_change
               FROM sleep_days WHERE date >= ? ORDER BY date DESC""",
            (start,),
        ).fetchall()
    return templates.TemplateResponse("sleep.html", {
        "request": request,
        "sleep_days": [dict(r) for r in rows],
        "days": days,
        "chart_data": json.dumps({
            "dates": [r["date"] for r in rows][::-1],
            "duration_h": [round(r["duration_seconds"] / 3600, 2) if r["duration_seconds"] else None for r in rows][::-1],
            "deep_h": [round(r["deep_seconds"] / 3600, 2) if r["deep_seconds"] else None for r in rows][::-1],
            "score": [r["sleep_score"] for r in rows][::-1],
            "hrv": [r["avg_overnight_hrv"] for r in rows][::-1],
        }),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health", response_class=HTMLResponse)
async def health(request: Request, days: int = 30) -> HTMLResponse:
    start = (date.today() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date, resting_hr, avg_stress, max_stress,
                      body_battery_high, body_battery_low,
                      hrv_weekly_avg, hrv_last_night, hrv_status,
                      avg_spo2, steps, intensity_minutes_moderate, intensity_minutes_vigorous
               FROM health_days WHERE date >= ? ORDER BY date DESC""",
            (start,),
        ).fetchall()
    return templates.TemplateResponse("health.html", {
        "request": request,
        "health_days": [dict(r) for r in rows],
        "days": days,
        "chart_data": json.dumps({
            "dates": [r["date"] for r in rows][::-1],
            "resting_hr": [r["resting_hr"] for r in rows][::-1],
            "hrv": [r["hrv_last_night"] for r in rows][::-1],
            "stress": [r["avg_stress"] for r in rows][::-1],
            "bb_high": [r["body_battery_high"] for r in rows][::-1],
            "bb_low": [r["body_battery_low"] for r in rows][::-1],
            "steps": [r["steps"] for r in rows][::-1],
        }),
    })


# ─────────────────────────────────────────────────────────────────────────────
# AI Chat
# ─────────────────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    session_id: int | None = None


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    with get_conn() as conn:
        sessions = conn.execute(
            "SELECT id, title, updated_at FROM chat_sessions ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "sessions": [dict(s) for s in sessions],
        "has_api_key": bool(config.ANTHROPIC_API_KEY),
    })


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE-Stream für die Chat-UI."""
    def _gen():
        for event in stream_chat(req.message, req.history):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.post("/api/chat/session")
async def create_session(data: dict) -> JSONResponse:
    title = data.get("title", "Neues Gespräch")[:100]
    now = date.today().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_sessions(title, created_at, updated_at) VALUES(?,?,?)",
            (title, now, now),
        )
        sid = cur.lastrowid
    return JSONResponse({"id": sid})


@app.post("/api/chat/message")
async def save_message(data: dict) -> JSONResponse:
    sid = data["session_id"]
    role = data["role"]
    content = data["content"]
    now = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
            (sid, role, content, now),
        )
        conn.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, sid))
    return JSONResponse({"ok": True})


@app.get("/api/chat/session/{sid}")
async def get_session(sid: int) -> JSONResponse:
    with get_conn() as conn:
        msgs = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id",
            (sid,),
        ).fetchall()
    return JSONResponse({"messages": [dict(m) for m in msgs]})


# ─────────────────────────────────────────────────────────────────────────────
# Sync
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/api/sync")
async def trigger_sync() -> StreamingResponse:
    """Startet einen Sync und streamt Log-Zeilen."""
    def _gen():
        import traceback
        try:
            from .sync import full_sync
            yield "data: Sync gestartet...\n\n"
            result = full_sync()
            yield f"data: Fertig! {result}\n\n"
        except Exception as e:
            yield f"data: FEHLER: {e}\n\n"
            for line in traceback.format_exc().splitlines():
                yield f"data: {line}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")
