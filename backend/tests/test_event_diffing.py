"""Enter/exit/dwell diffing (D1, D3).

Timing is driven entirely by the `timestamp` field of each ping (not real
wall-clock sleeps) — `event_processor.diffing` computes dwell elapsed time
from `recorded_at`, so tests can simulate a 10-minute dwell in milliseconds.
"""

from __future__ import annotations

import json
import os

import geofence_manager.handler as gm
import position_tracker.handler as pt
from shared import redis_client
from tests.conftest import make_event

BASE_TS = 1_700_000_000  # arbitrary fixed epoch far from "now" so it never collides with real pings


def _create_geofence(tenant_id: str, **overrides) -> dict:
    body = {"name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250}
    body.update(overrides)
    resp = gm.lambda_handler(make_event("POST", "/geofences", tenant_id=tenant_id, body=body), None)
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])


def _ping(tenant_id: str, device_id: str, lat: float, lng: float, ts: int) -> dict:
    resp = pt.lambda_handler(
        make_event(
            "POST",
            "/positions",
            tenant_id=tenant_id,
            body={"device_id": device_id, "lat": lat, "lng": lng, "timestamp": ts},
        ),
        None,
    )
    assert resp["statusCode"] == 201, resp["body"]
    return json.loads(resp["body"])


INSIDE = (6.5244, 3.3792)
OUTSIDE = (1.0, 1.0)


def test_enter_then_exit(tenant_a: str) -> None:
    geofence = _create_geofence(tenant_a)

    entered = _ping(tenant_a, "dev1", *INSIDE, BASE_TS)
    assert [e["event_type"] for e in entered["events"]] == ["enter"]
    assert entered["events"][0]["geofence_id"] == geofence["id"]

    # Still inside: no further enter.
    still_in = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 5)
    assert still_in["events"] == []

    exited = _ping(tenant_a, "dev1", *OUTSIDE, BASE_TS + 10)
    assert [e["event_type"] for e in exited["events"]] == ["exit"]

    # Re-entering fires a fresh enter.
    re_entered = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 20)
    assert [e["event_type"] for e in re_entered["events"]] == ["enter"]


def test_dwell_fires_exactly_once_per_continuous_occupancy(tenant_a: str) -> None:
    _create_geofence(tenant_a, dwell_threshold_seconds=300)

    _ping(tenant_a, "dev1", *INSIDE, BASE_TS)  # enter

    before_threshold = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 100)
    assert before_threshold["events"] == []

    at_threshold = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 300)
    assert [e["event_type"] for e in at_threshold["events"]] == ["dwell"]

    # Further pings while still inside must NOT re-fire dwell.
    still_dwelling_1 = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 400)
    assert still_dwelling_1["events"] == []
    still_dwelling_2 = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 900)
    assert still_dwelling_2["events"] == []

    # Leaving and re-entering resets dwell state: it can fire again.
    _ping(tenant_a, "dev1", *OUTSIDE, BASE_TS + 1000)  # exit
    _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 1100)  # enter again

    second_dwell = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 1100 + 300)
    assert [e["event_type"] for e in second_dwell["events"]] == ["dwell"]


def test_dwell_disabled_when_threshold_is_null(tenant_a: str) -> None:
    _create_geofence(tenant_a, dwell_threshold_seconds=None)

    _ping(tenant_a, "dev1", *INSIDE, BASE_TS)
    long_stay = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 100_000)
    assert long_stay["events"] == []


def test_geofence_events_are_visible_via_get_events(tenant_a: str) -> None:
    import event_processor.handler as ep

    _create_geofence(tenant_a)
    _ping(tenant_a, "dev1", *INSIDE, BASE_TS)
    _ping(tenant_a, "dev1", *OUTSIDE, BASE_TS + 10)

    resp = ep.lambda_handler(make_event("GET", "/events", tenant_id=tenant_a), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    types = [e["event_type"] for e in body["events"]]
    # Newest first.
    assert types == ["exit", "enter"]


def test_deleting_a_geofence_the_device_is_inside_self_heals_on_next_ping(tenant_a: str) -> None:
    """Regression test (SPEC-DECISIONS D9).

    Deleting a geofence used to leave the device's own `device_zones` Redis
    set pointing at a now-nonexistent geofence_id. The device's next ping
    then tried to INSERT an `exit` event referencing that dead id, which
    violated the device_events FK, threw unhandled (500), and — because the
    Redis cleanup that would have fixed the stale state never ran — repeated
    on *every* subsequent ping forever. This must instead self-heal silently.
    """
    geofence = _create_geofence(tenant_a)
    _ping(tenant_a, "dev1", *INSIDE, BASE_TS)  # dev1 is now inside the zone

    delete_resp = gm.lambda_handler(
        make_event("DELETE", f"/geofences/{geofence['id']}", tenant_id=tenant_a), None
    )
    assert delete_resp["statusCode"] == 204

    # The device pings again from inside the (now-deleted) zone's footprint.
    # This must succeed, not 500 — and it must succeed *repeatedly*, since a
    # regression here specifically manifested as "fails forever".
    for _ in range(3):
        after_delete = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 10)
        assert after_delete["events"] == []


def test_dwell_does_not_refire_during_a_redis_outage(tenant_a: str) -> None:
    """Regression test (SPEC-DECISIONS D9).

    The "already dwelled" guard had no Postgres fallback, unlike the
    `entered_at` check next to it — so a degraded Redis made every ping
    re-emit a duplicate `dwell` event for the rest of the outage.
    """
    _create_geofence(tenant_a, dwell_threshold_seconds=1)

    _ping(tenant_a, "dev1", *INSIDE, BASE_TS)  # enter
    dwelled = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 5)  # past the 1s threshold
    assert [e["event_type"] for e in dwelled["events"]] == ["dwell"]

    old_port = os.environ.get("REDIS_PORT")
    os.environ["REDIS_PORT"] = "1"  # nothing listens on TCP port 1 — a real, not mocked, outage
    redis_client.reset_client()
    try:
        # Still dwelling, Redis down: must NOT re-fire dwell.
        while_down = _ping(tenant_a, "dev1", *INSIDE, BASE_TS + 10)
        assert while_down["events"] == [], (
            "dwell re-fired during a Redis outage — the Postgres fallback for "
            "the 'already dwelled' guard regressed"
        )
    finally:
        if old_port is None:
            os.environ.pop("REDIS_PORT", None)
        else:
            os.environ["REDIS_PORT"] = old_port
        redis_client.reset_client()


def test_dwell_threshold_over_thirty_days_is_rejected(tenant_a: str) -> None:
    """Regression test (SPEC-DECISIONS D9): the dwell-state Redis TTL scales
    with the threshold, so the threshold itself must stay bounded."""
    resp = gm.lambda_handler(
        make_event(
            "POST", "/geofences", tenant_id=tenant_a,
            body={
                "name": "TooLong", "type": "circle", "center": [3.3792, 6.5244],
                "radius_m": 250, "dwell_threshold_seconds": 60 * 60 * 24 * 31,
            },
        ),
        None,
    )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == "validation_error"
