"""Pytest fixtures shared by the whole suite.

Tests run against the real Docker Postgres+PostGIS and Redis from
`infra/docker-compose.yml` (per SPEC-DECISIONS: "Prefer the real Docker
Postgres over mocks") — nothing here is a DB/Redis mock. Every test gets a
clean slate: `truncate_tables` wipes the three tenant tables and `flush_redis`
flushes the Redis DB before each test.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Environment must be set before any `shared.*` module is imported, since the
# db/redis singletons read config.* lazily on first use but memoise the
# client/pool afterwards.
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("DEV_JWT_SECRET", "test-suite-secret-please-do-not-reuse-32")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:dev@localhost:5432/geotag")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("SNS_TOPIC_ARN", "")
os.environ.setdefault("AWS_REGION", "eu-west-1")

from shared import db, geofence_engine, redis_client  # noqa: E402

DEV_JWT_SECRET = os.environ["DEV_JWT_SECRET"]


def mint_token(tenant_id: str, subject: str | None = None, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    claims = {
        "sub": subject or str(uuid.uuid4()),
        "custom:tenant_id": tenant_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, DEV_JWT_SECRET, algorithm="HS256")


def make_event(
    method: str,
    path: str,
    *,
    tenant_id: str | None = "tenant-a",
    body: Any = None,
    query: dict[str, str] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Build an API-Gateway-proxy event, matching backend/local_server.py's shape."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    elif tenant_id is not None:
        headers["Authorization"] = f"Bearer {mint_token(tenant_id)}"

    event: dict[str, Any] = {
        "httpMethod": method,
        "path": path,
        "headers": headers,
        "queryStringParameters": query,
        "pathParameters": None,
        "isBase64Encoded": False,
    }
    if body is not None:
        import json

        event["body"] = json.dumps(body)
    else:
        event["body"] = None
    return event


@pytest.fixture(scope="session", autouse=True)
def _verify_infra() -> None:
    """Fail fast with a clear message if Docker Postgres/Redis aren't up."""
    try:
        db.query_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001 - fixture-time diagnostic, re-raised as skip
        pytest.skip(f"Postgres not reachable at {os.environ['DATABASE_URL']}: {exc}")
    if not redis_client.ping():
        pytest.skip(f"Redis not reachable at {os.environ['REDIS_HOST']}:{os.environ['REDIS_PORT']}")


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Wipe tenant tables and Redis before every test for isolation."""
    db.execute("TRUNCATE positions, device_events, geofences")
    redis_client.redis_op("flushdb", lambda c: c.flushdb(), None)
    redis_client.reset_client()
    geofence_engine.reset_engine()
    yield


@pytest.fixture
def tenant_a() -> str:
    return "tenant-a"


@pytest.fixture
def tenant_b() -> str:
    return "tenant-b"
