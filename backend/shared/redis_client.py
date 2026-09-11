"""Redis connection singleton with graceful degradation.

CLAUDE.md: "Every Lambda must handle Redis failure gracefully (log + continue;
Postgres is source of truth)."

Nothing in this module ever raises on a Redis failure. Callers go through
`redis_op`, which returns the supplied `default` (conventionally `None`) when
Redis is unreachable, so a caller can tell "cache miss" from "cache down" by
choosing a sentinel default.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

import redis
from redis.client import Pipeline

from shared import config
from shared.logging_utils import get_logger

logger = get_logger("geotag.redis")

T = TypeVar("T")

_client: redis.Redis | None = None
_client_lock = threading.Lock()

# After a failure, skip Redis entirely for this long instead of eating a
# connect timeout on every one of (say) 500 batched pings.
_unavailable_until: float = 0.0


def _cooldown_seconds() -> float:
    return float(config.env_int("REDIS_FAILURE_COOLDOWN_SECONDS", 5))


def get_client() -> redis.Redis:
    """Return the process-wide Redis client (lazy, does not connect eagerly)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = redis.Redis(
                    host=config.redis_host(),
                    port=config.redis_port(),
                    db=config.env_int("REDIS_DB", 0),
                    password=config.env("REDIS_PASSWORD"),
                    decode_responses=True,
                    ssl=config.redis_tls(),
                    socket_connect_timeout=float(config.env_int("REDIS_CONNECT_TIMEOUT_MS", 500)) / 1000.0,
                    socket_timeout=float(config.env_int("REDIS_SOCKET_TIMEOUT_MS", 1000)) / 1000.0,
                    retry_on_timeout=False,
                    health_check_interval=0,
                )
                logger.info(
                    "redis_client_created",
                    extra={"host": config.redis_host(), "port": config.redis_port()},
                )
    return _client


def reset_client() -> None:
    """Drop the client and clear the failure cooldown (used by tests)."""
    global _client, _unavailable_until
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except (redis.RedisError, OSError):
                pass
        _client = None
        _unavailable_until = 0.0


def _mark_unavailable(operation: str, exc: BaseException) -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + _cooldown_seconds()
    logger.warning(
        "redis_unavailable",
        extra={"operation": operation, "detail": str(exc), "degraded_to": "postgres"},
    )


def is_degraded() -> bool:
    """True when Redis is inside its failure cooldown."""
    return time.monotonic() < _unavailable_until


def redis_op(operation: str, fn: Callable[[redis.Redis], T], default: T) -> T:
    """Run `fn` against Redis, returning `default` if Redis is unavailable."""
    if is_degraded():
        return default
    try:
        return fn(get_client())
    except (redis.RedisError, OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError (a ValueError subclass) covers a genuinely
        # corrupted response under `decode_responses=True` — a real Redis
        # problem. A bare `ValueError` was caught here too until this fix,
        # which meant any *application* bug in a callback that happened to
        # raise ValueError (e.g. a bad int() parse) was silently misfiled as
        # "Redis unavailable" and tripped the process-wide failure cooldown,
        # masking the real bug as a transient infra issue.
        _mark_unavailable(operation, exc)
        return default


def redis_pipeline(operation: str, fn: Callable[[Pipeline], None]) -> bool:
    """Run a buffered pipeline. Returns False if Redis was unavailable."""

    def run(client: redis.Redis) -> bool:
        pipe = client.pipeline(transaction=False)
        fn(pipe)
        pipe.execute()
        return True

    return redis_op(operation, run, False)


def ping() -> bool:
    """Best-effort liveness probe."""
    return bool(redis_op("ping", lambda client: client.ping(), False))


def as_any(client: redis.Redis) -> Any:
    """Escape hatch for typing when chaining raw commands."""
    return client
