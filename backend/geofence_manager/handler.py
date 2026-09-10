"""geofence_manager Lambda — GET/POST/PUT/DELETE /geofences.

Circle handling is SPEC-DECISIONS D2: the API accepts either a polygon ring or
a `{center, radius_m}` circle; circles are materialised into the `boundary`
polygon column with `ST_Buffer` on write and round-trip as circles on read
(via the stored `center`/`radius_m`), while containment always queries
`boundary` so `shared.geofence_engine` has exactly one code path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from shared import cache_keys
from shared.apigw import Router, require_json_object
from shared.auth import require_auth
from shared.config import CIRCLE_QUAD_SEGS
from shared.db import execute, query_all, query_one
from shared.errors import NotFoundError, ValidationError
from shared.logging_utils import get_logger
from shared.redis_client import redis_op, redis_pipeline
from shared.responses import api_handler, json_response
from shared.timeutil import to_iso
from shared.validation import GeofenceInput, parse_geofence_body

from event_processor.diffing import zone_device_count, zone_device_counts

logger = get_logger("geotag.geofence_manager")
router = Router()


def _as_uuid(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise NotFoundError("no such geofence") from None


def _row_to_geofence(tenant_id: str, row: dict[str, Any], device_count: int | None = None) -> dict[str, Any]:
    geojson = json.loads(row["boundary_geojson"])
    coordinates = geojson["coordinates"][0]  # exterior ring, already [lng, lat]

    center = None
    if row["shape_type"] == "circle" and row["center_lng"] is not None:
        center = [row["center_lng"], row["center_lat"]]

    return {
        "id": str(row["id"]),
        "name": row["name"],
        "shape_type": row["shape_type"],
        "coordinates": coordinates,
        "center": center,
        "radius_m": row["radius_m"],
        "dwell_threshold_seconds": row["dwell_threshold_seconds"],
        "created_at": to_iso(row["created_at"]),
        # Callers with more than one row (list_geofences) pass a pre-batched
        # count; single-row callers (create/get) fall back to one lookup.
        "device_count": zone_device_count(tenant_id, str(row["id"])) if device_count is None else device_count,
    }


_SELECT_COLUMNS = """
    id, name, shape_type, radius_m, dwell_threshold_seconds, created_at,
    ST_AsGeoJSON(boundary::geometry) AS boundary_geojson,
    ST_X(center::geometry) AS center_lng, ST_Y(center::geometry) AS center_lat
