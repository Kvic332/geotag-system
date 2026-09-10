"""shared.auth / D6: missing token, invalid token, missing tenant claim."""

from __future__ import annotations

import json
import time

import jwt

import geofence_manager.handler as gm
from tests.conftest import DEV_JWT_SECRET, make_event


def test_missing_auth_header_is_401() -> None:
    resp = gm.lambda_handler(make_event("GET", "/geofences", tenant_id=None), None)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"]["code"] == "unauthorized"


def test_malformed_bearer_header_is_401() -> None:
    event = make_event("GET", "/geofences", tenant_id=None)
    event["headers"]["Authorization"] = "NotBearer abc"
    resp = gm.lambda_handler(event, None)
    assert resp["statusCode"] == 401


def test_token_without_tenant_claim_is_401() -> None:
    now = int(time.time())
    token = jwt.encode({"sub": "someone", "iat": now, "exp": now + 3600}, DEV_JWT_SECRET, algorithm="HS256")
    resp = gm.lambda_handler(make_event("GET", "/geofences", token=token), None)
    assert resp["statusCode"] == 401


def test_expired_token_is_401() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "someone", "custom:tenant_id": "acme", "iat": now - 7200, "exp": now - 3600},
        DEV_JWT_SECRET,
        algorithm="HS256",
    )
    resp = gm.lambda_handler(make_event("GET", "/geofences", token=token), None)
    assert resp["statusCode"] == 401


def test_wrong_secret_is_401() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "someone", "custom:tenant_id": "acme", "iat": now, "exp": now + 3600},
        "a-completely-different-secret-32-bytes-long",
        algorithm="HS256",
    )
    resp = gm.lambda_handler(make_event("GET", "/geofences", token=token), None)
    assert resp["statusCode"] == 401
