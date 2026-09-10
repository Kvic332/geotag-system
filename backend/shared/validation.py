"""Request validation.

All user input is validated here so handlers only ever see well-formed values,
and so the wording of `validation_error` messages lives in one module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared import config
from shared.errors import ValidationError
from shared.timeutil import parse_time, utcnow

# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def as_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        raise ValidationError(f"{field} must be a finite number")
    return number


def as_optional_number(value: Any, field: str) -> float | None:
    return None if value is None else as_number(value, field)


def as_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def parse_limit(raw: str | None, default: int, maximum: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("limit must be an integer") from exc
    if limit < 1:
        raise ValidationError("limit must be >= 1")
    if limit > maximum:
        raise ValidationError(f"limit must be <= {maximum}")
    return limit


def parse_range(
    raw_from: str | None,
    raw_to: str | None,
    default_from: datetime,
    default_to: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Parse `from`/`to` query params, applying defaults and ordering rules."""
    start = parse_time(raw_from, "from") if raw_from else default_from
    end = parse_time(raw_to, "to") if raw_to else (default_to or utcnow())
    if start > end:
        raise ValidationError("from must not be after to")
    return start, end


# ---------------------------------------------------------------------------
# Position pings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ping:
    device_id: str
    lat: float
    lng: float
    accuracy: float | None
    speed: float | None
    bearing: float | None
    battery: int | None
    recorded_at: datetime


def parse_ping(raw: Any) -> Ping:
    """Validate one ping object. Raises `ValidationError` with a terse reason."""
    if not isinstance(raw, dict):
        raise ValidationError("ping must be a JSON object")

    device_id = as_non_empty_string(raw.get("device_id"), "device_id")

    if raw.get("lat") is None:
        raise ValidationError("lat is required")
    if raw.get("lng") is None:
        raise ValidationError("lng is required")

    lat = as_number(raw["lat"], "lat")
    lng = as_number(raw["lng"], "lng")
    if not -90.0 <= lat <= 90.0:
        raise ValidationError("lat out of range")
    if not -180.0 <= lng <= 180.0:
        raise ValidationError("lng out of range")

    accuracy = as_optional_number(raw.get("accuracy"), "accuracy")
    if accuracy is not None and accuracy < 0:
        raise ValidationError("accuracy out of range")

    speed = as_optional_number(raw.get("speed"), "speed")
    if speed is not None and speed < 0:
        raise ValidationError("speed out of range")

    bearing = as_optional_number(raw.get("bearing"), "bearing")
    if bearing is not None and not 0.0 <= bearing <= 360.0:
        raise ValidationError("bearing out of range")

    battery_raw = raw.get("battery")
    battery: int | None = None
    if battery_raw is not None:
        battery_number = as_number(battery_raw, "battery")
        if not 0 <= battery_number <= 100:
            raise ValidationError("battery out of range")
        battery = int(battery_number)

    timestamp = raw.get("timestamp")
    recorded_at = parse_time(timestamp, "timestamp") if timestamp is not None else utcnow()

    return Ping(
        device_id=device_id,
        lat=lat,
        lng=lng,
        accuracy=accuracy,
        speed=speed,
        bearing=bearing,
        battery=battery,
        recorded_at=recorded_at.replace(microsecond=0),
    )


def parse_ping_request(body: Any) -> tuple[list[Any], bool]:
    """Split a POST /positions body into (raw pings, is_batch) — SPEC-DECISIONS D4."""
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")

    if "pings" in body:
        pings = body["pings"]
        if not isinstance(pings, list):
            raise ValidationError("pings must be an array")
        if not pings:
            raise ValidationError("pings must not be empty")
        if len(pings) > config.MAX_BATCH_PINGS:
            raise ValidationError(f"pings must contain at most {config.MAX_BATCH_PINGS} items")
        return pings, True

    return [body], False


# ---------------------------------------------------------------------------
# Geofences (SPEC-DECISIONS D2)
# ---------------------------------------------------------------------------

Ring = list[tuple[float, float]]


