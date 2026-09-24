"""intelligence_engine — AI-powered per-device location intelligence.

Routes:
  GET  /devices/{device_id}/intelligence          — read cached analysis
  POST /devices/{device_id}/intelligence/generate — run fresh analysis via Claude

The generate endpoint gathers all position + event data for the device and
upserts an analysis into device_intelligence. GET just reads the cached row.

With ANTHROPIC_API_KEY set the analysis is written by Claude; without it the
built-in rule engine (local_analyzer) produces the same fields at no cost.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.errors import UndefinedTable

from shared.apigw import Router
from shared.auth import require_auth
from shared.db import execute, query_all, query_one
from shared.errors import InternalError, NotFoundError
from shared.responses import api_handler, json_response
from shared import config
from shared.timeutil import to_iso, utcnow

from intelligence_engine import local_analyzer

logger = logging.getLogger(__name__)
router = Router()

_MODEL = "claude-haiku-4-5-20251001"

_MISSING_TABLE = (
    "The device_intelligence table does not exist. "
    "Apply infra/migrations/003_intelligence.sql to the database."
)

# ---------------------------------------------------------------------------
# Data-gathering SQL
# ---------------------------------------------------------------------------

_SQL_NIGHT_CLUSTER = """
WITH clustered AS (
    SELECT
        ROUND(ST_Y(location::geometry)::numeric, 3) AS cluster_lat,
        ROUND(ST_X(location::geometry)::numeric, 3) AS cluster_lng,
        recorded_at
    FROM positions
    WHERE tenant_id = %s AND device_id = %s
      AND (
          EXTRACT(HOUR FROM (recorded_at + INTERVAL '1 hour')) >= 22
          OR EXTRACT(HOUR FROM (recorded_at + INTERVAL '1 hour')) < 6
      )
),
totals AS (
    SELECT cluster_lat, cluster_lng, COUNT(*) AS night_pings
    FROM clustered
    GROUP BY cluster_lat, cluster_lng
),
total_count AS (SELECT COALESCE(SUM(night_pings), 0) AS total FROM totals)
SELECT
    t.cluster_lat, t.cluster_lng, t.night_pings,
    tc.total AS total_night_pings,
    ROUND(t.night_pings::numeric / NULLIF(tc.total, 0) * 100, 1) AS confidence_pct
FROM totals t, total_count tc
ORDER BY t.night_pings DESC
LIMIT 1
"""

_SQL_TOP_GEOFENCES = """
SELECT geofence_name, event_type, COUNT(*) AS cnt
  FROM device_events
 WHERE tenant_id = %s AND device_id = %s
   AND geofence_name IS NOT NULL AND geofence_name != ''
 GROUP BY geofence_name, event_type
 ORDER BY cnt DESC
 LIMIT 20
"""

_SQL_HOUR_DIST = """
SELECT
    EXTRACT(HOUR FROM (recorded_at + INTERVAL '1 hour'))::int AS hour_wat,
    COUNT(*) AS cnt
  FROM positions
 WHERE tenant_id = %s AND device_id = %s
 GROUP BY hour_wat
 ORDER BY hour_wat
"""

_SQL_DAY_DIST = """
SELECT
    EXTRACT(DOW FROM (recorded_at + INTERVAL '1 hour'))::int AS dow,
    COUNT(*) AS cnt
  FROM positions
 WHERE tenant_id = %s AND device_id = %s
 GROUP BY dow
 ORDER BY dow
