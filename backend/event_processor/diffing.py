"""Enter/exit/dwell diffing (SPEC-DECISIONS D1, D3).

D1: "Enter/exit diffing (comparing the previous zone set to the current one)
lives in event_processor, NOT in the engine. The engine only answers 'which
zones contain this point'."

This module owns that comparison plus the dwell bookkeeping from D3. It is
imported directly by `position_tracker.handler` (to build the `events` array
in the POST /positions response — SPEC-DECISIONS explicitly requires that
response to carry events synchronously, and locally there is no AWS Location
Service to trigger this asynchronously) and by `geofence_manager.handler`
(for live `device_count`). See infra/template.yaml and the final report for
the packaging note this implies (all three Lambdas share one CodeUri so this
cross-module import is deployable, not just a local convenience).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared import cache_keys, config
from shared.db import execute, query_all, query_one
from shared.geofence_engine import get_engine
from shared.logging_utils import get_logger
from shared.redis_client import redis_op, redis_pipeline
from shared.timeutil import from_unix, to_iso, to_unix

from event_processor.notifications import publish_event

logger = get_logger("geotag.event_processor.diffing")

_MISSING = object()  # sentinel: Redis unavailable (distinct from "empty set")


@dataclass(frozen=True)
class EventOut:
    device_id: str
    event_type: str  # 'enter' | 'exit' | 'dwell'
    geofence_id: str
    geofence_name: str
    occurred_at: datetime
    dwell_ms: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "event_type": self.event_type,
            "geofence_id": self.geofence_id,
            "geofence_name": self.geofence_name,
            "occurred_at": to_iso(self.occurred_at),
            "dwell_ms": self.dwell_ms,
        }


# ---------------------------------------------------------------------------
# Postgres fallback reconstruction (used whenever Redis is degraded — every
# Lambda must survive Redis being down, CLAUDE.md coding standards / D6 test
# requirement).
# ---------------------------------------------------------------------------


def _zones_from_postgres(tenant_id: str, device_id: str) -> dict[str, datetime]:
    """Reconstruct {geofence_id: entered_at} for zones the device is currently
    inside, from the device_events log alone (no Redis)."""
    rows = query_all(
        """
        SELECT DISTINCT ON (geofence_id) geofence_id, event_type, occurred_at
          FROM device_events
         WHERE tenant_id = %s AND device_id = %s AND geofence_id IS NOT NULL
           AND event_type IN ('enter', 'exit')
         ORDER BY geofence_id, occurred_at DESC, id DESC
        """,
        (tenant_id, device_id),
    )
    return {
        str(row["geofence_id"]): row["occurred_at"]
        for row in rows
        if row["event_type"] == "enter"
    }


def current_zone_ids(tenant_id: str, device_id: str) -> list[str]:
    """Zones the device is currently inside. Redis first, Postgres fallback."""
    key = cache_keys.device_zones(tenant_id, device_id)
    members = redis_op("smembers_device_zones", lambda c: c.smembers(key), _MISSING)
    if members is not _MISSING:
        return sorted(members)  # type: ignore[arg-type]
    return sorted(_zones_from_postgres(tenant_id, device_id).keys())


def zone_device_count(tenant_id: str, geofence_id: str) -> int:
    """Current occupancy of a zone. Redis first, Postgres fallback."""
    key = cache_keys.zone_members(tenant_id, geofence_id)
    count = redis_op("scard_zone_members", lambda c: c.scard(key), _MISSING)
    if count is not _MISSING:
        return int(count)  # type: ignore[arg-type]

    rows = query_all(
        """
        SELECT DISTINCT ON (device_id) device_id, event_type
          FROM device_events
         WHERE tenant_id = %s AND geofence_id = %s
           AND event_type IN ('enter', 'exit')
         ORDER BY device_id, occurred_at DESC, id DESC
        """,
        (tenant_id, geofence_id),
    )
    return sum(1 for row in rows if row["event_type"] == "enter")


def zone_device_counts(tenant_id: str, geofence_ids: list[str]) -> dict[str, int]:
    """Batched form of `zone_device_count`.

    `GET /geofences` used to call `zone_device_count` once per row — N
    sequential Redis (or Postgres-fallback) round trips per call, and the
    dashboard polls that endpoint every 20s, making it a steady recurring
    N+1 cost. This does it in one Redis pipeline, with one grouped Postgres
    query (instead of N) as the degraded-Redis fallback.
    """
    if not geofence_ids:
        return {}

    keys = [cache_keys.zone_members(tenant_id, gid) for gid in geofence_ids]

    def _scard_all(client: Any) -> list[int]:
        pipe = client.pipeline(transaction=False)
        for key in keys:
            pipe.scard(key)
        return pipe.execute()

    raw = redis_op("scard_zone_members_batch", _scard_all, _MISSING)
    if raw is not _MISSING:
        return {gid: int(count) for gid, count in zip(geofence_ids, raw)}  # type: ignore[arg-type]

    rows = query_all(
        """
        SELECT DISTINCT ON (geofence_id, device_id) geofence_id, device_id, event_type
          FROM device_events
         WHERE tenant_id = %s AND geofence_id = ANY(%s::uuid[])
           AND event_type IN ('enter', 'exit')
         ORDER BY geofence_id, device_id, occurred_at DESC, id DESC
        """,
        (tenant_id, geofence_ids),
    )
    counts = {gid: 0 for gid in geofence_ids}
    for row in rows:
        if row["event_type"] == "enter":
            gid = str(row["geofence_id"])
            counts[gid] = counts.get(gid, 0) + 1
    return counts


def _dwell_already_fired(tenant_id: str, device_id: str, geofence_id: str, since_unix: int) -> bool:
    """Postgres fallback for "has dwell already fired for this occupancy".

    Used only when Redis is degraded — without this, the `dwelled` guard in
    `process_ping_events` had no fallback at all and re-fired a `dwell` event
    on every ping for the duration of a Redis outage (SPEC-DECISIONS D3 fix).
    """
    row = query_one(
        """
        SELECT 1 FROM device_events
         WHERE tenant_id = %s AND device_id = %s AND geofence_id = %s
           AND event_type = 'dwell' AND occurred_at >= %s
         LIMIT 1
        """,
        (tenant_id, device_id, geofence_id, from_unix(since_unix)),
    )
    return row is not None


def _dwell_state_ttl(threshold_seconds: int | None) -> int:
    """TTL for the `entered_at`/`dwelled` Redis keys.

    Must outlive any occupancy the threshold can plausibly describe — a fixed
    24h TTL silently broke dwell for any zone configured with a threshold
    above 24h (SPEC-DECISIONS D3 fix): the key expired mid-occupancy, came
    back as a plain Redis miss (indistinguishable from "never entered"), and
    dwell for that occupancy never fired again.
    """
    if threshold_seconds is None:
        return config.DWELL_STATE_TTL_SECONDS
    return max(config.DWELL_STATE_TTL_SECONDS, threshold_seconds + config.DWELL_STATE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Core diffing
# ---------------------------------------------------------------------------


def _geofence_meta(tenant_id: str, geofence_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not geofence_ids:
        return {}
    rows = query_all(
        "SELECT id, name, dwell_threshold_seconds FROM geofences WHERE tenant_id = %s AND id = ANY(%s::uuid[])",
        (tenant_id, geofence_ids),
    )
    return {str(row["id"]): row for row in rows}


def _geofence_meta_cached(
    tenant_id: str, geofence_ids: list[str], cache: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """`_geofence_meta`, reusing a caller-supplied cache across many pings.

    `position_tracker`'s batch path (SPEC-DECISIONS D4, up to 500 pings/request)
    called `_geofence_meta` once per ping even though the same handful of zones
    repeat across a whole batch — a real, measured N+1. `cache=None` (the
    default, used by every existing caller: single pings, geofence_manager's
    zone_device_count) preserves the exact old per-call behavior.
    """
    if cache is None:
        return _geofence_meta(tenant_id, geofence_ids)
    missing = [gid for gid in geofence_ids if gid not in cache]
    if missing:
        cache.update(_geofence_meta(tenant_id, missing))
    return {gid: cache[gid] for gid in geofence_ids if gid in cache}


def _insert_event(
    tenant_id: str,
    device_id: str,
    geofence_id: str,
    geofence_name: str,
    event_type: str,
    lat: float,
    lng: float,
    occurred_at: datetime,
    dwell_ms: int | None,
) -> None:
    execute(
        """
        INSERT INTO device_events
            (tenant_id, device_id, geofence_id, geofence_name, event_type, location, dwell_ms, occurred_at)
        VALUES
            (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s)
        """,
        (tenant_id, device_id, geofence_id, geofence_name, event_type, lng, lat, dwell_ms, occurred_at),
    )


def process_ping_events(
    tenant_id: str,
    device_id: str,
    lat: float,
    lng: float,
    recorded_at: datetime,
    meta_cache: dict[str, dict[str, Any]] | None = None,
) -> list[EventOut]:
    """Diff the device's zone membership against this ping and emit events.

    Writes every emitted event to Postgres (source of truth), updates the
    Redis membership caches (best-effort — a Redis outage degrades but never
    fails ingestion), publishes each event via `notifications.publish_event`,
    and returns the events for the caller (position_tracker) to render.
    """
    engine = get_engine()
    current = set(engine.zones_containing(tenant_id, lat, lng))

    zones_key = cache_keys.device_zones(tenant_id, device_id)
    previous_raw = redis_op("smembers_device_zones", lambda c: c.smembers(zones_key), _MISSING)
    redis_degraded = previous_raw is _MISSING
    previous: set[str] = set(previous_raw) if not redis_degraded else set(_zones_from_postgres(tenant_id, device_id).keys())  # type: ignore[arg-type]

    entered = current - previous
    exited = previous - current
    still_inside = current & previous

    meta = _geofence_meta_cached(tenant_id, sorted(current | previous), meta_cache)
    events: list[EventOut] = []
    now_unix = to_unix(recorded_at)

    for geofence_id in sorted(entered):
        info = meta.get(geofence_id)
        name = str(info["name"]) if info else ""
        _insert_event(tenant_id, device_id, geofence_id, name, "enter", lat, lng, recorded_at, None)

        entered_key = cache_keys.entered_at(tenant_id, geofence_id, device_id)
        dwelled_key = cache_keys.dwelled(tenant_id, geofence_id, device_id)
        members_key = cache_keys.zone_members(tenant_id, geofence_id)
        ttl = _dwell_state_ttl(info.get("dwell_threshold_seconds") if info else None)

        def _apply_enter(
            pipe: Any,
            members_key: str = members_key,
            entered_key: str = entered_key,
            dwelled_key: str = dwelled_key,
            ttl: int = ttl,
        ) -> None:
            pipe.sadd(members_key, device_id)
            pipe.sadd(zones_key, geofence_id)
            pipe.set(entered_key, now_unix, ex=ttl)
            pipe.delete(dwelled_key)

        redis_pipeline("enter_zone", _apply_enter)

        event = EventOut(device_id, "enter", geofence_id, name, recorded_at, None)
        events.append(event)
        publish_event(event.as_dict())

    for geofence_id in sorted(exited):
        info = meta.get(geofence_id)

        entered_key = cache_keys.entered_at(tenant_id, geofence_id, device_id)
        dwelled_key = cache_keys.dwelled(tenant_id, geofence_id, device_id)
        members_key = cache_keys.zone_members(tenant_id, geofence_id)

        def _apply_exit(pipe: Any, members_key: str = members_key, entered_key: str = entered_key, dwelled_key: str = dwelled_key) -> None:
            pipe.srem(members_key, device_id)
            pipe.srem(zones_key, geofence_id)
            pipe.delete(entered_key)
            pipe.delete(dwelled_key)

        # Clear the Redis membership before anything that can fail below —
        # this is what self-heals a device whose `device_zones` set still
        # references a geofence that has since been deleted (see next block).
        redis_pipeline("exit_zone", _apply_exit)

        if info is None:
            # The geofence no longer exists (deleted between this device's
            # last ping and this one). `device_events.geofence_id` has a FK
            # to `geofences`, so inserting an exit event for a dead id would
            # raise, and — because that used to happen *before* the Redis
            # cleanup above ran — the same failure repeated on every
            # subsequent ping forever (SPEC-DECISIONS D6/geofence-delete
            # fix). The membership is already cleared; there is nothing left
            # to reference, so skip the DB row and the callback.
            logger.warning(
                "exit_from_deleted_geofence",
                extra={"tenant_id": tenant_id, "device_id": device_id, "geofence_id": geofence_id},
            )
            continue

        name = str(info["name"])
        _insert_event(tenant_id, device_id, geofence_id, name, "exit", lat, lng, recorded_at, None)

        event = EventOut(device_id, "exit", geofence_id, name, recorded_at, None)
        events.append(event)
        publish_event(event.as_dict())

    for geofence_id in sorted(still_inside):
        info = meta.get(geofence_id)
        if info is None:
            continue
        threshold = info.get("dwell_threshold_seconds")
        if threshold is None:
            continue
        name = str(info["name"])

        entered_key = cache_keys.entered_at(tenant_id, geofence_id, device_id)
        dwelled_key = cache_keys.dwelled(tenant_id, geofence_id, device_id)

        entered_at_raw = redis_op("get_entered_at", lambda c, k=entered_key: c.get(k), _MISSING)
        if entered_at_raw is _MISSING:
            fallback = _zones_from_postgres(tenant_id, device_id).get(geofence_id)
            entered_at_unix = to_unix(fallback) if fallback is not None else None
        elif entered_at_raw is None:
            entered_at_unix = None
        else:
            entered_at_unix = int(entered_at_raw)

        if entered_at_unix is None:
            continue

        already_dwelled_raw = redis_op("get_dwelled", lambda c, k=dwelled_key: c.get(k), _MISSING)
        if already_dwelled_raw is _MISSING:
            # Redis is degraded — fall back to Postgres instead of assuming
            # "not yet dwelled". Without this, dwell re-fired on every ping
            # for the whole outage (SPEC-DECISIONS D3 fix).
            already_dwelled = _dwell_already_fired(tenant_id, device_id, geofence_id, entered_at_unix)
        else:
            already_dwelled = already_dwelled_raw is not None
        if already_dwelled:
            continue

        elapsed = now_unix - entered_at_unix
        if elapsed < threshold:
            continue

        dwell_ms = elapsed * 1000
        _insert_event(tenant_id, device_id, geofence_id, name, "dwell", lat, lng, recorded_at, dwell_ms)
        redis_op("set_dwelled", lambda c, k=dwelled_key: c.set(k, "1", ex=_dwell_state_ttl(threshold)), None)

        event = EventOut(device_id, "dwell", geofence_id, name, recorded_at, dwell_ms)
        events.append(event)
        publish_event(event.as_dict())

    if redis_degraded:
        logger.warning(
            "event_diffing_redis_degraded",
            extra={"tenant_id": tenant_id, "device_id": device_id, "detail": "used Postgres fallback for previous zones"},
        )

    return events