def parse_ring(raw: Any) -> Ring:
    """Validate a polygon ring of `[lng, lat]` positions, closing it if needed."""
    if not isinstance(raw, list):
        raise ValidationError("coordinates must be an array of [lng, lat] positions")

    ring: Ring = []
    for index, position in enumerate(raw):
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise ValidationError(f"coordinates[{index}] must be a [lng, lat] pair")
        lng = as_number(position[0], f"coordinates[{index}][0]")
        lat = as_number(position[1], f"coordinates[{index}][1]")
        if not -180.0 <= lng <= 180.0:
            raise ValidationError(f"coordinates[{index}] lng out of range")
        if not -90.0 <= lat <= 90.0:
            raise ValidationError(f"coordinates[{index}] lat out of range")
        ring.append((lng, lat))

    if not ring:
        raise ValidationError("coordinates must not be empty")

    # API-CONTRACT: "the API closes an unclosed ring rather than rejecting it".
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    if len(ring) < 4:
        raise ValidationError("polygon ring must have at least 4 positions once closed")

    if len(set(ring[:-1])) < 3:
        raise ValidationError("polygon ring must have at least 3 distinct positions")

    return ring


def parse_center(raw: Any) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValidationError("center must be a [lng, lat] pair")
    lng = as_number(raw[0], "center[0]")
    lat = as_number(raw[1], "center[1]")
    if not -180.0 <= lng <= 180.0:
        raise ValidationError("center lng out of range")
    if not -90.0 <= lat <= 90.0:
        raise ValidationError("center lat out of range")
    return lng, lat


def parse_radius(raw: Any) -> float:
    radius = as_number(raw, "radius_m")
    if radius <= 0:
        raise ValidationError("radius_m must be > 0")
    if radius > config.MAX_RADIUS_M:
        raise ValidationError(f"radius_m must be <= {config.MAX_RADIUS_M}")
    return radius


def parse_dwell_threshold(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("dwell_threshold_seconds must be an integer or null")
    if raw <= 0:
        raise ValidationError("dwell_threshold_seconds must be > 0")
    if raw > config.MAX_DWELL_THRESHOLD_SECONDS:
        # SPEC-DECISIONS D3 addendum: the Redis entered/dwelled TTLs scale with
        # this value (see event_processor.diffing._dwell_state_ttl), so an
        # unbounded threshold would produce an unbounded Redis TTL. Capped at
        # a sane 30 days rather than left open-ended.
        raise ValidationError(
            f"dwell_threshold_seconds must be <= {config.MAX_DWELL_THRESHOLD_SECONDS}"
        )
    return raw


@dataclass(frozen=True)
class GeofenceInput:
    """Normalised geofence write. `None` means "leave unchanged" on PUT."""

    name: str | None
    shape_type: str | None
    ring: Ring | None
    center: tuple[float, float] | None
    radius_m: float | None
    dwell_threshold_seconds: int | None
    dwell_threshold_supplied: bool


def parse_geofence_body(body: Any, *, partial: bool) -> GeofenceInput:
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")

    name = as_non_empty_string(body["name"], "name") if "name" in body else None
    if not partial and name is None:
        raise ValidationError("name is required")

    shape_type: str | None = None
    ring: Ring | None = None
    center: tuple[float, float] | None = None
    radius_m: float | None = None

    if "type" in body:
        shape_type = as_non_empty_string(body.get("type"), "type").lower()
        if shape_type not in ("polygon", "circle"):
            raise ValidationError("type must be 'polygon' or 'circle'")
    elif not partial:
        raise ValidationError("type is required")
    elif "coordinates" in body or "center" in body or "radius_m" in body:
        raise ValidationError("type is required when changing geometry")

    if shape_type == "polygon":
        if "coordinates" not in body:
            raise ValidationError("coordinates is required for a polygon geofence")
        ring = parse_ring(body["coordinates"])
    elif shape_type == "circle":
        if "center" not in body:
            raise ValidationError("center is required for a circle geofence")
        if "radius_m" not in body:
            raise ValidationError("radius_m is required for a circle geofence")
        center = parse_center(body["center"])
        radius_m = parse_radius(body["radius_m"])

    dwell_supplied = "dwell_threshold_seconds" in body
    dwell = parse_dwell_threshold(body.get("dwell_threshold_seconds")) if dwell_supplied else None

    return GeofenceInput(
        name=name,
        shape_type=shape_type,
        ring=ring,
        center=center,
        radius_m=radius_m,
        dwell_threshold_seconds=dwell,
        dwell_threshold_supplied=dwell_supplied,
    )
