"""Tenant isolation (SPEC-DECISIONS D6): tenant A must never see tenant B's
positions, geofences, or events. This is the highest-severity gap D6 names,
so it gets a real, direct test — not just a code-review claim.
"""

from __future__ import annotations

import json
import time

import event_processor.handler as ep
import geofence_manager.handler as gm
import position_tracker.handler as pt
from tests.conftest import make_event


def test_positions_are_tenant_scoped(tenant_a: str, tenant_b: str) -> None:
    # GET /positions/active only shows the last hour (D5), so this test uses
    # real "now"-based timestamps rather than a fixed synthetic epoch.
    now = int(time.time())

    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "shared-device-id", "lat": 6.5, "lng": 3.3, "timestamp": now},
        ),
        None,
    )

    # Same device_id, tenant B has never pinged it: must be a 404, not tenant A's data.
    resp = pt.lambda_handler(make_event("GET", "/positions/shared-device-id", tenant_id=tenant_b), None)
    assert resp["statusCode"] == 404

    # tenant B's own ping for the same device_id must not see tenant A's history.
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_b,
            body={"device_id": "shared-device-id", "lat": 1.0, "lng": 1.0, "timestamp": now + 1},
        ),
        None,
    )
    history = json.loads(
        pt.lambda_handler(
            make_event(
                "GET", "/positions/shared-device-id/history", tenant_id=tenant_b,
                query={"from": str(now - 10), "to": str(now + 10)},
            ),
            None,
        )["body"]
    )
    assert history["count"] == 1
    assert history["positions"][0]["lat"] == 1.0

    active_a = json.loads(pt.lambda_handler(make_event("GET", "/positions/active", tenant_id=tenant_a), None)["body"])
    active_b = json.loads(pt.lambda_handler(make_event("GET", "/positions/active", tenant_id=tenant_b), None)["body"])
    assert active_a["count"] == 1 and active_a["devices"][0]["lat"] == 6.5
    assert active_b["count"] == 1 and active_b["devices"][0]["lat"] == 1.0


def test_geofences_are_tenant_scoped(tenant_a: str, tenant_b: str) -> None:
    created = json.loads(
        gm.lambda_handler(
            make_event(
                "POST", "/geofences", tenant_id=tenant_a,
                body={"name": "A-only", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250},
            ),
            None,
        )["body"]
    )

    listed_b = json.loads(gm.lambda_handler(make_event("GET", "/geofences", tenant_id=tenant_b), None)["body"])
    assert listed_b["count"] == 0

    get_by_id_b = gm.lambda_handler(make_event("PUT", f"/geofences/{created['id']}", tenant_id=tenant_b, body={"name": "hijacked"}), None)
    assert get_by_id_b["statusCode"] == 404

    delete_by_b = gm.lambda_handler(make_event("DELETE", f"/geofences/{created['id']}", tenant_id=tenant_b), None)
    assert delete_by_b["statusCode"] == 404

    # tenant A can still see and manage its own zone untouched.
    listed_a = json.loads(gm.lambda_handler(make_event("GET", "/geofences", tenant_id=tenant_a), None)["body"])
    assert listed_a["count"] == 1
    assert listed_a["geofences"][0]["name"] == "A-only"


def test_events_are_tenant_scoped(tenant_a: str, tenant_b: str) -> None:
    gm.lambda_handler(
        make_event(
            "POST", "/geofences", tenant_id=tenant_a,
            body={"name": "A-zone", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250},
        ),
        None,
    )
    gm.lambda_handler(
        make_event(
            "POST", "/geofences", tenant_id=tenant_b,
            body={"name": "B-zone", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250},
        ),
        None,
    )

    # Both tenants have a device passing through the exact same coordinates.
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "devA", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_000},
        ),
        None,
    )
    pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_b,
            body={"device_id": "devB", "lat": 6.5244, "lng": 3.3792, "timestamp": 1_700_000_000},
        ),
        None,
    )

    events_a = json.loads(ep.lambda_handler(make_event("GET", "/events", tenant_id=tenant_a), None)["body"])
    events_b = json.loads(ep.lambda_handler(make_event("GET", "/events", tenant_id=tenant_b), None)["body"])

    assert events_a["count"] == 1 and events_a["events"][0]["device_id"] == "devA"
    assert events_a["events"][0]["geofence_name"] == "A-zone"
    assert events_b["count"] == 1 and events_b["events"][0]["device_id"] == "devB"
    assert events_b["events"][0]["geofence_name"] == "B-zone"

    # Cross-tenant filter probing must not leak the other tenant's rows either.
    filtered = json.loads(
        ep.lambda_handler(make_event("GET", "/events", tenant_id=tenant_a, query={"device_id": "devB"}), None)["body"]
    )
    assert filtered["count"] == 0
