#!/usr/bin/env python3
"""Fetch and process Garmin Connect activities for GitHub Pages display.

Authentication via environment variables (priority order):
  1. GARMIN_TOKEN_BASE64  — base64-encoded garth token (no MFA needed)
  2. GARMIN_EMAIL + GARMIN_PASSWORD — credentials (MFA may be required)

Run generate_token.py locally once to create GARMIN_TOKEN_BASE64.
"""

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def get_api():
    """Initialize and return an authenticated Garmin API client."""
    from garminconnect import Garmin

    token_b64 = os.environ.get("GARMIN_TOKEN_BASE64")
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if token_b64:
        import garth

        print("Using saved token for authentication...")
        token_str = base64.b64decode(token_b64).decode()
        garth.client.loads(token_str)
        api = Garmin()
        api.garth = garth.client
        return api

    if email and password:
        print("Using email/password for authentication...")
        api = Garmin(email=email, password=password)
        api.login()
        return api

    print("ERROR: Set GARMIN_TOKEN_BASE64 or GARMIN_EMAIL + GARMIN_PASSWORD", file=sys.stderr)
    sys.exit(1)


def format_duration(seconds: int) -> str:
    """Format seconds as H:MM:SS or MM:SS."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def process_activity(raw: dict) -> dict:
    """Normalize a raw Garmin activity into a clean dict."""
    atype = (raw.get("activityType") or {}).get("typeKey", "unknown")

    distance_m = raw.get("distance") or 0
    distance_km = round(distance_m / 1000, 2)

    duration_s = raw.get("duration") or 0

    # Pace only for running-type activities
    run_types = {
        "running", "trail_running", "treadmill_running", "virtual_run",
        "indoor_running", "track_running",
    }
    pace = None
    if atype in run_types and distance_km >= 0.1:
        pace = round((duration_s / 60) / distance_km, 4)

    # Speed for cycling
    avg_speed_ms = raw.get("averageSpeed") or 0
    avg_speed_kmh = round(avg_speed_ms * 3.6, 1) if avg_speed_ms else None

    start_time = raw.get("startTimeLocal", "") or ""

    return {
        "id": raw.get("activityId"),
        "name": raw.get("activityName") or "",
        "date": start_time[:10],
        "datetime": start_time,
        "type": atype,
        "distance_km": distance_km,
        "duration_seconds": int(duration_s),
        "duration_formatted": format_duration(duration_s),
        "pace_min_per_km": pace,
        "avg_speed_kmh": avg_speed_kmh,
        "avg_heart_rate": raw.get("averageHR"),
        "max_heart_rate": raw.get("maxHR"),
        "calories": raw.get("calories"),
        "elevation_gain_m": raw.get("elevationGain"),
        "avg_cadence": raw.get("averageRunningCadenceInStepsPerMinute"),
        "training_effect": raw.get("aerobicTrainingEffect"),
        "vo2max": raw.get("vO2MaxValue"),
    }


def compute_stats(activities: list) -> dict:
    """Compute summary statistics."""
    year = datetime.now().year

    run_types = {
        "running", "trail_running", "treadmill_running", "virtual_run",
        "indoor_running", "track_running",
    }

    runs = [a for a in activities if a["type"] in run_types]
    runs_ytd = [a for a in runs if a["date"].startswith(str(year))]
    all_ytd = [a for a in activities if a["date"].startswith(str(year))]

    def total_km(acts):
        return round(sum(a["distance_km"] for a in acts), 1)

    def mean_pace(acts):
        paces = [a["pace_min_per_km"] for a in acts if a["pace_min_per_km"]]
        return round(sum(paces) / len(paces), 4) if paces else None

    def mean_hr(acts):
        hrs = [a["avg_heart_rate"] for a in acts if a["avg_heart_rate"]]
        return round(sum(hrs) / len(hrs)) if hrs else None

    # Best runs (top 5 by distance)
    top_runs = sorted(runs_ytd, key=lambda a: a["distance_km"], reverse=True)[:5]

    return {
        "current_year": year,
        "total_distance_km": total_km(runs),
        "total_runs": len(runs),
        "ytd_distance_km": total_km(runs_ytd),
        "ytd_runs": len(runs_ytd),
        "ytd_activities": len(all_ytd),
        "avg_pace_min_per_km": mean_pace(runs_ytd) or mean_pace(runs),
        "avg_heart_rate": mean_hr(runs_ytd) or mean_hr(runs),
        "longest_run_km": top_runs[0]["distance_km"] if top_runs else 0,
    }


def main():
    api = get_api()

    print("Fetching activities (up to 200)...")
    raw_activities = api.get_activities(0, 200)
    print(f"Fetched {len(raw_activities)} activities.")

    activities = [process_activity(a) for a in raw_activities]
    activities.sort(key=lambda a: a["datetime"], reverse=True)

    stats = compute_stats(activities)

    output = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": stats,
        "activities": activities,
    }

    out_path = Path(__file__).parent.parent / "data" / "activities.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    print(f"Saved to {out_path}")
    print(f"Stats: {stats}")


if __name__ == "__main__":
    main()
