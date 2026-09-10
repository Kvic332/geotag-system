"""PostGISGeofenceEngine containment — circle and polygon zones (D1, D2)."""

from __future__ import annotations

import json

import geofence_manager.handler as gm
from shared.geofence_engine import PostGISGeofenceEngine
from tests.conftest import make_event


def _create(tenant_id: str, body: dict) -> dict:
    resp = gm.lambda_handler(make_event("POST", "/geofences", tenant_id=tenant_id, body=body), None)
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])


def test_circle_containment(tenant_a: str) -> None:
    geofence = _create(
        tenant_a,
        {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250},
    )
    engine = PostGISGeofenceEngine()

    # Center point: inside.
    assert geofence["id"] in engine.zones_containing(tenant_a, 6.5244, 3.3792)

    # ~2.5km away: outside a 250m circle.
    assert geofence["id"] not in engine.zones_containing(tenant_a, 6.55, 3.40)


def test_polygon_containment(tenant_a: str) -> None:
    geofence = _create(
        tenant_a,
        {
            "name": "Warehouse",
            "type": "polygon",
            "coordinates": [[3.0, 6.0], [3.1, 6.0], [3.1, 6.1], [3.0, 6.1], [3.0, 6.0]],
        },
    )
    engine = PostGISGeofenceEngine()

    # Centroid of the square: inside.
    assert geofence["id"] in engine.zones_containing(tenant_a, 6.05, 3.05)

    # Well outside the square.
    assert geofence["id"] not in engine.zones_containing(tenant_a, 10.0, 10.0)


def test_polygon_unclosed_ring_is_closed_not_rejected(tenant_a: str) -> None:
    # API-CONTRACT: "the API closes an unclosed ring rather than rejecting it".
    geofence = _create(
        tenant_a,
        {
            "name": "Unclosed",
            "type": "polygon",
            "coordinates": [[3.0, 6.0], [3.1, 6.0], [3.1, 6.1], [3.0, 6.1]],
        },
    )
    assert geofence["coordinates"][0] == geofence["coordinates"][-1]


def test_zones_containing_is_tenant_scoped(tenant_a: str, tenant_b: str) -> None:
    geofence = _create(
        tenant_a,
        {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250},
    )
    engine = PostGISGeofenceEngine()

    assert geofence["id"] in engine.zones_containing(tenant_a, 6.5244, 3.3792)
    # Same point, different tenant: must not see tenant_a's zone.
    assert engine.zones_containing(tenant_b, 6.5244, 3.3792) == []
