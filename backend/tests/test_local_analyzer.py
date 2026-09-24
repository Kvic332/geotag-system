"""Unit tests for the rule-based intelligence engine (no DB, no API key)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from intelligence_engine.local_analyzer import analyze

HOME = {"cluster_lat": Decimal("6.500"), "cluster_lng": Decimal("3.350"),
        "night_pings": 400, "total_night_pings": 450, "confidence_pct": Decimal("88.9")}
OFFICE = {"cluster_lat": Decimal("6.428"), "cluster_lng": Decimal("3.422"),
          "day_pings": 500, "total_day_pings": 600, "share_pct": Decimal("83.3")}


def _stats(total: int, days: int, avg: float = 0.2, max_speed: float = 3.0,
           first: str = "2026-09-01", last: str = "2026-09-10") -> dict:
    return {
        "total_pings": total, "active_days": days,
        "avg_speed": Decimal(str(avg)), "max_speed": Decimal(str(max_speed)),
        "first_seen": datetime.fromisoformat(first).replace(tzinfo=timezone.utc),
        "last_seen": datetime.fromisoformat(last).replace(tzinfo=timezone.utc),
    }


def _hours(spec: dict[int, int]) -> list[dict]:
    return [{"hour_wat": h, "cnt": c} for h, c in spec.items()]


def _days(spec: dict[int, int]) -> list[dict]:
    return [{"dow": d, "cnt": c} for d, c in spec.items()]


def _run(**overrides):
    args = dict(stats=_stats(1000, 10), night=None, day=None, spread=None,
                geofences=[], hour_dist=[], day_dist=[])
    args.update(overrides)
    return analyze(**args)


def test_too_little_data_is_unknown_and_low_confidence() -> None:
    out = _run(stats=_stats(12, 1))
    assert out["location_type"] == "unknown"
    assert out["confidence"] == "low"
    assert "not enough data" in out["summary"]


def test_daytime_only_device_is_workplace_and_asks_for_overnight_data() -> None:
    out = _run(
        stats=_stats(330, 1, first="2026-09-24", last="2026-09-24"),
        day=OFFICE,
        spread={"places": 3, "top_lat": Decimal("6.428"), "top_lng": Decimal("3.422"),
                "top_share_pct": Decimal("91.0")},
        hour_dist=_hours({10: 100, 11: 120, 12: 80, 14: 30}),
        day_dist=_days({4: 330}),
    )
    assert out["location_type"] == "workplace"
    assert out["home_area"] == "6.428°N, 3.422°E"
    assert "no overnight pings" in out["summary"]
    assert out["confidence"] == "low"
    assert any("10:00–13:00" in p for p in out["patterns"])


def test_home_and_distinct_office_is_mixed_routine_with_high_confidence() -> None:
    out = _run(
        night=HOME, day=OFFICE,
        spread={"places": 6, "top_lat": Decimal("6.500"), "top_lng": Decimal("3.350"),
                "top_share_pct": Decimal("55.0")},
        hour_dist=_hours({h: 40 for h in range(24)}),
        day_dist=_days({1: 180, 2: 180, 3: 180, 4: 180, 5: 180, 6: 50, 0: 50}),
    )
    assert out["location_type"] == "mixed"
    assert out["home_area"] == "6.500°N, 3.350°E"
    assert "home–work routine" in out["summary"]
    assert out["confidence"] == "high"
    assert any("weekdays" in p for p in out["patterns"])


def test_fast_moving_device_is_transit_and_flags_speed_glitch() -> None:
    out = _run(
        stats=_stats(800, 5, avg=9.0, max_speed=70.0, first="2026-09-01", last="2026-09-05"),
        spread={"places": 40, "top_lat": Decimal("6.45"), "top_lng": Decimal("3.40"),
                "top_share_pct": Decimal("8.0")},
    )
    assert out["location_type"] == "transit_hub"
    assert any("GPS glitch" in a for a in out["anomalies"])


def test_tracking_gaps_are_reported() -> None:
    out = _run(stats=_stats(400, 3, first="2026-09-01", last="2026-09-20"))
    assert any("Tracking was off on 17 of the 20 days" in a for a in out["anomalies"])


def test_geofence_visits_become_patterns() -> None:
    out = _run(geofences=[
        {"geofence_name": "Office", "event_type": "enter", "cnt": 12},
        {"geofence_name": "Office", "event_type": "exit", "cnt": 12},
        {"geofence_name": "Gym", "event_type": "enter", "cnt": 1},
    ])
    assert "Entered the 'Office' zone 12 times." in out["patterns"]
    assert "Entered the 'Gym' zone 1 time." in out["patterns"]
