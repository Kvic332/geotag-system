-- 003_intelligence.sql — per-device AI intelligence cache.
--
-- One row per (tenant_id, device_id). Overwritten on each regeneration.
-- Safe to re-run: IF NOT EXISTS.

BEGIN;

CREATE TABLE IF NOT EXISTS device_intelligence (
    tenant_id       TEXT        NOT NULL,
    device_id       TEXT        NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    location_type   TEXT,
    home_area       TEXT,
    summary         TEXT,
    patterns        JSONB       NOT NULL DEFAULT '[]',
    anomalies       JSONB       NOT NULL DEFAULT '[]',
    confidence      TEXT,
    model_used      TEXT,

    PRIMARY KEY (tenant_id, device_id)
);

COMMIT;
