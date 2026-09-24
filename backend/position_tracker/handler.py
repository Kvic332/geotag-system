"""position_tracker Lambda.

Routes: POST /positions (single ping or batch, SPEC-DECISIONS D4),
GET /positions/{device_id}, GET /positions/{device_id}/history,
GET /positions/active.

Every write updates Postgres (source of truth) and best-effort updates Redis
(latest-position cache + `devices:active` ZSET, D5). Every read prefers Redis
and falls back to Postgres when Redis is degraded — CLAUDE.md: "Every Lambda
must handle Redis failure gracefully."

Enter/exit/dwell diffing is delegated to `event_processor.diffing` — see that
module's docstring for why position_tracker imports it directly instead of
this being an async trigger.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from shared import cache_keys, config
from shared.apigw import Router, query_params, require_json_object
from shared.auth import require_auth
from shared.db import connection, execute, query_all, query_one
from shared.errors import NotFoundError, ValidationError
from shared.logging_utils import get_logger
from shared.redis_client import redis_op, redis_pipeline
from shared.responses import api_handler, json_response
from shared.timeutil import to_iso, to_unix, utcnow
from shared.validation import Ping, parse_limit, parse_ping, parse_ping_request, parse_range

from event_processor.diffing import current_zone_ids, process_ping_events

logger = get_logger("geotag.position_tracker")
router = Router()

_MISSING = object()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _write_position(tenant_id: str, ping: Ping) -> None:
    """Persist one ping: Postgres (source of truth) then best-effort Redis."""
    execute(
        """
        INSERT INTO positions (tenant_id, device_id, location, accuracy, speed, bearing, battery, recorded_at)
        VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, device_id, recorded_at) DO UPDATE SET
            location = EXCLUDED.location, accuracy = EXCLUDED.accuracy, speed = EXCLUDED.speed,
            bearing = EXCLUDED.bearing, battery = EXCLUDED.battery
        """,
        (tenant_id, ping.device_id, ping.lng, ping.lat, ping.accuracy, ping.speed, ping.bearing, ping.battery, ping.recorded_at),
    )

    position_key = cache_keys.position(tenant_id, ping.device_id)
    active_key = cache_keys.active_devices(tenant_id)
    ping_unix = to_unix(ping.recorded_at)
    cutoff = ping_unix - config.ACTIVE_WINDOW_SECONDS

    def _apply(pipe: Any) -> None:
        pipe.hset(
            position_key,
            mapping={
                "lat": ping.lat,
                "lng": ping.lng,
                "accuracy": "" if ping.accuracy is None else ping.accuracy,
                "speed": "" if ping.speed is None else ping.speed,
                "bearing": "" if ping.bearing is None else ping.bearing,
                "battery": "" if ping.battery is None else ping.battery,
                "ts": ping_unix,
            },
        )
        pipe.expire(position_key, config.POSITION_TTL_SECONDS)
        pipe.zadd(active_key, {ping.device_id: ping_unix})
        pipe.zremrangebyscore(active_key, "-inf", cutoff)

    redis_pipeline("write_position", _apply)


def _process_one(
    tenant_id: str, ping: Ping, meta_cache: dict[str, dict[str, Any]] | None = None
) -> list[Any]:
    _write_position(tenant_id, ping)
    return process_ping_events(
        tenant_id, ping.device_id, ping.lat, ping.lng, ping.recorded_at, meta_cache=meta_cache
    )


def _single_event_shape(e: Any) -> dict[str, Any]:
    """API-CONTRACT single-ping event: event_type, geofence_id, geofence_name, dwell_ms (D7)."""
    return {
        "event_type": e.event_type,
        "geofence_id": e.geofence_id,
        "geofence_name": e.geofence_name,
        "dwell_ms": e.dwell_ms,
    }


def _batch_event_shape(e: Any) -> dict[str, Any]:
    """API-CONTRACT batch event: adds device_id and occurred_at (D7 adds dwell_ms everywhere)."""
    return {
        "device_id": e.device_id,
        "event_type": e.event_type,
        "geofence_id": e.geofence_id,
        "geofence_name": e.geofence_name,
        "occurred_at": to_iso(e.occurred_at),
        "dwell_ms": e.dwell_ms,
    }


@router.route("POST", "/positions")
def post_positions(event: dict[str, Any], _params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    body = require_json_object(event)
    raw_pings, is_batch = parse_ping_request(body)

    if not is_batch:
        ping = parse_ping(raw_pings[0])
        events = _process_one(ctx.tenant_id, ping)
        return json_response(
            201,
            {
                "device_id": ping.device_id,
                "recorded_at": to_iso(ping.recorded_at),
                "events": [_single_event_shape(e) for e in events],
            },
        )

    parsed: list[tuple[int, Ping]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_pings):
        try:
            parsed.append((index, parse_ping(raw)))
        except ValidationError as exc:
            rejected.append({"index": index, "error": exc.message})

    # D4: process in timestamp order regardless of array order.
    parsed.sort(key=lambda pair: pair[1].recorded_at)

    # One geofence-metadata cache for the whole batch instead of one query per
    # ping — the same handful of zones repeat across a batch in practice.
    meta_cache: dict[str, dict[str, Any]] = {}
    all_events: list[dict[str, Any]] = []
    # One transaction for the batch: a per-query BEGIN/COMMIT costs two extra
    # round trips each, which made a 300-ping backlog outlast the SDK timeout.
    with connection():
        for _index, ping in parsed:
            all_events.extend(
                _batch_event_shape(e) for e in _process_one(ctx.tenant_id, ping, meta_cache)
            )

    return json_response(
        201,
        {"accepted": len(parsed), "rejected": rejected, "events": all_events},
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _latest_from_postgres(tenant_id: str, device_id: str) -> dict[str, Any] | None:
    return query_one(
        """
        SELECT device_id, ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng,
               accuracy, speed, bearing, battery, recorded_at
          FROM positions
         WHERE tenant_id = %s AND device_id = %s
         ORDER BY recorded_at DESC
         LIMIT 1
        """,
        (tenant_id, device_id),
    )


def _num_or_none(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    return float(raw)


def _int_or_none(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    return int(float(raw))


@router.route("GET", "/positions/active")
def get_active(event: dict[str, Any], _params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    active_key = cache_keys.active_devices(ctx.tenant_id)
    now_unix = to_unix(utcnow())
    cutoff = now_unix - config.ACTIVE_WINDOW_SECONDS

    device_ids = redis_op(
        "zrevrangebyscore_active",
        lambda c: c.zrevrangebyscore(active_key, "+inf", cutoff),
        _MISSING,
    )

    if device_ids is not _MISSING:
        devices: list[dict[str, Any]] = []
        for device_id in device_ids:  # type: ignore[union-attr]
            position_key = cache_keys.position(ctx.tenant_id, device_id)
            hash_ = redis_op("hgetall_active_member", lambda c, k=position_key: c.hgetall(k), None)
            if not hash_:
                continue
            devices.append(
                {
                    "device_id": device_id,
                    "lat": _num_or_none(hash_.get("lat")),
                    "lng": _num_or_none(hash_.get("lng")),
                    "speed": _num_or_none(hash_.get("speed")),
                    "battery": _int_or_none(hash_.get("battery")),
                    "recorded_at": to_iso(datetime.fromtimestamp(int(hash_["ts"]), tz=timezone.utc)),
                }
            )
        return json_response(200, {"count": len(devices), "devices": devices})

    # Redis degraded: Postgres fallback (002_indexes.sql idx_positions_tenant_recent).
    cutoff_dt = utcnow() - timedelta(seconds=config.ACTIVE_WINDOW_SECONDS)
    rows = query_all(
        """
        SELECT DISTINCT ON (device_id) device_id, ST_Y(location::geometry) AS lat,
               ST_X(location::geometry) AS lng, speed, battery, recorded_at
          FROM positions
         WHERE tenant_id = %s AND recorded_at >= %s
         ORDER BY device_id, recorded_at DESC
        """,
        (ctx.tenant_id, cutoff_dt),
    )
    rows.sort(key=lambda row: row["recorded_at"], reverse=True)
    devices = [
        {
            "device_id": row["device_id"],
            "lat": row["lat"],
            "lng": row["lng"],
            "speed": row["speed"],
            "battery": row["battery"],
            "recorded_at": to_iso(row["recorded_at"]),
        }
        for row in rows
    ]
    return json_response(200, {"count": len(devices), "devices": devices})


@router.route("GET", "/positions/{device_id}/history")
def get_history(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    device_id = params["device_id"]
    q = query_params(event)

    default_from = utcnow() - timedelta(hours=24)
    start, end = parse_range(q.get("from"), q.get("to"), default_from)
    limit = parse_limit(q.get("limit"), default=config.HISTORY_DEFAULT_LIMIT, maximum=config.HISTORY_MAX_LIMIT)

    rows = query_all(
        """
        SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng,
               accuracy, speed, bearing, battery, recorded_at
          FROM positions
         WHERE tenant_id = %s AND device_id = %s AND recorded_at >= %s AND recorded_at <= %s
         ORDER BY recorded_at ASC
         LIMIT %s
        """,
        (ctx.tenant_id, device_id, start, end, limit),
    )

    positions = [
        {
            "lat": row["lat"],
            "lng": row["lng"],
            "accuracy": row["accuracy"],
            "speed": row["speed"],
            "bearing": row["bearing"],
            "battery": row["battery"],
            "recorded_at": to_iso(row["recorded_at"]),
        }
        for row in rows
    ]
    return json_response(200, {"device_id": device_id, "count": len(positions), "positions": positions})


@router.route("GET", "/positions/{device_id}")
def get_position(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    device_id = params["device_id"]

    position_key = cache_keys.position(ctx.tenant_id, device_id)
    hash_ = redis_op("hgetall_position", lambda c: c.hgetall(position_key), _MISSING)

    if hash_ is not _MISSING and hash_:
        source = "redis"
        lat = _num_or_none(hash_.get("lat"))
        lng = _num_or_none(hash_.get("lng"))
        accuracy = _num_or_none(hash_.get("accuracy"))
        speed = _num_or_none(hash_.get("speed"))
        bearing = _num_or_none(hash_.get("bearing"))
        battery = _int_or_none(hash_.get("battery"))
        recorded_at = to_iso(datetime.fromtimestamp(int(hash_["ts"]), tz=timezone.utc))
    else:
        row = _latest_from_postgres(ctx.tenant_id, device_id)
        if row is None:
            raise NotFoundError(f"no position known for device {device_id}")
        source = "postgres"
        lat = row["lat"]
        lng = row["lng"]
        accuracy = row["accuracy"]
        speed = row["speed"]
        bearing = row["bearing"]
        battery = row["battery"]
        recorded_at = to_iso(row["recorded_at"])

    zones = current_zone_ids(ctx.tenant_id, device_id)

    return json_response(
        200,
        {
            "device_id": device_id,
            "lat": lat,
            "lng": lng,
            "accuracy": accuracy,
            "speed": speed,
            "bearing": bearing,
            "battery": battery,
            "recorded_at": recorded_at,
            "source": source,
            "zones": zones,
        },
    )


@api_handler(logger)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return router.dispatch(event)