"""

_SQL_DAY_CLUSTER = """
WITH clustered AS (
    SELECT
        ROUND(ST_Y(location::geometry)::numeric, 3) AS cluster_lat,
        ROUND(ST_X(location::geometry)::numeric, 3) AS cluster_lng
    FROM positions
    WHERE tenant_id = %s AND device_id = %s
      AND EXTRACT(HOUR FROM (recorded_at + INTERVAL '1 hour')) BETWEEN 9 AND 16
      AND EXTRACT(DOW FROM (recorded_at + INTERVAL '1 hour')) BETWEEN 1 AND 5
),
totals AS (
    SELECT cluster_lat, cluster_lng, COUNT(*) AS day_pings
    FROM clustered
    GROUP BY cluster_lat, cluster_lng
),
total_count AS (SELECT COALESCE(SUM(day_pings), 0) AS total FROM totals)
SELECT
    t.cluster_lat, t.cluster_lng, t.day_pings,
    tc.total AS total_day_pings,
    ROUND(t.day_pings::numeric / NULLIF(tc.total, 0) * 100, 1) AS share_pct
FROM totals t, total_count tc
ORDER BY t.day_pings DESC
LIMIT 1
"""

_SQL_SPREAD = """
WITH cells AS (
    SELECT
        ROUND(ST_Y(location::geometry)::numeric, 3) AS cell_lat,
        ROUND(ST_X(location::geometry)::numeric, 3) AS cell_lng,
        COUNT(*) AS c
    FROM positions
    WHERE tenant_id = %s AND device_id = %s
    GROUP BY 1, 2
)
SELECT
    (SELECT COUNT(*) FROM cells WHERE c >= 2) AS places,
    top.cell_lat AS top_lat,
    top.cell_lng AS top_lng,
    ROUND(top.c::numeric / NULLIF((SELECT SUM(c) FROM cells), 0) * 100, 1) AS top_share_pct
FROM (SELECT * FROM cells ORDER BY c DESC LIMIT 1) top
"""

_SQL_SUMMARY_STATS = """
SELECT
    COUNT(*)                                      AS total_pings,
    ROUND(AVG(speed)::numeric, 2)                 AS avg_speed,
    ROUND(MAX(speed)::numeric, 2)                 AS max_speed,
    COUNT(DISTINCT DATE(recorded_at + INTERVAL '1 hour')) AS active_days,
    MIN(recorded_at)                              AS first_seen,
    MAX(recorded_at)                              AS last_seen
  FROM positions
 WHERE tenant_id = %s AND device_id = %s
