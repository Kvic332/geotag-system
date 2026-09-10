"""Redis key names.

Structure is exactly CLAUDE.md's Redis data model, prefixed with `t:{tenant_id}:`
per SPEC-DECISIONS D6, plus the two dwell-state keys from D3.

`devices:active` is a ZSET, not a TTL'd SET — see SPEC-DECISIONS D5.
"""

from __future__ import annotations


def _prefix(tenant_id: str) -> str:
    return f"t:{tenant_id}:"


def position(tenant_id: str, device_id: str) -> str:
    """HASH (lat, lng, accuracy, speed, bearing, ts, battery), TTL 3600s."""
    return f"{_prefix(tenant_id)}device:{device_id}:position"


def active_devices(tenant_id: str) -> str:
    """ZSET device_id -> unix ts of last ping (D5)."""
    return f"{_prefix(tenant_id)}devices:active"


def zone_members(tenant_id: str, geofence_id: str) -> str:
    """SET of device_ids currently inside the zone."""
    return f"{_prefix(tenant_id)}geofence:{geofence_id}:members"


def device_zones(tenant_id: str, device_id: str) -> str:
    """SET of geofence ids the device is currently inside."""
    return f"{_prefix(tenant_id)}device:{device_id}:zones"


def entered_at(tenant_id: str, geofence_id: str, device_id: str) -> str:
    """STRING unix ts of entry, TTL 86400 (D3)."""
    return f"{_prefix(tenant_id)}geofence:{geofence_id}:entered:{device_id}"


def dwelled(tenant_id: str, geofence_id: str, device_id: str) -> str:
    """STRING "1" once a dwell has fired for this occupancy, TTL 86400 (D3)."""
    return f"{_prefix(tenant_id)}geofence:{geofence_id}:dwelled:{device_id}"
