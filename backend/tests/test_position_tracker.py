"""position_tracker: batch ingest partial success, reads, Redis-down degradation.

CLAUDE.md coding standard: "Every Lambda must handle Redis failure gracefully
(log + continue; Postgres is source of truth)." The degradation test below
repoints Redis at an unreachable port and drives a REAL (unmocked) redis-py
connection attempt through `shared.redis_client`, exactly as SPEC-DECISIONS
asks ("Prefer the real Docker Postgres over mocks" — the same principle is
applied to Redis here: a real client hitting a real closed port, not a mock).
"""

from __future__ import annotations

import json
import os

import position_tracker.handler as pt
from shared import redis_client
from tests.conftest import make_event


def test_single_ping_sample_from_claude_md(tenant_a: str) -> None:
    resp = pt.lambda_handler(
        make_event(
            "POST",
            "/positions",
            tenant_id=tenant_a,
            body={
                "device_id": "device_abc123",
                "lat": 6.5244,
                "lng": 3.3792,
                "accuracy": 12.5,
                "speed": 0.0,
                "bearing": 180.0,
                "battery": 82,
                "timestamp": 1725868800,
            },
        ),
        None,
    )
    assert resp["statusCode"] == 201
    body = json.loads(resp["body"])
    assert body["device_id"] == "device_abc123"
    assert body["recorded_at"] == "2024-09-09T08:00:00Z"
    assert body["events"] == []


def test_batch_ingest_partial_success(tenant_a: str) -> None:
    resp = pt.lambda_handler(
        make_event(
            "POST",
            "/positions",
            tenant_id=tenant_a,
            body={
                "pings": [
                    {"device_id": "d1", "lat": 6.5, "lng": 3.3, "timestamp": 1_700_000_000},
                    {"device_id": "d2", "lat": 999, "lng": 3.3, "timestamp": 1_700_000_001},  # bad lat
                    {"device_id": "d3", "lat": 6.6, "lng": 3.4},  # missing timestamp -> defaults to now, still valid
                    {"lat": 6.6, "lng": 3.4, "timestamp": 1_700_000_002},  # missing device_id
                ]
            },
        ),
        None,
    )
    assert resp["statusCode"] == 201
    body = json.loads(resp["body"])
    assert body["accepted"] == 2
    assert len(body["rejected"]) == 2
    assert {r["index"] for r in body["rejected"]} == {1, 3}
    assert all("error" in r for r in body["rejected"])


def test_batch_processes_in_timestamp_order_not_array_order(tenant_a: str) -> None:
    # Two pings for the same device, given out of order; the later timestamp
    # should win as the "latest" position regardless of array position.
    resp = pt.lambda_handler(
        make_event(
            "POST",
            "/positions",
            tenant_id=tenant_a,
            body={
                "pings": [
                    {"device_id": "dev1", "lat": 2.0, "lng": 2.0, "timestamp": 1_700_000_100},
                    {"device_id": "dev1", "lat": 1.0, "lng": 1.0, "timestamp": 1_700_000_000},
                ]
            },
        ),
        None,
    )
    assert resp["statusCode"] == 201

    get_resp = pt.lambda_handler(make_event("GET", "/positions/dev1", tenant_id=tenant_a), None)
    body = json.loads(get_resp["body"])
    assert body["lat"] == 2.0 and body["lng"] == 2.0


def test_get_position_unknown_device_is_404(tenant_a: str) -> None:
    resp = pt.lambda_handler(make_event("GET", "/positions/nope", tenant_id=tenant_a), None)
    assert resp["statusCode"] == 404
    assert json.loads(resp["body"])["error"]["code"] == "not_found"


def test_history_orders_ascending_and_respects_range(tenant_a: str) -> None:
    for i, ts in enumerate([1_700_000_000, 1_700_000_100, 1_700_000_200]):
        pt.lambda_handler(
            make_event(
                "POST", "/positions", tenant_id=tenant_a,
                body={"device_id": "dev1", "lat": 1.0 + i, "lng": 2.0, "timestamp": ts},
            ),
            None,
        )

    resp = pt.lambda_handler(
        make_event(
            "GET", "/positions/dev1/history", tenant_id=tenant_a,
            query={"from": "1700000000", "to": "1700000300"},
        ),
        None,
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 3
    lats = [p["lat"] for p in body["positions"]]
    assert lats == [1.0, 2.0, 3.0]  # ascending by recorded_at


def test_redis_down_degrades_to_postgres(tenant_a: str) -> None:
    # 1. Write a position with Redis healthy.
    write_resp = pt.lambda_handler(
        make_event(
            "POST", "/positions", tenant_id=tenant_a,
            body={"device_id": "resilient1", "lat": 6.0, "lng": 3.0, "battery": 55, "timestamp": 1_700_000_500},
        ),
        None,
    )
    assert write_resp["statusCode"] == 201

    sanity = pt.lambda_handler(make_event("GET", "/positions/resilient1", tenant_id=tenant_a), None)
    assert json.loads(sanity["body"])["source"] == "redis"

    # 2. Repoint Redis at an unreachable port and force a real reconnect
    # attempt (not a mock) on the next operation.
    old_port = os.environ.get("REDIS_PORT")
    os.environ["REDIS_PORT"] = "1"  # nothing listens on TCP port 1
    redis_client.reset_client()
    try:
        # 3. A write while Redis is down must still succeed (Postgres only).
        write_while_down = pt.lambda_handler(
            make_event(
                "POST", "/positions", tenant_id=tenant_a,
                body={"device_id": "resilient1", "lat": 6.01, "lng": 3.01, "battery": 54, "timestamp": 1_700_000_600},
            ),
            None,
        )
        assert write_while_down["statusCode"] == 201

        # 4. A read while Redis is down must degrade to Postgres and say so.
        read_while_down = pt.lambda_handler(make_event("GET", "/positions/resilient1", tenant_id=tenant_a), None)
        assert read_while_down["statusCode"] == 200
        body = json.loads(read_while_down["body"])
        assert body["source"] == "postgres"
        assert body["lat"] == 6.01
        assert body["lng"] == 3.01
    finally:
        if old_port is None:
            os.environ.pop("REDIS_PORT", None)
        else:
            os.environ["REDIS_PORT"] = old_port
        redis_client.reset_client()
