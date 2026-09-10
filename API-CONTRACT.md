# API contract (frozen)

Every agent builds against this. The backend agent implements it; the dashboard and Android
agents consume it. **Nobody changes a shape here without saying so in their final report.**

Base URL local: `http://localhost:3000`. All routes require
`Authorization: Bearer <jwt>`. Missing/invalid token → `401`. Tenant comes from the
`custom:tenant_id` JWT claim (see SPEC-DECISIONS D6) — it is never a request parameter.

Errors are always: `{"error": {"code": "<machine_code>", "message": "<human text>"}}`
with codes `unauthorized` (401), `forbidden` (403), `not_found` (404),
`validation_error` (400), `internal_error` (500).

Coordinates are **always `[lng, lat]`** in arrays (GeoJSON order), and named `lat`/`lng` when
they are object fields. Timestamps in responses are ISO-8601 UTC strings. Timestamps in the
ping payload are unix seconds, per CLAUDE.md.

---

## POST /positions

Single ping (exactly the CLAUDE.md sample payload):

```json
{ "device_id": "device_abc123", "lat": 6.5244, "lng": 3.3792,
  "accuracy": 12.5, "speed": 0.0, "bearing": 180.0, "battery": 82,
  "timestamp": 1725868800 }
```

Required: `device_id`, `lat`, `lng`. Optional: `accuracy`, `speed`, `bearing`, `battery`,
`timestamp` (defaults to server now). `lat` in [-90,90], `lng` in [-180,180], `battery` in
[0,100], `accuracy` >= 0, `speed` >= 0, `bearing` in [0,360] when supplied — otherwise
`validation_error`.

`201` response, single form:

```json
{ "device_id": "device_abc123", "recorded_at": "2026-09-09T12:00:00Z",
  "events": [ { "event_type": "enter", "geofence_id": "uuid", "geofence_name": "Depot",
                "dwell_ms": null } ] }
```

Batch form (SPEC-DECISIONS D4) — body `{"pings": [ <ping>, ... ]}`, max 500:

```json
{ "accepted": 498, "rejected": [ { "index": 12, "error": "lat out of range" } ],
  "events": [ { "device_id": "d1", "event_type": "exit", "geofence_id": "uuid",
                "geofence_name": "Depot", "occurred_at": "2026-09-09T12:00:00Z",
                "dwell_ms": null } ] }
```

**D7 addendum (added post-build, see SPEC-DECISIONS.md):** every event object, wherever it
appears — here and in `GET /events` — carries `dwell_ms`: non-null only on `event_type:
"dwell"`, `null` on `enter`/`exit`. The Android SDK reads its callbacks from this array, not
from `GET /events`, so this field must be present here too, not only on the query endpoint.

## GET /positions/{device_id}

`200`:

```json
{ "device_id": "device_abc123", "lat": 6.5244, "lng": 3.3792, "accuracy": 12.5,
  "speed": 0.0, "bearing": 180.0, "battery": 82,
  "recorded_at": "2026-09-09T12:00:00Z", "source": "redis",
  "zones": ["uuid-of-zone-currently-inside"] }
```

`source` is `"redis"` or `"postgres"` — it must say `"postgres"` when Redis is down
(SPEC-DECISIONS, Redis-failure requirement). Unknown device → `404 not_found`.

## GET /positions/{device_id}/history

Query params: `from` and `to` (ISO-8601 or unix seconds, `from` defaults to 24h ago, `to`
defaults to now), `limit` (default 1000, max 10000). `from` after `to` → `validation_error`.

`200`:

```json
{ "device_id": "device_abc123", "count": 2,
  "positions": [ { "lat": 6.5244, "lng": 3.3792, "accuracy": 12.5, "speed": 0.0,
                   "bearing": 180.0, "battery": 82,
                   "recorded_at": "2026-09-09T12:00:00Z" } ] }
```

Ordered `recorded_at` ascending.

## GET /positions/active

Not in the CLAUDE.md route table, but the dashboard device list needs it and D5 makes it
cheap. Returns devices seen in the last hour, newest first.

```json
{ "count": 2, "devices": [ { "device_id": "d1", "lat": 6.5, "lng": 3.3, "battery": 82,
                             "recorded_at": "2026-09-09T12:00:00Z" } ] }
```

## GET /geofences

```json
{ "count": 1, "geofences": [ <geofence> ] }
```

A `<geofence>` is:

```json
{ "id": "uuid", "name": "Depot", "shape_type": "polygon",
  "coordinates": [[3.37,6.52],[3.38,6.52],[3.38,6.53],[3.37,6.52]],
  "center": null, "radius_m": null,
  "dwell_threshold_seconds": 300,
  "created_at": "2026-09-09T12:00:00Z", "device_count": 3 }
```

For `shape_type: "circle"`, `center` is `[lng, lat]` and `radius_m` is a number;
`coordinates` still carries the materialised buffer ring so the map can draw it either way.
`device_count` is the current occupancy from `geofence:{id}:members`.

## POST /geofences

```json
{ "name": "Depot", "type": "polygon",
  "coordinates": [[3.37,6.52],[3.38,6.52],[3.38,6.53],[3.37,6.52]],
  "dwell_threshold_seconds": 300 }
```

or

```json
{ "name": "Depot", "type": "circle", "center": [3.3792, 6.5244], "radius_m": 250,
  "dwell_threshold_seconds": null }
```

Polygon rings must have >= 4 positions and be closed (first == last); the API closes an
unclosed ring rather than rejecting it. `radius_m` must be > 0 and <= 100000.
`201` → the full `<geofence>` object.

## PUT /geofences/{id}

Same body as POST, all fields optional; only supplied fields change. `200` → full
`<geofence>`. Changing geometry does **not** retroactively emit events — the next ping
reconciles membership naturally.

## DELETE /geofences/{id}

`204`, no body. Deleting a zone must not orphan `device_events` rows
(`ON DELETE SET NULL` on the FK, keeping event history readable).

## GET /events

Query params: `device_id`, `geofence_id`, `event_type` (`enter`|`exit`|`dwell`), `from`,
`to`, `limit` (default 100, max 1000), `cursor`.

```json
{ "count": 1, "next_cursor": "opaque-or-null",
  "events": [ { "id": "uuid", "device_id": "d1", "geofence_id": "uuid",
                "geofence_name": "Depot", "event_type": "enter",
                "lat": 6.5244, "lng": 3.3792,
                "occurred_at": "2026-09-09T12:00:00Z", "dwell_ms": null } ] }
```

Ordered `occurred_at` **descending** (newest first — it feeds the live event log).
`dwell_ms` is non-null only on `dwell` events.
