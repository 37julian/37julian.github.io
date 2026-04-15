"""Garmin-Connect-Sync.

Lädt alle relevanten Daten herunter:
* Aktivitäten (Liste + jeweilige Detail-JSON + rohe FIT-Datei)
* Schlaf-Daten (täglich)
* Health-Metriken (HRV, Stress, Body Battery, Steps, Resting HR)
* Trainings-Status (VO2max, Acute/Chronic Load, Race Predictor)

Speichert alles in der SQLite-DB und FIT-Files unter ``data/fit/``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import garth
from garminconnect import Garmin

from . import config
from .db import get_conn, init_db, to_json, upsert
from .fit_parser import lap_to_db_row, parse_fit_file, record_to_db_row

log = logging.getLogger("garmin_coach.sync")


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────


def login(interactive: bool = False) -> Garmin:
    """Login zu Garmin Connect.

    Versucht zuerst, einen gespeicherten Token aus ``.garth/`` zu nutzen.
    Wenn das fehlschlägt und ``interactive=True`` ist, wird per E-Mail/Passwort
    eingeloggt (mit MFA-Prompt falls nötig) und der neue Token gespeichert.
    """
    # Token-Login versuchen
    if config.GARTH_DIR.exists() and any(config.GARTH_DIR.iterdir()):
        try:
            garth.resume(str(config.GARTH_DIR))
            api = Garmin()
            api.garth = garth.client
            # Sanity-Check
            api.get_full_name()
            log.info("Eingeloggt via gespeichertem Token.")
            return api
        except Exception as e:
            log.warning("Token-Login fehlgeschlagen (%s) — fallback auf Credentials.", e)

    if not interactive:
        raise RuntimeError(
            "Kein gültiger Garmin-Token. Führe einmalig 'python scripts/setup_auth.py' aus."
        )

    if not config.GARMIN_EMAIL or not config.GARMIN_PASSWORD:
        raise RuntimeError("GARMIN_EMAIL und GARMIN_PASSWORD müssen in .env gesetzt sein.")

    api = Garmin(email=config.GARMIN_EMAIL, password=config.GARMIN_PASSWORD)
    try:
        api.login()
    except Exception as e:
        if "MFA" in str(e) or "two-factor" in str(e).lower() or "TwoFactor" in type(e).__name__:
            mfa = input("MFA-Code: ").strip()
            api.login(mfa)
        else:
            raise

    # Token speichern
    config.GARTH_DIR.mkdir(parents=True, exist_ok=True)
    api.garth.dump(str(config.GARTH_DIR))
    log.info("Token gespeichert nach %s", config.GARTH_DIR)
    return api


# ─────────────────────────────────────────────────────────────────────────────
# Activity → DB row
# ─────────────────────────────────────────────────────────────────────────────


def _pace_min_per_km(speed_ms: float | None) -> float | None:
    if not speed_ms or speed_ms <= 0:
        return None
    return round((1000 / speed_ms) / 60, 4)


def _activity_to_row(a: dict[str, Any], fit_path: Path | None) -> dict[str, Any]:
    atype = (a.get("activityType") or {}).get("typeKey")
    sub_type = (a.get("eventType") or {}).get("typeKey")
    avg_speed = a.get("averageSpeed")

    return {
        "garmin_id": a["activityId"],
        "name": a.get("activityName"),
        "type": atype,
        "sub_type": sub_type,
        "start_time": a.get("startTimeGMT"),
        "start_time_local": a.get("startTimeLocal"),
        "timezone": a.get("timeZoneId"),
        "duration_seconds": a.get("duration"),
        "moving_duration_seconds": a.get("movingDuration"),
        "elapsed_duration_seconds": a.get("elapsedDuration"),
        "distance_m": a.get("distance"),
        "elevation_gain_m": a.get("elevationGain"),
        "elevation_loss_m": a.get("elevationLoss"),
        "avg_speed_ms": avg_speed,
        "max_speed_ms": a.get("maxSpeed"),
        "avg_pace_min_per_km": _pace_min_per_km(avg_speed),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "calories": a.get("calories"),
        "avg_cadence": a.get("averageBikingCadenceInRevPerMinute") or a.get("averageRunningCadenceInStepsPerMinute"),
        "max_cadence": a.get("maxBikingCadenceInRevPerMinute") or a.get("maxRunningCadenceInStepsPerMinute"),
        "avg_power": a.get("avgPower") or a.get("averagePower"),
        "max_power": a.get("maxPower"),
        "normalized_power": a.get("normPower") or a.get("normalizedPower"),
        "training_effect_aerobic": a.get("aerobicTrainingEffect"),
        "training_effect_anaerobic": a.get("anaerobicTrainingEffect"),
        "training_effect_label": a.get("trainingEffectLabel"),
        "training_load": a.get("activityTrainingLoad"),
        "vo2max": a.get("vO2MaxValue"),
        "avg_temperature": a.get("avgTemperature"),
        "max_temperature": a.get("maxTemperature"),
        "min_temperature": a.get("minTemperature"),
        "avg_running_cadence": a.get("averageRunningCadenceInStepsPerMinute"),
        "avg_stride_length": a.get("avgStrideLength"),
        "avg_vertical_oscillation": a.get("avgVerticalOscillation"),
        "avg_vertical_ratio": a.get("avgVerticalRatio"),
        "avg_ground_contact_time": a.get("avgGroundContactTime"),
        "location_name": a.get("locationName"),
        "fit_file_path": str(fit_path.relative_to(config.ROOT)) if fit_path else None,
        "raw_json": to_json(a),
        "synced_at": datetime.utcnow().isoformat() + "Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# FIT download
# ─────────────────────────────────────────────────────────────────────────────


def download_fit(api: Garmin, activity_id: int) -> Path | None:
    """Lädt die rohe FIT-Datei einer Aktivität herunter."""
    fit_path = config.FIT_DIR / f"{activity_id}.fit"
    if fit_path.exists() and fit_path.stat().st_size > 0:
        return fit_path

    try:
        # api.download_activity gibt Bytes zurück. Format: "ORIGINAL" liefert ein ZIP
        # mit der .fit-Datei drin (so handhabt Garmin Connect den Download).
        data = api.download_activity(activity_id, dl_fmt=api.ActivityDownloadFormat.ORIGINAL)
    except Exception as e:
        log.warning("FIT-Download für %s fehlgeschlagen: %s", activity_id, e)
        return None

    # Garmin packt's in ein ZIP
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".fit"):
                    fit_path.write_bytes(zf.read(name))
                    return fit_path
    except zipfile.BadZipFile:
        # Vielleicht direkt eine .fit
        if data[:4] in (b"PK\x03\x04",):  # eigentlich ZIP-Magic — nicht gut
            return None
        fit_path.write_bytes(data)
        return fit_path

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Activity-Sync
# ─────────────────────────────────────────────────────────────────────────────


def sync_activities(api: Garmin, limit: int | None = None) -> int:
    """Synchronisiert die letzten ``limit`` Aktivitäten.

    Wenn die Aktivität schon in der DB ist, werden FIT/Records/Laps NICHT
    erneut verarbeitet (das spart viel Zeit). Das Activity-Summary wird aber
    aktualisiert (z.B. wenn sich Trainings-Effekt-Werte nachträglich ändern).
    """
    limit = limit or config.ACTIVITY_SYNC_LIMIT
    log.info("Hole letzte %d Aktivitäten...", limit)
    activities = api.get_activities(0, limit)
    log.info("Erhalten: %d", len(activities))

    added = 0
    with get_conn() as conn:
        existing_with_records = {
            r["activity_id"]
            for r in conn.execute("SELECT DISTINCT activity_id FROM records").fetchall()
        }

        for a in activities:
            aid = a["activityId"]

            fit_path = download_fit(api, aid)
            row = _activity_to_row(a, fit_path)
            upsert(conn, "activities", row, pk="garmin_id")

            # Records & Laps nur einmal verarbeiten
            if aid in existing_with_records:
                continue

            if fit_path and fit_path.exists():
                try:
                    parsed = parse_fit_file(fit_path)
                except Exception as e:
                    log.warning("FIT-Parse für %s fehlgeschlagen: %s", aid, e)
                    parsed = None

                if parsed:
                    for idx, lap in enumerate(parsed["laps"]):
                        lap_row = lap_to_db_row(aid, idx, lap)
                        cols = ",".join(lap_row.keys())
                        ph = ",".join("?" for _ in lap_row)
                        conn.execute(
                            f"INSERT OR REPLACE INTO laps ({cols}) VALUES ({ph})",
                            list(lap_row.values()),
                        )

                    for seq, rec in enumerate(parsed["records"]):
                        rec_row = record_to_db_row(aid, seq, rec)
                        cols = ",".join(rec_row.keys())
                        ph = ",".join("?" for _ in rec_row)
                        conn.execute(
                            f"INSERT INTO records ({cols}) VALUES ({ph})",
                            list(rec_row.values()),
                        )

                    log.info(
                        "Aktivität %s: %d Laps, %d Records gespeichert.",
                        aid, len(parsed["laps"]), len(parsed["records"]),
                    )
            added += 1

    return added


# ─────────────────────────────────────────────────────────────────────────────
# Health- und Sleep-Sync
# ─────────────────────────────────────────────────────────────────────────────


def _safe(api_call, *args, **kwargs):
    """Wrapper, der Fehler abfängt und None zurückgibt."""
    try:
        return api_call(*args, **kwargs)
    except Exception as e:
        log.debug("API-Call fehlgeschlagen: %s", e)
        return None


def sync_sleep(api: Garmin, days: int | None = None) -> int:
    """Synchronisiert Schlaf-Daten der letzten ``days`` Tage."""
    days = days or config.HEALTH_SYNC_DAYS
    log.info("Synchronisiere Schlaf-Daten der letzten %d Tage...", days)

    today = date.today()
    added = 0

    with get_conn() as conn:
        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()

            sleep = _safe(api.get_sleep_data, d_str)
            if not sleep:
                continue

            dto = sleep.get("dailySleepDTO") or {}
            hrv = sleep.get("hrvData") or {}
            spo2 = sleep.get("wellnessSpO2SleepSummaryDTO") or {}
            resp = sleep.get("wellnessEpochRespirationDataDTOList") or []

            row = {
                "date": d_str,
                "duration_seconds": dto.get("sleepTimeSeconds"),
                "deep_seconds": dto.get("deepSleepSeconds"),
                "light_seconds": dto.get("lightSleepSeconds"),
                "rem_seconds": dto.get("remSleepSeconds"),
                "awake_seconds": dto.get("awakeSleepSeconds"),
                "sleep_score": (dto.get("sleepScores") or {}).get("overall", {}).get("value")
                    if isinstance((dto.get("sleepScores") or {}).get("overall"), dict)
                    else dto.get("sleepScores", {}).get("overall"),
                "sleep_score_quality": (dto.get("sleepScores") or {}).get("overall", {}).get("qualifierKey")
                    if isinstance((dto.get("sleepScores") or {}).get("overall"), dict)
                    else None,
                "avg_overnight_hrv": dto.get("avgOvernightHrv"),
                "hrv_status": dto.get("hrvStatus"),
                "avg_respiration": dto.get("averageRespirationValue"),
                "lowest_respiration": dto.get("lowestRespirationValue"),
                "highest_respiration": dto.get("highestRespirationValue"),
                "avg_spo2": dto.get("averageSpO2Value") or spo2.get("averageSpO2"),
                "lowest_spo2": dto.get("lowestSpO2Value") or spo2.get("lowestSpO2"),
                "body_battery_change": dto.get("bodyBatteryChange"),
                "restless_moments": dto.get("restlessMomentsCount"),
                "raw_json": to_json(sleep),
            }
            upsert(conn, "sleep_days", row, pk="date")
            added += 1

    log.info("Schlaf: %d Tage gespeichert.", added)
    return added


def sync_health(api: Garmin, days: int | None = None) -> int:
    """Synchronisiert tägliche Health-Metriken."""
    days = days or config.HEALTH_SYNC_DAYS
    log.info("Synchronisiere Health-Metriken der letzten %d Tage...", days)

    today = date.today()
    added = 0

    with get_conn() as conn:
        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()

            stats = _safe(api.get_stats, d_str) or {}
            stress = _safe(api.get_stress_data, d_str) or {}
            bb = _safe(api.get_body_battery, d_str, d_str) or []
            hrv = _safe(api.get_hrv_data, d_str) or {}
            spo2 = _safe(api.get_spo2_data, d_str) or {}

            if not (stats or stress or bb or hrv):
                continue

            bb_today = bb[0] if bb else {}
            hrv_summary = (hrv or {}).get("hrvSummary", {})

            row = {
                "date": d_str,
                "resting_hr": stats.get("restingHeartRate"),
                "max_hr": stats.get("maxHeartRate"),
                "min_hr": stats.get("minHeartRate"),
                "avg_stress": stats.get("averageStressLevel") or stress.get("avgStressLevel"),
                "max_stress": stats.get("maxStressLevel") or stress.get("maxStressLevel"),
                "rest_stress_duration": stats.get("restStressDuration"),
                "low_stress_duration": stats.get("lowStressDuration"),
                "medium_stress_duration": stats.get("mediumStressDuration"),
                "high_stress_duration": stats.get("highStressDuration"),
                "body_battery_high": bb_today.get("charged"),
                "body_battery_low": bb_today.get("drained"),
                "body_battery_charged": bb_today.get("charged"),
                "body_battery_drained": bb_today.get("drained"),
                "hrv_weekly_avg": hrv_summary.get("weeklyAvg"),
                "hrv_last_night": hrv_summary.get("lastNightAvg"),
                "hrv_status": hrv_summary.get("status"),
                "avg_spo2": spo2.get("averageSpO2") if isinstance(spo2, dict) else None,
                "avg_respiration": stats.get("avgWakingRespirationValue"),
                "steps": stats.get("totalSteps"),
                "floors_climbed": stats.get("floorsAscended"),
                "intensity_minutes_moderate": stats.get("moderateIntensityMinutes"),
                "intensity_minutes_vigorous": stats.get("vigorousIntensityMinutes"),
                "raw_json": to_json({
                    "stats": stats, "stress": stress, "bb": bb,
                    "hrv": hrv, "spo2": spo2,
                }),
            }
            upsert(conn, "health_days", row, pk="date")
            added += 1

    log.info("Health: %d Tage gespeichert.", added)
    return added


def sync_training_status(api: Garmin) -> int:
    """Holt den aktuellen Trainings-Status (VO2max, Load, Race Predictor)."""
    log.info("Hole Trainings-Status...")
    today = date.today().isoformat()

    status = _safe(api.get_training_status, today) or {}
    rp = _safe(api.get_race_predictions) or {}
    most_recent_vo2 = (status.get("mostRecentVO2Max") or {}).get("generic") or {}
    most_recent_vo2_cy = (status.get("mostRecentVO2Max") or {}).get("cycling") or {}
    load = (status.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
    # Pick first device's load
    load_metrics = next(iter(load.values())) if load else {}

    if not (status or rp):
        return 0

    row = {
        "date": today,
        "vo2max_running": most_recent_vo2.get("vo2MaxValue"),
        "vo2max_cycling": most_recent_vo2_cy.get("vo2MaxValue"),
        "training_status": (status.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatus"),
        "training_status_feedback": (status.get("mostRecentTrainingStatus") or {}).get("trainingStatusFeedbackPhrase"),
        "acute_load": load_metrics.get("acuteTrainingLoadDTO", {}).get("acwrPercent")
            if isinstance(load_metrics.get("acuteTrainingLoadDTO"), dict)
            else load_metrics.get("acuteTrainingLoad"),
        "chronic_load": load_metrics.get("chronicTrainingLoad"),
        "load_ratio": load_metrics.get("acwrPercent"),
        "fitness_age": (status.get("heatAltitudeAcclimationDTO") or {}).get("fitnessAge"),
        "recovery_time_hours": status.get("recoveryTime"),
        "race_predictor_5k": rp.get("time5K"),
        "race_predictor_10k": rp.get("time10K"),
        "race_predictor_half": rp.get("timeHalfMarathon"),
        "race_predictor_marathon": rp.get("timeMarathon"),
        "raw_json": to_json({"training_status": status, "race_predictions": rp}),
    }
    with get_conn() as conn:
        upsert(conn, "training_status", row, pk="date")

    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Vollständiger Sync
# ─────────────────────────────────────────────────────────────────────────────


def full_sync(activity_limit: int | None = None, health_days: int | None = None) -> dict:
    """Macht einen kompletten Sync und gibt eine Zusammenfassung zurück."""
    init_db()
    api = login()

    started = datetime.utcnow().isoformat() + "Z"
    summary = {"started_at": started, "errors": []}

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sync_log (started_at) VALUES (?)", (started,)
        )
        log_id = cur.lastrowid

    try:
        summary["activities_added"] = sync_activities(api, activity_limit)
    except Exception as e:
        log.exception("Activity-Sync fehlgeschlagen")
        summary["errors"].append(f"activities: {e}")

    try:
        summary["sleep_days_added"] = sync_sleep(api, health_days)
    except Exception as e:
        log.exception("Sleep-Sync fehlgeschlagen")
        summary["errors"].append(f"sleep: {e}")

    try:
        summary["health_days_added"] = sync_health(api, health_days)
    except Exception as e:
        log.exception("Health-Sync fehlgeschlagen")
        summary["errors"].append(f"health: {e}")

    try:
        sync_training_status(api)
    except Exception as e:
        log.exception("Training-Status-Sync fehlgeschlagen")
        summary["errors"].append(f"training_status: {e}")

    finished = datetime.utcnow().isoformat() + "Z"
    summary["finished_at"] = finished

    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_log SET finished_at=?, activities_added=?, "
            "sleep_days_added=?, health_days_added=?, error=? WHERE id=?",
            (
                finished,
                summary.get("activities_added", 0),
                summary.get("sleep_days_added", 0),
                summary.get("health_days_added", 0),
                "; ".join(summary["errors"]) or None,
                log_id,
            ),
        )

    return summary
