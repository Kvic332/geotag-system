-- 002_indexes.sql — spatial + btree indexes.
--
-- D6: every index leads with tenant_id. For the GIST indexes that means the
-- btree_gist extension (created in 001) so a scalar column can share a GIST
-- index with a geography column.
--
-- The btree indexes below are exactly the ones the API-CONTRACT query patterns
-- need — no speculative extras.
--
-- Safe to re-run.

BEGIN;

-- ---------------------------------------------------------------------------
-- positions
-- ---------------------------------------------------------------------------

-- GET /positions/{device_id} (latest) and .../history (time range) are both
-- served by the primary key (tenant_id, device_id, recorded_at) — no extra
-- index needed for them.

-- GET /positions/active — Postgres fallback when Redis is down: "devices for
-- this tenant seen since <now-1h>", newest first, DISTINCT ON (device_id).
CREATE INDEX IF NOT EXISTS idx_positions_tenant_recent
    ON positions (tenant_id, recorded_at DESC);

-- Spatial index on the raw pings. Not on the hot API path today, but it is the
-- index that makes any "who was near X" / proximity query possible at all, and
-- it is what the GIST requirement in the build order refers to.
CREATE INDEX IF NOT EXISTS idx_positions_tenant_location_gist
    ON positions USING GIST (tenant_id, location);

-- ---------------------------------------------------------------------------
-- geofences
-- ---------------------------------------------------------------------------

-- The hot path: PostGISGeofenceEngine.zones_containing() does
--   WHERE tenant_id = %s AND ST_Covers(boundary, point)
CREATE INDEX IF NOT EXISTS idx_geofences_tenant_boundary_gist
    ON geofences USING GIST (tenant_id, boundary);

-- Circle round-trip / centre proximity (D2).
CREATE INDEX IF NOT EXISTS idx_geofences_tenant_center_gist
    ON geofences USING GIST (tenant_id, center)
    WHERE center IS NOT NULL;

-- GET /geofences — list for a tenant, newest first.
CREATE INDEX IF NOT EXISTS idx_geofences_tenant_created
    ON geofences (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- device_events
-- ---------------------------------------------------------------------------

-- GET /events with no filter: ORDER BY occurred_at DESC, id DESC (cursor
-- pagination keys on that pair, so the index carries both).
CREATE INDEX IF NOT EXISTS idx_events_tenant_occurred
    ON device_events (tenant_id, occurred_at DESC, id DESC);

-- GET /events?device_id=...
CREATE INDEX IF NOT EXISTS idx_events_tenant_device_occurred
    ON device_events (tenant_id, device_id, occurred_at DESC, id DESC);

-- GET /events?geofence_id=...
CREATE INDEX IF NOT EXISTS idx_events_tenant_geofence_occurred
    ON device_events (tenant_id, geofence_id, occurred_at DESC, id DESC);

-- GET /events?event_type=...
CREATE INDEX IF NOT EXISTS idx_events_tenant_type_occurred
    ON device_events (tenant_id, event_type, occurred_at DESC, id DESC);

COMMIT;
