"""event_processor Lambda — GET /events.

CLAUDE.md describes this Lambda as "triggered by AWS Location Service". Per
SPEC-DECISIONS D1, the real (tested) geofencing path is PostGIS, driven
synchronously from `position_tracker` on every ping — there is no local
equivalent of an async Location Service trigger, so that half of this
Lambda's CLAUDE.md description has no local entry point. The diffing logic
itself lives in `event_processor.diffing` (imported by `position_tracker`
and `geofence_manager` as well as used here) — see that module's docstring.

This handler's own job is the one route the API contract actually gives it:
`GET /events`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.apigw import Router, query_params
from shared.auth import require_auth
from shared.db import query_all
from shared.errors import ValidationError
from shared.logging_utils import get_logger
from shared.responses import api_handler, json_response
from shared.timeutil import parse_time, to_iso
from shared.validation import parse_limit, parse_range

logger = get_logger("geotag.event_processor")
router = Router()

_VALID_EVENT_TYPES = {"enter", "exit", "dwell"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # GET /events "from" default: no lower bound


def _encode_cursor(occurred_at_iso: str, event_id: str) -> str:
    return f"{occurred_at_iso}|{event_id}"


def _decode_cursor(raw: str) -> tuple[str, str]:
    try:
        occurred_at_iso, event_id = raw.split("|", 1)
    except ValueError as exc:
        raise ValidationError("cursor is malformed") from exc
    if not occurred_at_iso or not event_id:
        raise ValidationError("cursor is malformed")
    return occurred_at_iso, event_id


@router.route("GET", "/events")
def list_events(event: dict[str, Any], _params: dict[str, str]) -> dict[str, Any]:
    ctx = require_auth(event)
    q = query_params(event)

    device_id = q.get("device_id")
    geofence_id = q.get("geofence_id")
    event_type = q.get("event_type")
    if event_type is not None and event_type not in _VALID_EVENT_TYPES:
        raise ValidationError("event_type must be one of enter, exit, dwell")

    start, end = parse_range(q.get("from"), q.get("to"), _EPOCH)
    limit = parse_limit(q.get("limit"), default=100, maximum=1000)

    conditions = ["tenant_id = %s", "occurred_at >= %s", "occurred_at <= %s"]
    params: list[Any] = [ctx.tenant_id, start, end]

    if device_id:
        conditions.append("device_id = %s")
        params.append(device_id)
    if geofence_id:
        conditions.append("geofence_id = %s")
        params.append(geofence_id)
    if event_type:
        conditions.append("event_type = %s")
        params.append(event_type)

    cursor_raw = q.get("cursor")
    if cursor_raw:
        cursor_occurred_at_iso, cursor_id = _decode_cursor(cursor_raw)
        cursor_occurred_at = parse_time(cursor_occurred_at_iso, "cursor")
        # DESC pagination: strictly-older rows, or same instant with a smaller id.
        conditions.append("(occurred_at < %s OR (occurred_at = %s AND id < %s))")
        params.append(cursor_occurred_at)
        params.append(cursor_occurred_at)
        params.append(cursor_id)

    where_clause = " AND ".join(conditions)
    rows = query_all(
        f"""
        SELECT id, device_id, geofence_id, geofence_name, event_type,
               ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng,
               occurred_at, dwell_ms
          FROM device_events
         WHERE {where_clause}
         ORDER BY occurred_at DESC, id DESC
         LIMIT %s
        """,
        (*params, limit + 1),
    )

    has_more = len(rows) > limit
    rows = rows[:limit]

    events = [
        {
            "id": str(row["id"]),
            "device_id": row["device_id"],
            "geofence_id": str(row["geofence_id"]) if row["geofence_id"] else None,
            "geofence_name": row["geofence_name"],
            "event_type": row["event_type"],
            "lat": row["lat"],
            "lng": row["lng"],
            "occurred_at": to_iso(row["occurred_at"]),
            "dwell_ms": row["dwell_ms"],
        }
        for row in rows
    ]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(to_iso(last["occurred_at"]) or "", str(last["id"]))

    return json_response(200, {"count": len(events), "next_cursor": next_cursor, "events": events})


@api_handler(logger)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return router.dispatch(event)
