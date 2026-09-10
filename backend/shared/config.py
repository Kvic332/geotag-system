"""Environment configuration. Secrets are read from env vars only — never hardcoded."""

from __future__ import annotations

import os

DEFAULT_AWS_REGION = "eu-west-1"


def env(name: str, default: str | None = None) -> str | None:
    """Return an env var, treating blank/whitespace-only as unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    stripped = raw.strip()
    return stripped if stripped else default


def require_env(name: str) -> str:
    """Return an env var or raise — used for values with no safe default."""
    value = env(name)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def env_int(name: str, default: int) -> int:
    value = env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def environment() -> str:
    return (env("ENVIRONMENT", "local") or "local").lower()


def is_local() -> bool:
    return environment() != "production"


def aws_region() -> str:
    return env("AWS_REGION", DEFAULT_AWS_REGION) or DEFAULT_AWS_REGION


def database_url() -> str:
    return env("DATABASE_URL", "postgresql://postgres:dev@localhost:5432/geotag") or ""


def redis_host() -> str:
    return env("REDIS_HOST", "localhost") or "localhost"


def redis_port() -> int:
    return env_int("REDIS_PORT", 6379)


# --- Domain constants -------------------------------------------------------

ACTIVE_WINDOW_SECONDS = 3600      # CLAUDE.md: "devices seen in the last hour"
POSITION_TTL_SECONDS = 3600       # CLAUDE.md Redis data model
DWELL_STATE_TTL_SECONDS = 86400   # SPEC-DECISIONS D3
MAX_DWELL_THRESHOLD_SECONDS = 2_592_000  # 30 days — SPEC-DECISIONS D3 addendum
MAX_BATCH_PINGS = 500             # SPEC-DECISIONS D4
MAX_RADIUS_M = 100_000            # API-CONTRACT POST /geofences
CIRCLE_QUAD_SEGS = 16             # 16 per quarter-circle == 64 segments (D2)
HISTORY_DEFAULT_LIMIT = 1000
HISTORY_MAX_LIMIT = 10_000
EVENTS_DEFAULT_LIMIT = 100
EVENTS_MAX_LIMIT = 1000