"""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _build_prompt(device_id: str, cluster: dict | None, geofences: list[dict],
                  hour_dist: list[dict], day_dist: list[dict], stats: dict) -> str:
    first_seen = stats.get("first_seen")
    last_seen = stats.get("last_seen")
    window_desc = (
        f"from {first_seen.strftime('%Y-%m-%d') if first_seen else 'unknown'} "
        f"to {last_seen.strftime('%Y-%m-%d') if last_seen else 'now'}"
    )
    lines: list[str] = [
        f"Analyze ALL available GPS tracking data for device '{device_id}' ({window_desc}). "
        "Return ONLY a single valid JSON object with the exact fields shown below — no markdown, no explanation.",
        "",
        "=== DATA ===",
        "",
    ]

    # Night cluster
    if cluster and cluster.get("night_pings"):
        lines += [
            "PROBABLE RESIDENCE (night-time cluster 22:00-06:00 WAT):",
            f"  Coordinates: {cluster['cluster_lat']}, {cluster['cluster_lng']}",
            f"  Night pings in top cluster: {cluster['night_pings']} / {cluster['total_night_pings']} ({cluster['confidence_pct']}% confidence)",
        ]
    else:
        lines.append("PROBABLE RESIDENCE: No night-time data available.")

    lines.append("")

    # Geofence events
    if geofences:
        lines.append("NAMED LOCATIONS VISITED (enter/exit/dwell events):")
        by_name: dict[str, dict[str, int]] = {}
        for row in geofences:
            name = row["geofence_name"]
            by_name.setdefault(name, {})
            by_name[name][row["event_type"]] = int(row["cnt"])
        for name, counts in list(by_name.items())[:10]:
            parts = []
            if counts.get("enter"):
                parts.append(f"entered {counts['enter']}x")
            if counts.get("exit"):
                parts.append(f"exited {counts['exit']}x")
            if counts.get("dwell"):
                parts.append(f"dwelled {counts['dwell']}x")
            lines.append(f"  - {name}: {', '.join(parts)}")
    else:
        lines.append("NAMED LOCATIONS: No geofence events recorded.")

    lines.append("")

    # Hour distribution
    if hour_dist:
        active_hours = sorted(
            [r for r in hour_dist if int(r["cnt"]) > 0],
            key=lambda r: int(r["cnt"]), reverse=True
        )[:5]
        hour_strs = [f"{int(r['hour_wat']):02d}:00 ({r['cnt']} pings)" for r in active_hours]
        lines.append(f"MOST ACTIVE HOURS (WAT): {', '.join(hour_strs)}")

    # Day distribution
    if day_dist:
        day_rows = [(int(r["dow"]), int(r["cnt"])) for r in day_dist]
        day_strs = [f"{_DOW_NAMES[dow]} ({cnt})" for dow, cnt in sorted(day_rows, key=lambda x: -x[1])[:5]]
        lines.append(f"MOST ACTIVE DAYS: {', '.join(day_strs)}")

    lines.append("")

    # Summary stats
    lines += [
        f"TOTAL PINGS: {stats.get('total_pings', 0)}",
        f"ACTIVE DAYS: {stats.get('active_days', 0)}",
        f"AVG SPEED: {stats.get('avg_speed') or 0} m/s  |  MAX SPEED: {stats.get('max_speed') or 0} m/s",
    ]

    lines += [
        "",
        "=== OUTPUT FORMAT (JSON only, no extra text) ===",
        "{",
        '  "location_type": "residential" | "workplace" | "transit_hub" | "mixed" | "unknown",',
        '  "home_area": "<neighborhood, city> or null",',
        '  "summary": "<2-3 sentence description of this device owner\'s patterns and likely daily routine>",',
        '  "patterns": ["<pattern 1>", "<pattern 2>"],',
        '  "anomalies": ["<anomaly 1>"] or [],',
        '  "confidence": "high" | "medium" | "low"',
        "}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(prompt: str) -> dict[str, Any]:
    api_key = config.env("ANTHROPIC_API_KEY")
    if not api_key:
        raise InternalError("AI is not configured: ANTHROPIC_API_KEY is not set on the server.")

    import anthropic  # local import: keep optional for deployments that don't use intelligence
    client = anthropic.Anthropic(api_key=api_key)

    try:
        message = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        raise InternalError("Anthropic rejected ANTHROPIC_API_KEY (401). Check the key on the server.") from None
    except anthropic.PermissionDeniedError:
        raise InternalError("ANTHROPIC_API_KEY lacks permission for this request (403).") from None
    except anthropic.NotFoundError:
        raise InternalError(f"Anthropic does not recognise model '{_MODEL}' (404).") from None
    except anthropic.RateLimitError:
        raise InternalError("Anthropic rate limit hit (429). Try again in a minute.") from None
    except anthropic.APIStatusError as exc:
        raise InternalError(f"Anthropic API error {exc.status_code}: {exc.message}") from None
    except anthropic.APIConnectionError:
        raise InternalError("Could not reach the Anthropic API from the server.") from None

    raw = message.content[0].text.strip()

    # Strip any accidental markdown code fences
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("intelligence_bad_json", extra={"stop_reason": message.stop_reason, "raw": raw[:500]})
        raise InternalError("The AI reply was not valid JSON. Try generating again.") from None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.route("GET", "/devices/{device_id}/intelligence")
def get_intelligence(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    auth = require_auth(event)
    device_id = params.get("device_id", "")

    try:
        row = query_one(
            "SELECT * FROM device_intelligence WHERE tenant_id = %s AND device_id = %s",
            (auth.tenant_id, device_id),
        )
    except UndefinedTable:
        raise InternalError(_MISSING_TABLE) from None

    if row is None:
        return json_response(404, {"error": {
            "code": "not_found",
            "message": f"No intelligence generated yet for device '{device_id}'. POST to /generate to create one.",
        }})

    return json_response(200, {
        "device_id": device_id,
        "generated_at": to_iso(row["generated_at"]),
        "location_type": row["location_type"],
        "home_area": row["home_area"],
        "summary": row["summary"],
        "patterns": row["patterns"] if isinstance(row["patterns"], list) else json.loads(row["patterns"] or "[]"),
        "anomalies": row["anomalies"] if isinstance(row["anomalies"], list) else json.loads(row["anomalies"] or "[]"),
        "confidence": row["confidence"],
        "model_used": row["model_used"],
    })


@router.route("POST", "/devices/{device_id}/intelligence/generate")
def generate_intelligence(event: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    auth = require_auth(event)
    device_id = params.get("device_id", "")

    exists = query_one(
        "SELECT 1 FROM positions WHERE tenant_id = %s AND device_id = %s LIMIT 1",
        (auth.tenant_id, device_id),
    )
    if exists is None:
        raise NotFoundError(f"No position data found for device '{device_id}'.")

    # Gather all data
    cluster = query_one(_SQL_NIGHT_CLUSTER, (auth.tenant_id, device_id))
    geofences = query_all(_SQL_TOP_GEOFENCES, (auth.tenant_id, device_id))
    hour_dist = query_all(_SQL_HOUR_DIST, (auth.tenant_id, device_id))
    day_dist = query_all(_SQL_DAY_DIST, (auth.tenant_id, device_id))
    stats = query_one(_SQL_SUMMARY_STATS, (auth.tenant_id, device_id)) or {}

    if config.env("ANTHROPIC_API_KEY"):
        model_used = _MODEL
        logger.info("intelligence_generate_start", extra={"device_id": device_id, "model": model_used})
        result = _call_claude(_build_prompt(device_id, cluster, geofences, hour_dist, day_dist, stats))
    else:
        model_used = local_analyzer.ENGINE_NAME
        logger.info("intelligence_generate_start", extra={"device_id": device_id, "model": model_used})
        result = local_analyzer.analyze(
            stats=stats,
            night=cluster,
            day=query_one(_SQL_DAY_CLUSTER, (auth.tenant_id, device_id)),
            spread=query_one(_SQL_SPREAD, (auth.tenant_id, device_id)),
            geofences=geofences,
            hour_dist=hour_dist,
            day_dist=day_dist,
        )

    location_type = result.get("location_type", "unknown")
    home_area = result.get("home_area")
    summary = result.get("summary", "")
    patterns = result.get("patterns", [])
    anomalies = result.get("anomalies", [])
    confidence = result.get("confidence", "low")

    try:
        execute(
            """
            INSERT INTO device_intelligence
                (tenant_id, device_id, generated_at, location_type, home_area,
                 summary, patterns, anomalies, confidence, model_used)
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, device_id) DO UPDATE SET
                generated_at = EXCLUDED.generated_at,
                location_type = EXCLUDED.location_type,
                home_area = EXCLUDED.home_area,
                summary = EXCLUDED.summary,
                patterns = EXCLUDED.patterns,
                anomalies = EXCLUDED.anomalies,
                confidence = EXCLUDED.confidence,
                model_used = EXCLUDED.model_used
            """,
            (
                auth.tenant_id, device_id, location_type, home_area,
                summary, json.dumps(patterns), json.dumps(anomalies), confidence, model_used,
            ),
        )
    except UndefinedTable:
        raise InternalError(_MISSING_TABLE) from None

    logger.info("intelligence_generate_done", extra={"device_id": device_id, "location_type": location_type})

    return json_response(200, {
        "device_id": device_id,
        "generated_at": to_iso(utcnow()),
        "location_type": location_type,
        "home_area": home_area,
        "summary": summary,
        "patterns": patterns,
        "anomalies": anomalies,
        "confidence": confidence,
        "model_used": model_used,
    })


@api_handler(logger)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return router.dispatch(event)
