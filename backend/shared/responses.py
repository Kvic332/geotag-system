"""API Gateway proxy responses.

`_error_envelope` is the ONLY place in the codebase that builds the
`{"error": {"code": ..., "message": ...}}` shape from API-CONTRACT.md, and
`api_handler` is the only caller. Handlers signal failure by raising an
`ApiError`; they never construct an error body themselves.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from aws_lambda_powertools import Logger

from shared.errors import ApiError, InternalError

JSON_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    # The dashboard is served from :5173 in local dev.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
}

LambdaEvent = dict[str, Any]
LambdaResponse = dict[str, Any]
HandlerFn = Callable[[LambdaEvent, Any], LambdaResponse]


def json_response(status_code: int, body: Any | None = None) -> LambdaResponse:
    """Build a successful API Gateway proxy response."""
    response: LambdaResponse = {
        "statusCode": status_code,
        "headers": dict(JSON_HEADERS),
        "isBase64Encoded": False,
    }
    response["body"] = "" if body is None else json.dumps(body, default=str)
    return response


def _error_envelope(code: str, message: str) -> dict[str, Any]:
    """THE single definition of the API-CONTRACT error envelope."""
    return {"error": {"code": code, "message": message}}


def api_handler(logger: Logger) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator turning raised `ApiError`s into contract-shaped responses.

    Any non-`ApiError` exception is logged with a stack trace and surfaced as a
    500 `internal_error` — the message is deliberately generic so that internal
    detail (SQL, connection strings) never leaks to a caller.
    """

    def decorate(func: HandlerFn) -> HandlerFn:
        def wrapper(event: LambdaEvent, context: Any) -> LambdaResponse:
            try:
                return func(event, context)
            except ApiError as exc:
                logger.warning(
                    "api_error",
                    extra={
                        "error_code": exc.code,
                        "status_code": exc.status_code,
                        "detail": exc.message,
                        "path": event.get("path"),
                        "method": event.get("httpMethod"),
                    },
                )
                return {
                    "statusCode": exc.status_code,
                    "headers": dict(JSON_HEADERS),
                    "isBase64Encoded": False,
                    "body": json.dumps(_error_envelope(exc.code, exc.message)),
                }
            except Exception:  # noqa: BLE001 - deliberate top-level boundary, re-logged
                logger.exception(
                    "unhandled_exception",
                    extra={"path": event.get("path"), "method": event.get("httpMethod")},
                )
                fallback = InternalError("internal server error")
                return {
                    "statusCode": fallback.status_code,
                    "headers": dict(JSON_HEADERS),
                    "isBase64Encoded": False,
                    "body": json.dumps(_error_envelope(fallback.code, fallback.message)),
                }

        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorate
