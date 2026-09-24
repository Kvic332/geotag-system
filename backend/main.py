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
import time
from typing import Any, Callable

import psycopg2
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from shared import config

# Import the three lambda_handlers (they wrap each router with @api_handler).
from position_tracker.handler import lambda_handler as _position
from geofence_manager.handler import lambda_handler as _geofence
from event_processor.handler import lambda_handler as _event
from residence_detector.handler import lambda_handler as _residence
from intelligence_engine.handler import lambda_handler as _intelligence

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


async def _dispatch(
    handler: Callable[[dict[str, Any], Any], dict[str, Any]],
    request: Request,
    path_params: dict[str, str] | None = None,
) -> JSONResponse:
    # Handlers do blocking psycopg2/redis I/O; running them on the event loop
    # lets one slow DB call stall every other request, /health included.
    event = await _build_event(request, path_params)
    return _to_response(await run_in_threadpool(handler, event, None))


# ---------------------------------------------------------------------------
# Health checks (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _probe_db() -> dict[str, Any]:
    started = time.monotonic()
    try:
        conn = psycopg2.connect(config.database_url(), connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    except psycopg2.Error as exc:
        detail = (str(exc).strip().splitlines() or [""])[0]
        return {"status": "error", "error": f"{type(exc).__name__}: {detail}",
                "ms": round((time.monotonic() - started) * 1000)}
    return {"status": "ok", "ms": round((time.monotonic() - started) * 1000)}


@app.get("/health/db")
async def health_db() -> JSONResponse:
    result = await run_in_threadpool(_probe_db)
    return JSONResponse(content=result, status_code=200 if result["status"] == "ok" else 503)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@app.post("/positions")
async def post_positions(request: Request) -> JSONResponse:
    return await _dispatch(_position, request)


@app.get("/positions/active")
async def get_active(request: Request) -> JSONResponse:
    return await _dispatch(_position, request)


@app.get("/positions/{device_id}/history")
async def get_history(device_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_position, request, {"device_id": device_id})


@app.get("/positions/{device_id}")
async def get_position(device_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_position, request, {"device_id": device_id})


# ---------------------------------------------------------------------------
# Geofences
# ---------------------------------------------------------------------------

@app.get("/geofences")
async def list_geofences(request: Request) -> JSONResponse:
    return await _dispatch(_geofence, request)


@app.post("/geofences")
async def create_geofence(request: Request) -> JSONResponse:
    return await _dispatch(_geofence, request)


@app.put("/geofences/{geofence_id}")
async def update_geofence(geofence_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_geofence, request, {"id": geofence_id})


@app.delete("/geofences/{geofence_id}")
async def delete_geofence(geofence_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_geofence, request, {"id": geofence_id})


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@app.get("/events")
async def list_events(request: Request) -> JSONResponse:
    return await _dispatch(_event, request)


# ---------------------------------------------------------------------------
# Residence detection
# ---------------------------------------------------------------------------

@app.get("/devices/{device_id}/residence")
async def get_residence(device_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_residence, request, {"device_id": device_id})


# ---------------------------------------------------------------------------
# Intelligence (AI analysis)
# ---------------------------------------------------------------------------

@app.get("/devices/{device_id}/intelligence")
async def get_intelligence(device_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_intelligence, request, {"device_id": device_id})


@app.post("/devices/{device_id}/intelligence/generate")
async def generate_intelligence(device_id: str, request: Request) -> JSONResponse:
    return await _dispatch(_intelligence, request, {"device_id": device_id})
