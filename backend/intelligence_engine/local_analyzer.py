"""Rule-based device intelligence — no external AI service or API key.

Turns the aggregates gathered by the intelligence handler into the same
fields the Claude-backed path returns (location_type, home_area, summary,
patterns, anomalies, confidence). Pure function: no I/O, easy to test.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

ENGINE_NAME = "geotag-rules-v1"

_WAT = timezone(timedelta(hours=1))

_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

MIN_PINGS = 30
MIN_CLUSTER_PINGS = 10
HOME_MIN_SHARE_PCT = 50.0
WORK_MIN_SHARE_PCT = 40.0
DISTINCT_PLACE_METRES = 250.0
MOBILE_AVG_SPEED_MS = 5.0          # 18 km/h averaged over every ping
MOBILE_MIN_PLACES = 15
MOBILE_MAX_TOP_SHARE_PCT = 25.0
HIGH_SPEED_MS = 33.0               # ~120 km/h
GLITCH_SPEED_MS = 55.0             # ~200 km/h — implausible on Lagos roads


def _f(value: Any, default: float = 0.0) -> float:
    return default if value is None else float(value)


def _kmh(ms: float) -> int:
    return round(ms * 3.6)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _fmt_point(lat: float, lng: float) -> str:
    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lng >= 0 else "W")
    return f"{abs(lat):.3f}°{ns}, {abs(lng):.3f}°{ew}"


def _fmt_date(value: datetime | None) -> str:
    return value.astimezone(_WAT).strftime("%d %b %Y") if value else "unknown"


def _busiest_window(hour_counts: dict[int, int], width: int = 3) -> tuple[int, int]:
    """Start hour of the busiest `width`-hour window (wrapping midnight) and its ping count."""
    best_start, best_total = 0, -1
    for start in range(24):
        total = sum(hour_counts.get((start + i) % 24, 0) for i in range(width))
        if total > best_total:
            best_start, best_total = start, total
    return best_start, best_total


def analyze(
    *,
    stats: dict[str, Any],
    night: dict[str, Any] | None,
    day: dict[str, Any] | None,
    spread: dict[str, Any] | None,
    geofences: list[dict[str, Any]],
    hour_dist: list[dict[str, Any]],
    day_dist: list[dict[str, Any]],
) -> dict[str, Any]:
    total = int(_f(stats.get("total_pings")))
    active_days = int(_f(stats.get("active_days")))
    avg_speed = _f(stats.get("avg_speed"))
    max_speed = _f(stats.get("max_speed"))
    first_seen, last_seen = stats.get("first_seen"), stats.get("last_seen")

    tracked = (
        f"Tracked on {active_days} day{'s' if active_days != 1 else ''} ({total} pings) "
        f"between {_fmt_date(first_seen)} and {_fmt_date(last_seen)}."
    )

    if total < MIN_PINGS:
        return {
            "location_type": "unknown",
            "home_area": None,
            "summary": f"{tracked} That is not enough data to describe a routine yet — "
                       f"keep tracking on and generate again once there are at least {MIN_PINGS} pings.",
            "patterns": [],
            "anomalies": [],
            "confidence": "low",
        }

    # --- places -------------------------------------------------------------
    home = None
    if night and int(_f(night.get("night_pings"))) >= MIN_CLUSTER_PINGS \
            and _f(night.get("confidence_pct")) >= HOME_MIN_SHARE_PCT:
        home = (_f(night["cluster_lat"]), _f(night["cluster_lng"]))

    work = None
    if day and int(_f(day.get("day_pings"))) >= MIN_CLUSTER_PINGS \
            and _f(day.get("share_pct")) >= WORK_MIN_SHARE_PCT:
        candidate = (_f(day["cluster_lat"]), _f(day["cluster_lng"]))
        if home is None or _haversine_m(*home, *candidate) > DISTINCT_PLACE_METRES:
            work = candidate

    places = int(_f(spread.get("places"))) if spread else 0
    top_share = _f(spread.get("top_share_pct")) if spread else 0.0
    top_place = (_f(spread["top_lat"]), _f(spread["top_lng"])) if spread and spread.get("top_lat") is not None else None

    hour_counts = {int(r["hour_wat"]): int(r["cnt"]) for r in hour_dist}
    daytime_share = 100.0 * sum(c for h, c in hour_counts.items() if 8 <= h < 19) / total
    has_night_data = night is not None and int(_f(night.get("total_night_pings"))) > 0

    mobile = avg_speed >= MOBILE_AVG_SPEED_MS or (
        places >= MOBILE_MIN_PLACES and top_share < MOBILE_MAX_TOP_SHARE_PCT
    )

    # --- classification + summary --------------------------------------------
    home_area = _fmt_point(*home) if home else None
    if mobile:
        location_type = "transit_hub"
        routine = (f"The device moves a lot — {places} distinct places and an average speed of "
                   f"{_kmh(avg_speed)} km/h — which fits a driver, courier or frequent commuter.")
    elif home and work:
        location_type = "mixed"
        km = _haversine_m(*home, *work) / 1000
        routine = (f"Nights are spent around {_fmt_point(*home)} and weekday daytime around a second "
                   f"location {km:.1f} km away, which points to a regular home–work routine.")
    elif home:
        location_type = "residential"
        routine = (f"Nights are consistently spent around {_fmt_point(*home)}, "
                   "which is most likely the owner's residence.")
    elif work or (top_share >= 60 and daytime_share >= 70):
        location_type = "workplace"
        base = work or top_place
        home_area = home_area or (_fmt_point(*base) if base else None)
        routine = (f"Most activity happens during the day at one location ({top_share:.0f}% of pings), "
                   "which looks like a workplace or daytime base.")
    elif top_share >= 60:
        location_type = "residential"
        home_area = home_area or (_fmt_point(*top_place) if top_place else None)
        routine = f"The device stays at one location most of the time ({top_share:.0f}% of pings)."
    else:
        location_type = "mixed" if places >= 5 else "unknown"
        routine = f"Activity is spread over {places} places with no single dominant location."

    summary = f"{tracked} {routine}"
    if not has_night_data:
        summary += " There are no overnight pings yet, so a home location can't be inferred — leave tracking on overnight."

    # --- patterns -------------------------------------------------------------
    patterns: list[str] = []
    if hour_counts:
        start, count = _busiest_window(hour_counts)
        if count / total >= 0.25:  # twice what an evenly spread day would put in 3 hours
            patterns.append(f"Busiest period is {start:02d}:00–{(start + 3) % 24:02d}:00 WAT "
                            f"({round(100 * count / total)}% of pings).")
        elif len(hour_counts) >= 20:
            patterns.append("Active around the clock with no single peak hour.")

    day_counts = {int(r["dow"]): int(r["cnt"]) for r in day_dist}
    if day_counts and active_days >= 2:
        weekday_share = 100.0 * sum(c for d, c in day_counts.items() if 1 <= d <= 5) / total
        busiest = max(day_counts, key=lambda d: day_counts[d])
        if active_days >= 3 and weekday_share >= 75:
            patterns.append(f"Mostly active on weekdays ({weekday_share:.0f}% of pings), busiest on {_DOW_NAMES[busiest]}.")
        elif active_days >= 3 and weekday_share <= 40:
            patterns.append(f"Mostly active at weekends ({100 - weekday_share:.0f}% of pings).")
        else:
            patterns.append(f"Busiest day so far is {_DOW_NAMES[busiest]}.")

    if avg_speed < 0.5:
        patterns.append("Mostly stationary — little movement between pings.")
    elif not mobile:
        patterns.append(f"Moves at an average of {_kmh(avg_speed)} km/h when tracked.")

    if top_place and not mobile:
        patterns.append(f"Spends {top_share:.0f}% of recorded time within about 100 m of {_fmt_point(*top_place)}.")

    enters: dict[str, int] = {}
    for row in geofences:
        if row["event_type"] == "enter":
            enters[row["geofence_name"]] = enters.get(row["geofence_name"], 0) + int(row["cnt"])
    for name, count in sorted(enters.items(), key=lambda kv: -kv[1])[:2]:
        patterns.append(f"Entered the '{name}' zone {count} time{'s' if count != 1 else ''}.")

    # --- anomalies ------------------------------------------------------------
    anomalies: list[str] = []
    if max_speed >= GLITCH_SPEED_MS:
        anomalies.append(f"Speed spike of {_kmh(max_speed)} km/h — most likely a GPS glitch.")
    elif max_speed >= HIGH_SPEED_MS:
        anomalies.append(f"Reached {_kmh(max_speed)} km/h — unusually fast travel.")

    if first_seen and last_seen:
        span_days = (last_seen.date() - first_seen.date()).days + 1
        if span_days >= 3 and active_days / span_days < 0.5:
            anomalies.append(f"Tracking was off on {span_days - active_days} of the {span_days} days in the record.")

    # --- confidence -----------------------------------------------------------
    if active_days >= 7 and total >= 500 and home is not None:
        confidence = "high"
    elif active_days >= 2 and total >= 100:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "location_type": location_type,
        "home_area": home_area,
        "summary": summary,
        "patterns": patterns[:5],
        "anomalies": anomalies,
        "confidence": confidence,
    }
