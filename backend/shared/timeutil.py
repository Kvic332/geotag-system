"""Timestamp handling.

API-CONTRACT: responses carry ISO-8601 UTC strings; ping payloads carry unix
seconds. Everything the system stores is truncated to whole seconds so that a
value written from a unix-second ping and a value written from server `now()`
are the same shape, and so the `positions` primary key
`(tenant_id, device_id, recorded_at)` behaves predictably.
"""

from __future__ import annotations

from datetime import datetime, timezone

from shared.errors import ValidationError

ISO_SECONDS = "%Y-%m-%dT%H:%M:%SZ"

# Bounds that catch "this is milliseconds, not seconds" mistakes from clients.
_MIN_UNIX = 0
_MAX_UNIX = 4_102_444_800  # 2100-01-01T00:00:00Z


def utcnow() -> datetime:
    """Current UTC time, truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_unix(seconds: float) -> datetime:
    if not _MIN_UNIX <= seconds <= _MAX_UNIX:
        raise ValidationError("timestamp is out of range (expected unix seconds)")
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def to_unix(value: datetime) -> int:
    return int(_as_utc(value).timestamp())


def to_iso(value: datetime | None) -> str | None:
    """Render as `2026-09-09T12:00:00Z` (API-CONTRACT response format)."""
    if value is None:
        return None
    return _as_utc(value).replace(microsecond=0).strftime(ISO_SECONDS)


def parse_time(raw: str | int | float, field: str) -> datetime:
    """Accept ISO-8601 (with or without `Z`) or unix seconds."""
    if isinstance(raw, bool):
        raise ValidationError(f"{field} must be an ISO-8601 string or unix seconds")
    if isinstance(raw, (int, float)):
        return from_unix(raw)

    text = str(raw).strip()
    if not text:
        raise ValidationError(f"{field} must be an ISO-8601 string or unix seconds")

    if text.lstrip("-").replace(".", "", 1).isdigit():
        try:
            return from_unix(float(text))
        except ValueError as exc:
            raise ValidationError(f"{field} is not a valid timestamp") from exc

    normalised = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValidationError(f"{field} is not a valid ISO-8601 timestamp") from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
