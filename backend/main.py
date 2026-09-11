"""FastAPI entry point for Railway deployment.

Converts FastAPI HTTP requests into API Gateway proxy event dicts and passes
them through the existing Lambda handlers unchanged. No handler code is
modified — this is a pure routing adapter.

Auth: ENVIRONMENT=local  → HS256 JWT signed with DEV_JWT_SECRET
      ENVIRONMENT=production → Cognito RS256 (requires COGNITO_* vars)
Set ENVIRONMENT=local on Railway and supply a strong DEV_JWT_SECRET.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Import the three lambda_handlers (they wrap each router with @api_handler).
from position_tracker.handler import lambda_handler as _position
from geofence_manager.handler import lambda_handler as _geofence
from event_processor.handler import lambda_handler as _event
from residence_detector.handler import lambda_handler as _residence

app = FastAPI(title="GeoTag API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_event(request: Request, path_params: dict[str, str] | None = None) -> dict[str, Any]:
    body_bytes = await request.body()
    return {
        "httpMethod": request.method,
        "path": request.url.path,
        "headers": dict(request.headers),
        "queryStringParameters": dict(request.query_params) or None,
        "pathParameters": path_params or {},
        "body": body_bytes.decode("utf-8") if body_bytes else None,
        "isBase64Encoded": False,
    }


def _to_response(result: dict[str, Any]) -> JSONResponse:
    raw_body = result.get("body") or ""
    content = json.loads(raw_body) if raw_body else None
    # Strip hop-by-hop headers that FastAPI manages itself.
    headers = {
        k: v for k, v in (result.get("headers") or {}).items()
        if k.lower() not in ("content-type", "content-length")
    }
    return JSONResponse(content=content, status_code=result["statusCode"], headers=headers)


# ---------------------------------------------------------------------------
# Health check (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@app.post("/positions")
async def post_positions(request: Request) -> JSONResponse:
    return _to_response(_position(await _build_event(request), None))


@app.get("/positions/active")
async def get_active(request: Request) -> JSONResponse:
    return _to_response(_position(await _build_event(request), None))


@app.get("/positions/{device_id}/history")
async def get_history(device_id: str, request: Request) -> JSONResponse:
    return _to_response(_position(await _build_event(request, {"device_id": device_id}), None))


@app.get("/positions/{device_id}")
async def get_position(device_id: str, request: Request) -> JSONResponse:
    return _to_response(_position(await _build_event(request, {"device_id": device_id}), None))


# ---------------------------------------------------------------------------
# Geofences
# ---------------------------------------------------------------------------

@app.get("/geofences")
async def list_geofences(request: Request) -> JSONResponse:
    return _to_response(_geofence(await _build_event(request), None))


@app.post("/geofences")
async def create_geofence(request: Request) -> JSONResponse:
    return _to_response(_geofence(await _build_event(request), None))


@app.put("/geofences/{geofence_id}")
async def update_geofence(geofence_id: str, request: Request) -> JSONResponse:
    return _to_response(_geofence(await _build_event(request, {"id": geofence_id}), None))


@app.delete("/geofences/{geofence_id}")
async def delete_geofence(geofence_id: str, request: Request) -> JSONResponse:
    return _to_response(_geofence(await _build_event(request, {"id": geofence_id}), None))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@app.get("/events")
async def list_events(request: Request) -> JSONResponse:
    return _to_response(_event(await _build_event(request), None))


# ---------------------------------------------------------------------------
# Residence detection
# ---------------------------------------------------------------------------

@app.get("/devices/{device_id}/residence")
async def get_residence(device_id: str, request: Request) -> JSONResponse:
    return _to_response(_residence(await _build_event(request, {"device_id": device_id}), None))
