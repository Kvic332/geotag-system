-- 001_init.sql — core schema for the geo-tagging system.
--
-- Base schema is CLAUDE.md "Database schema (PostGIS)", amended by:
--   D2 — shape_type / center / radius_m on geofences (circles accepted, polygons stored)
--   D3 — dwell_threshold_seconds on geofences
--   D6 — tenant_id on every table, composite (tenant-leading) primary keys
--
-- Safe to re-run: every object is created IF NOT EXISTS.

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS btree_gist; -- lets GIST indexes lead with tenant_id (D6)

-- ---------------------------------------------------------------------------
-- positions — every ping ever received. Latest ping is also cached in Redis,
-- but Postgres is the source of truth (CLAUDE.md coding standards).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    tenant_id   TEXT        NOT NULL,
    device_id   TEXT        NOT NULL,
    location    GEOGRAPHY(POINT, 4326) NOT NULL,
    accuracy    DOUBLE PRECISION,
    speed       DOUBLE PRECISION,
    bearing     DOUBLE PRECISION,
    battery     INT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (tenant_id, device_id, recorded_at),

    CONSTRAINT positions_tenant_id_not_blank CHECK (length(tenant_id) > 0),
    CONSTRAINT positions_device_id_not_blank CHECK (length(device_id) > 0),
    CONSTRAINT positions_accuracy_range      CHECK (accuracy IS NULL OR accuracy >= 0),
    CONSTRAINT positions_speed_range         CHECK (speed    IS NULL OR speed    >= 0),
    CONSTRAINT positions_bearing_range       CHECK (bearing  IS NULL OR (bearing >= 0 AND bearing < 360)),
    CONSTRAINT positions_battery_range       CHECK (battery  IS NULL OR (battery BETWEEN 0 AND 100)),
    CONSTRAINT positions_lat_range           CHECK (ST_Y(location::geometry) BETWEEN -90  AND 90),
    CONSTRAINT positions_lng_range           CHECK (ST_X(location::geometry) BETWEEN -180 AND 180)
);

-- ---------------------------------------------------------------------------
-- geofences — polygon zones. Circles are accepted on the API and materialised
-- into `boundary` via ST_Buffer, keeping center/radius_m so they round-trip
-- as circles (D2). Containment ALWAYS uses `boundary`, so the engine has one
-- code path.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geofences (
    tenant_id                TEXT        NOT NULL,
    id                       UUID        NOT NULL DEFAULT gen_random_uuid(),
    name                     TEXT        NOT NULL,
    shape_type               TEXT        NOT NULL DEFAULT 'polygon',
    boundary                 GEOGRAPHY(POLYGON, 4326) NOT NULL,
    center                   GEOGRAPHY(POINT, 4326),
    radius_m                 DOUBLE PRECISION,
    dwell_threshold_seconds  INT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (tenant_id, id),
    -- id is a UUID and therefore globally unique on its own. The extra UNIQUE
    -- lets device_events keep a single-column FK with ON DELETE SET NULL,
    -- which a composite FK could not do (tenant_id is NOT NULL there).
    CONSTRAINT geofences_id_unique UNIQUE (id),

    CONSTRAINT geofences_tenant_id_not_blank CHECK (length(tenant_id) > 0),
    CONSTRAINT geofences_name_not_blank      CHECK (length(btrim(name)) > 0),
    CONSTRAINT geofences_shape_type          CHECK (shape_type IN ('polygon', 'circle')),
    CONSTRAINT geofences_radius_range        CHECK (radius_m IS NULL OR (radius_m > 0 AND radius_m <= 100000)),
    CONSTRAINT geofences_dwell_positive      CHECK (dwell_threshold_seconds IS NULL OR dwell_threshold_seconds > 0),
    -- D2: center/radius_m are non-null IF AND ONLY IF shape_type = 'circle'.
    CONSTRAINT geofences_circle_fields CHECK (
        (shape_type = 'circle'  AND center IS NOT NULL AND radius_m IS NOT NULL)
     OR (shape_type = 'polygon' AND center IS NULL     AND radius_m IS NULL)
    )
);

-- ---------------------------------------------------------------------------
-- device_events — enter / exit / dwell.
--
-- geofence_name is denormalised so that history stays readable after a zone is
-- deleted (API-CONTRACT DELETE /geofences/{id}: "not orphan device_events rows
-- ... keeping event history readable"); geofence_id then goes NULL.
-- dwell_ms is required by GET /events and is non-null only on dwell events (D3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_events (
    tenant_id     TEXT        NOT NULL,
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    device_id     TEXT        NOT NULL,
    geofence_id   UUID        REFERENCES geofences(id) ON DELETE SET NULL,
    geofence_name TEXT,
    event_type    TEXT        NOT NULL,
    location      GEOGRAPHY(POINT, 4326),
    dwell_ms      BIGINT,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (tenant_id, id),

    CONSTRAINT device_events_tenant_id_not_blank CHECK (length(tenant_id) > 0),
    CONSTRAINT device_events_device_id_not_blank CHECK (length(device_id) > 0),
    CONSTRAINT device_events_event_type          CHECK (event_type IN ('enter', 'exit', 'dwell')),
    CONSTRAINT device_events_dwell_ms_range      CHECK (dwell_ms IS NULL OR dwell_ms >= 0),
    -- dwell_ms is non-null only on dwell events (API-CONTRACT GET /events).
    CONSTRAINT device_events_dwell_ms_only_on_dwell CHECK (
        (event_type = 'dwell') OR (dwell_ms IS NULL)
    )
);

COMMIT;
