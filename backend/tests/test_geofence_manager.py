"""geofence_manager CRUD, D2 circle round-trip, and PUT partial updates."""

from __future__ import annotations

import json
import os

import geofence_manager.handler as gm
import position_tracker.handler as pt
from shared import redis_client
from tests.conftest import make_event


def _create(tenant_id: str, body: dict) -> dict:
    resp = gm.lambda_handler(make_event("POST", "/geofences", tenant_id=tenant_id, body=body), None)
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])


def test_circle_round_trips_as_circle(tenant_a: str) -> None:
    created = _create(
        tenant_a, {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250}
    )
    assert created["shape_type"] == "circle"
    assert created["center"] == [3.3792, 6.5244]
    assert created["radius_m"] == 250.0
    # The buffer ring still materialises for the map, closed, with >= 4 points.
    assert len(created["coordinates"]) >= 4
    assert created["coordinates"][0] == created["coordinates"][-1]

    listed = json.loads(gm.lambda_handler(make_event("GET", "/geofences", tenant_id=tenant_a), None)["body"])
    assert listed["count"] == 1
    assert listed["geofences"][0]["shape_type"] == "circle"
    assert listed["geofences"][0]["center"] == [3.3792, 6.5244]


def test_radius_out_of_bounds_is_validation_error(tenant_a: str) -> None:
    resp = gm.lambda_handler(
        make_event(
            "POST", "/geofences", tenant_id=tenant_a,
            body={"name": "Too Big", "type": "circle", "center": [3.0, 6.0], "radius_m": 200_000},
        ),
        None,
    )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "validation_error"


def test_put_partial_update_only_changes_supplied_fields(tenant_a: str) -> None:
    created = _create(
        tenant_a, {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250}
    )

    resp = gm.lambda_handler(
        make_event(
            "PUT", f"/geofences/{created['id']}", tenant_id=tenant_a,
            body={"dwell_threshold_seconds": 120},
        ),
        None,
    )
    assert resp["statusCode"] == 200
    updated = json.loads(resp["body"])
    assert updated["name"] == "Depot"  # unchanged
    assert updated["shape_type"] == "circle"  # unchanged
    assert updated["center"] == [3.3792, 6.5244]  # unchanged
    assert updated["dwell_threshold_seconds"] == 120  # changed


def test_put_can_switch_shape_from_circle_to_polygon(tenant_a: str) -> None:
    created = _create(
        tenant_a, {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250}
    )
    resp = gm.lambda_handler(
        make_event(
            "PUT", f"/geofences/{created['id']}", tenant_id=tenant_a,
            body={"type": "polygon", "coordinates": [[3.0, 6.0], [3.1, 6.0], [3.1, 6.1], [3.0, 6.1]]},
        ),
        None,
    )
    assert resp["statusCode"] == 200
    updated = json.loads(resp["body"])
    assert updated["shape_type"] == "polygon"
    assert updated["center"] is None
    assert updated["radius_m"] is None


def test_delete_returns_204_and_orphans_events_not_rows(tenant_a: str) -> None:
    created = _create(
        tenant_a, {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250}
    )
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "dev1", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_000},
        ),
        None,
    )

    delete_resp = gm.lambda_handler(make_event("DELETE", f"/geofences/{created['id']}", tenant_id=tenant_a), None)
    assert delete_resp["statusCode"] == 204
    assert delete_resp["body"] == ""

    again = gm.lambda_handler(make_event("DELETE", f"/geofences/{created['id']}", tenant_id=tenant_a), None)
    assert again["statusCode"] == 404


def test_device_count_reflects_current_occupancy(tenant_a: str) -> None:
    created = _create(
        tenant_a, {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250}
    )
    assert created["device_count"] == 0

    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "dev1", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_000},
        ),
        None,
    )
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "dev2", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_001},
        ),
        None,
    )

    listed = json.loads(gm.lambda_handler(make_event("GET", "/geofences", tenant_id=tenant_a), None)["body"])
    assert listed["geofences"][0]["device_count"] == 2


def test_device_count_batches_correctly_across_multiple_zones(tenant_a: str) -> None:
    """Regression test (SPEC-DECISIONS D9): `GET /geofences` batches all
    zones' device_count into one call (`zone_device_counts`, plural) instead
    of one lookup per zone — this specifically checks the batched results
    line back up with the right zone, not just that some count is nonzero."""
    zone_a = _create(tenant_a, {"name": "A", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250})
    zone_b = _create(tenant_a, {"name": "B", "type": "circle", "center": [10.0, 10.0], "radius_m": 250})
    zone_c = _create(tenant_a, {"name": "C", "type": "circle", "center": [-20.0, -20.0], "radius_m": 250})

    for device_id in ("dev1", "dev2", "dev3"):
        pt.lambda_handler(
            make_event(
                "POST", "/positions", tenant_id=tenant_a,
                body={"device_id": device_id, "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_000},
            ),
            None,
        )
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "dev4", "lat": 10.0, "lng": 10.0, "timestamp": 1_700_000_001},
        ),
        None,
    )
    # zone_c gets no devices at all — the count for an empty zone must be 0,
    # not missing/None (a zip-alignment bug would misassign counts here).

    listed = json.loads(gm.lambda_handler(make_event("GET", "/geofences", tenant_id=tenant_a), None)["body"])
    counts = {row["id"]: row["device_count"] for row in listed["geofences"]}
    assert counts[zone_a["id"]] == 3
    assert counts[zone_b["id"]] == 1
    assert counts[zone_c["id"]] == 0


def test_device_count_batches_correctly_with_redis_down(tenant_a: str) -> None:
    """Same as above but forces `zone_device_counts`' Postgres-fallback
    branch (one grouped query, not N) by taking Redis out during the read."""
    zone_a = _create(tenant_a, {"name": "A", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250})
    zone_b = _create(tenant_a, {"name": "B", "type": "circle", "center": [10.0, 10.0], "radius_m": 250})

    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "dev1", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_000},
        ),
        None,
    )
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "dev2", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_001},
        ),
        None,
    )

    old_port = os.environ.get("REDIS_PORT")
    os.environ["REDIS_PORT"] = "1"
    redis_client.reset_client()
    try:
        listed = json.loads(gm.lambda_handler(make_event("GET", "/geofences", tenant_id=tenant_a), None)["body"])
        counts = {row["id"]: row["device_count"] for row in listed["geofences"]}
        assert counts[zone_a["id"]] == 2
        assert counts[zone_b["id"]] == 0
    finally:
        if old_port is None:
            os.environ.pop("REDIS_PORT", None)
        else:
            os.environ["REDIS_PORT"] = old_port
        redis_client.reset_client()
