"""Local dev API server (SPEC-DECISIONS toolchain constraint: no AWS SAM CLI
on this machine).

A FastAPI/uvicorn adapter that builds API-Gateway-proxy events from real HTTP
requests and dispatches them into the SAME `lambda_handler` functions used by
`infra/template.yaml`. Zero handler logic lives here — this file only
translates between ASGI and the API Gateway proxy event/response shapes.

Run with:

    uvicorn local_server:app --port 3000 --app-dir backend

or simply `python backend/local_server.py`.
"""

from __future__ import annotations

import json
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response

from event_processor import handler as event_processor_handler
from geofence_manager import handler as geofence_manager_handler
from position_tracker import handler as position_tracker_handler
from shared.logging_utils import get_logger

logger = get_logger("geotag.local_server")

app = FastAPI(title="geotag-system local API", docs_url=None, redoc_url=None)

# First path segment -> the Lambda that owns it. Matches infra/template.yaml.
_ROUTES: dict[str, Any] = {
    "positions": position_tracker_handler.lambda_handler,
    "geofences": geofence_manager_handler.lambda_handler,
    "events": event_processor_handler.lambda_handler,
}


def _select_handler(path: str) -> Any | None:
    first_segment = path.strip("/").split("/", 1)[0]
    return _ROUTES.get(first_segment)


async def _build_event(request: Request) -> dict[str, Any]:
    body_bytes = await request.body()
    query_params: dict[str, str] = {k: v for k, v in request.query_params.items()}
    headers: dict[str, str] = dict(request.headers)

    return {
        "httpMethod": request.method,
        "path": request.url.path,
        "headers": headers,
        "queryStringParameters": query_params or None,
        "pathParameters": None,
        "body": body_bytes.decode("utf-8") if body_bytes else None,
        "isBase64Encoded": False,
    }


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def dispatch(full_path: str, request: Request) -> Response:
    if request.method == "OPTIONS":
        # CORS preflight for the dashboard (localhost:5173). Handlers never
        # see OPTIONS — API Gateway would normally answer this itself too.
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Content-Type,Authorization",
                "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
            },
        )

    handler = _select_handler(full_path)
    if handler is None:
        return Response(
            status_code=404,
            media_type="application/json",
            content=json.dumps({"error": {"code": "not_found", "message": f"no route for {request.url.path}"}}),
        )

    event = await _build_event(request)
    result = handler(event, None)

    return Response(
        status_code=result["statusCode"],
        headers={k: v for k, v in result.get("headers", {}).items()},
        content=result.get("body") or "",
        media_type="application/json",
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
