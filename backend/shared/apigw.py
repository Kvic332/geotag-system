"""Helpers for reading API Gateway proxy events, plus a tiny router.

The router exists so that every handler dispatches identically whether the event
came from real API Gateway, `sam local`, or `backend/local_server.py`.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import unquote

from shared.errors import NotFoundError, ValidationError

LambdaEvent = dict[str, Any]
RouteFn = Callable[..., Any]


def http_method(event: LambdaEvent) -> str:
    return str(event.get("httpMethod") or "GET").upper()


def request_path(event: LambdaEvent) -> str:
    path = str(event.get("path") or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def query_params(event: LambdaEvent) -> dict[str, str]:
    return {k: v for k, v in (event.get("queryStringParameters") or {}).items() if v is not None}


def json_body(event: LambdaEvent) -> Any:
    """Parse the request body as JSON. Empty body -> None."""
    raw = event.get("body")
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if event.get("isBase64Encoded"):
        import base64

        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("request body is not valid base64 UTF-8") from exc
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValidationError("request body is not valid JSON") from exc


def require_json_object(event: LambdaEvent) -> dict[str, Any]:
    body = json_body(event)
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    return body


class Router:
    """Maps (method, path template) pairs to functions.

    Templates use `{name}` segments, e.g. `/positions/{device_id}/history`.
    Literal segments always win over template segments, so `/positions/active`
    is never swallowed by `/positions/{device_id}`.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, tuple[str, ...], RouteFn]] = []

    def add(self, method: str, template: str, func: RouteFn) -> None:
        self._routes.append((method.upper(), tuple(_segments(template)), func))

    def route(self, method: str, template: str) -> Callable[[RouteFn], RouteFn]:
        def decorate(func: RouteFn) -> RouteFn:
            self.add(method, template, func)
            return func

        return decorate

    def templates(self) -> list[tuple[str, str]]:
        return [(method, "/" + "/".join(segs)) for method, segs, _ in self._routes]

    def dispatch(self, event: LambdaEvent, *args: Any) -> Any:
        method = http_method(event)
        path_segments = tuple(_segments(request_path(event)))

        best: tuple[int, RouteFn, dict[str, str]] | None = None
        path_exists = False

        for route_method, template_segments, func in self._routes:
            params = _match(template_segments, path_segments)
            if params is None:
                continue
            path_exists = True
            if route_method != method:
                continue
            # Fewer template variables == more literal == better match.
            specificity = -sum(1 for seg in template_segments if seg.startswith("{"))
            if best is None or specificity > best[0]:
                best = (specificity, func, params)

        if best is None:
            if path_exists:
                raise NotFoundError(f"{method} is not allowed on {request_path(event)}")
            raise NotFoundError(f"no route for {method} {request_path(event)}")

        _, func, params = best
        merged = dict(event.get("pathParameters") or {})
        merged.update(params)
        return func(event, merged, *args)


def _segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg != ""]


def _match(template: tuple[str, ...], actual: tuple[str, ...]) -> dict[str, str] | None:
    if len(template) != len(actual):
        return None
    params: dict[str, str] = {}
    for expected, got in zip(template, actual):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = unquote(got)
        elif expected != got:
            return None
    return params