"""


@router.route("GET", "/geofences")
def list_geofences(event: dict[str, Any], _params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    rows = query_all(
        f"SELECT {_SELECT_COLUMNS} FROM geofences WHERE tenant_id = %s ORDER BY created_at DESC",
        (ctx.tenant_id,),
    )
    counts = zone_device_counts(ctx.tenant_id, [str(row["id"]) for row in rows])
    geofences = [_row_to_geofence(ctx.tenant_id, row, counts.get(str(row["id"]), 0)) for row in rows]
    return json_response(200, {"count": len(geofences), "geofences": geofences})


def _ring_to_wkt(ring: list[tuple[float, float]]) -> str:
    points = ", ".join(f"{lng} {lat}" for lng, lat in ring)
    return f"POLYGON(({points}))"


@router.route("POST", "/geofences")
def create_geofence(event: dict[str, Any], _params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    body = require_json_object(event)
    parsed: GeofenceInput = parse_geofence_body(body, partial=False)

    if parsed.shape_type == "polygon":
        assert parsed.ring is not None
        row = query_one(
            f"""
            INSERT INTO geofences (tenant_id, name, shape_type, boundary, center, radius_m, dwell_threshold_seconds)
            VALUES (%s, %s, %s, ST_GeogFromText(%s), NULL, NULL, %s)
            RETURNING {_SELECT_COLUMNS}
            """,
            (ctx.tenant_id, parsed.name, parsed.shape_type, _ring_to_wkt(parsed.ring), parsed.dwell_threshold_seconds),
        )
    else:
        assert parsed.center is not None and parsed.radius_m is not None
        lng, lat = parsed.center
        row = query_one(
            f"""
            INSERT INTO geofences (tenant_id, name, shape_type, boundary, center, radius_m, dwell_threshold_seconds)
            VALUES (%s, %s, %s,
                    ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s),
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s, %s)
            RETURNING {_SELECT_COLUMNS}
            """,
            (
                ctx.tenant_id, parsed.name, parsed.shape_type,
                lng, lat, parsed.radius_m, CIRCLE_QUAD_SEGS,
                lng, lat,
                parsed.radius_m, parsed.dwell_threshold_seconds,
            ),
        )

    assert row is not None
    return json_response(201, _row_to_geofence(ctx.tenant_id, row))


@router.route("PUT", "/geofences/{id}")
def update_geofence(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    geofence_id = _as_uuid(params["id"])
    body = require_json_object(event)
    parsed: GeofenceInput = parse_geofence_body(body, partial=True)

    set_clauses: list[str] = []
    values: list[Any] = []

    if parsed.name is not None:
        set_clauses.append("name = %s")
        values.append(parsed.name)

    if parsed.shape_type == "polygon":
        set_clauses.append("shape_type = %s")
        values.append("polygon")
        set_clauses.append("boundary = ST_GeogFromText(%s)")
        values.append(_ring_to_wkt(parsed.ring))  # type: ignore[arg-type]
        set_clauses.append("center = NULL")
        set_clauses.append("radius_m = NULL")
    elif parsed.shape_type == "circle":
        lng, lat = parsed.center  # type: ignore[misc]
        set_clauses.append("shape_type = %s")
        values.append("circle")
        set_clauses.append("boundary = ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)")
        values.extend([lng, lat, parsed.radius_m, CIRCLE_QUAD_SEGS])
        set_clauses.append("center = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography")
        values.extend([lng, lat])
        set_clauses.append("radius_m = %s")
        values.append(parsed.radius_m)

    if parsed.dwell_threshold_supplied:
        set_clauses.append("dwell_threshold_seconds = %s")
        values.append(parsed.dwell_threshold_seconds)

    if not set_clauses:
        raise ValidationError("no fields supplied to update")

    set_clauses.append("updated_at = NOW()")
    values.extend([ctx.tenant_id, geofence_id])

    row = query_one(
        f"""
        UPDATE geofences SET {", ".join(set_clauses)}
         WHERE tenant_id = %s AND id = %s
        RETURNING {_SELECT_COLUMNS}
        """,
        tuple(values),
    )
    if row is None:
        raise NotFoundError("no such geofence")

    return json_response(200, _row_to_geofence(ctx.tenant_id, row))


@router.route("DELETE", "/geofences/{id}")
def delete_geofence(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    geofence_id = _as_uuid(params["id"])

    deleted = execute(
        "DELETE FROM geofences WHERE tenant_id = %s AND id = %s",
        (ctx.tenant_id, geofence_id),
    )
    if deleted == 0:
        raise NotFoundError("no such geofence")

    # Clear every affected device's *own* zone-membership set too, not just
    # the zone's — leaving `device_zones` stale meant the next ping from a
    # device that was inside this zone computed an `exit` for a geofence_id
    # that no longer exists, which violated the device_events FK and — since
    # that failure happened before any cleanup could run — repeated on every
    # subsequent ping forever with no self-recovery. `diffing.process_ping_events`
    # now also tolerates a dangling reference defensively (belt and suspenders
    # for the case where Redis was down at delete time), but doing the
    # cleanup here means the common case self-heals immediately instead of
    # waiting for that device's next ping.
    members_key = cache_keys.zone_members(ctx.tenant_id, geofence_id)
    member_ids = redis_op("smembers_zone_members_on_delete", lambda c: c.smembers(members_key), None)

    def _cleanup(pipe: Any, member_ids: Any = member_ids) -> None:
        for device_id in member_ids or ():
            pipe.srem(cache_keys.device_zones(ctx.tenant_id, device_id), geofence_id)
            pipe.delete(cache_keys.entered_at(ctx.tenant_id, geofence_id, device_id))
            pipe.delete(cache_keys.dwelled(ctx.tenant_id, geofence_id, device_id))
        pipe.delete(members_key)

    redis_pipeline("delete_zone_members_on_delete", _cleanup)

    return json_response(204)


@api_handler(logger)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return router.dispatch(event)
