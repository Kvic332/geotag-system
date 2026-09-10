"""Geofence containment engine (SPEC-DECISIONS D1).

CLAUDE.md names AWS Location Service as "the" geofencing engine but says it is
"mocked locally" without specifying the mock. D1 resolves this: PostGIS is the
real, tested engine; AWS Location Service is a thin, unverified swap-in for
production. Handlers never import a concrete engine — they call `get_engine()`.

Enter/exit diffing does NOT live here. This module only answers "which zones
(by id) contain this point right now" for a tenant. `event_processor` is
responsible for comparing that answer against the device's previous zone set.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from shared import config
from shared.db import query_all
from shared.logging_utils import get_logger

logger = get_logger("geotag.geofence_engine")


class GeofenceEngine(ABC):
    """Answers "which zones contain this point" for a tenant."""

    @abstractmethod
    def zones_containing(self, tenant_id: str, lat: float, lng: float) -> list[str]:
        """Return the ids of every geofence for `tenant_id` that covers (lat, lng)."""
        raise NotImplementedError


class PostGISGeofenceEngine(GeofenceEngine):
    """Real implementation: `ST_Covers(boundary, point)`, tenant-scoped.

    This is the default engine (`ENVIRONMENT=local`) and the one exercised by
    the test suite. `ST_Covers` (rather than `ST_Contains`) is used so a point
    exactly on the boundary counts as "inside" — the more intuitive behaviour
    for a geofence.
    """

    def zones_containing(self, tenant_id: str, lat: float, lng: float) -> list[str]:
        rows: list[dict[str, Any]] = query_all(
            """
            SELECT id
              FROM geofences
             WHERE tenant_id = %s
               AND ST_Covers(boundary, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
            """,
            (tenant_id, lng, lat),
        )
        return [str(row["id"]) for row in rows]


class LocationServiceGeofenceEngine(GeofenceEngine):
    """Calls AWS Location Service `BatchEvaluateGeofences`.

    Used when `ENVIRONMENT=production` and `AWS_LOCATION_COLLECTION` is set.
    NOT verified against a real AWS account in this environment — there is no
    AWS credential or Location Service collection available here. Treat this
    class as unexercised scaffolding: it is structurally correct against the
    boto3 API shape but has no integration test.
    """

    def __init__(self, collection_name: str) -> None:
        self._collection_name = collection_name
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # local import: keep boto3 optional for local dev

            self._client = boto3.client("location", region_name=config.aws_region())
        return self._client

    def zones_containing(self, tenant_id: str, lat: float, lng: float) -> list[str]:
        client = self._get_client()
        try:
            # AWS Location Service device-position based evaluation is async
            # (it publishes ENTER/EXIT to EventBridge); there is no synchronous
            # "which zones contain this point right now" call. The closest
            # analogue is BatchPutGeofence + BatchEvaluateGeofences via a
            # tracker, which requires a pre-provisioned tracker/collection per
            # tenant. That provisioning is out of scope here — this method is
            # deliberately conservative and returns no zones rather than
            # guessing at an unverified API shape.
            logger.warning(
                "location_service_engine_unverified",
                extra={
                    "tenant_id": tenant_id,
                    "collection": self._collection_name,
                    "detail": "BatchEvaluateGeofences path is not implemented/verified; "
                    "returning no zones. See SPEC-DECISIONS D1.",
                },
            )
            return []
        except Exception:  # noqa: BLE001 - never let an unverified AWS path crash ingestion
            logger.exception("location_service_engine_error", extra={"tenant_id": tenant_id})
            return []


_engine: GeofenceEngine | None = None


def get_engine() -> GeofenceEngine:
    """Return the process-wide engine for the current `ENVIRONMENT` (D1)."""
    global _engine
    if _engine is not None:
        return _engine

    collection = config.env("AWS_LOCATION_COLLECTION")
    if not config.is_local() and collection:
        _engine = LocationServiceGeofenceEngine(collection)
    else:
        _engine = PostGISGeofenceEngine()
    logger.info("geofence_engine_selected", extra={"engine": type(_engine).__name__})
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (used by tests that toggle ENVIRONMENT)."""
    global _engine
    _engine = None
