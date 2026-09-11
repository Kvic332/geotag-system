"""GET /devices/{device_id}/residence

Detects the probable home address of a device by clustering night-time GPS
pings (10 pm – 6 am WAT) over the last 14 days.  Pings are bucketed into
~100 m grid cells (lat/lng rounded to 3 decimal places).  The cell with the
most pings is returned as the detected residence, together with a confidence
score (that cell's share of all night-time pings).

Response shape:
  {
    "device_id": "...",
    "cluster_lat": 6.482,
    "cluster_lng": 3.284,
    "night_pings": 42,        # pings in the top cluster
    "total_night_pings": 50,  # all night pings for this device in the window
    "days_analyzed": 7,       # distinct nights with any data
    "confidence_pct": 84.0    # night_pings / total_night_pings * 100
  }

Returns 404 when there are no night-time pings in the 14-day window.
"""

import logging

from shared.apigw import Router
from shared.auth import require_auth
from shared.db import db
from shared.responses import api_handler, json_response

logger = logging.getLogger(__name__)
router = Router()

_SQL = """
WITH clustered AS (
    SELECT
        ROUND(ST_Y(location::geometry)::numeric, 3) AS cluster_lat,
        ROUND(ST_X(location::geometry)::numeric, 3) AS cluster_lng,
        recorded_at
    FROM positions
    WHERE
        tenant_id = %(tenant_id)s
        AND device_id = %(device_id)s
        AND recorded_at >= NOW() - INTERVAL '14 days'
        AND (
            EXTRACT(HOUR FROM recorded_at AT TIME ZONE 'Africa/Lagos') >= 22
            OR EXTRACT(HOUR FROM recorded_at AT TIME ZONE 'Africa/Lagos') < 6
        )
),
totals AS (
    SELECT
        cluster_lat,
        cluster_lng,
        COUNT(*)                                                          AS night_pings,
        COUNT(DISTINCT DATE(recorded_at AT TIME ZONE 'Africa/Lagos'))    AS active_nights
    FROM clustered
    GROUP BY cluster_lat, cluster_lng
),
total_count AS (SELECT SUM(night_pings) AS total_night_pings FROM totals),
total_days  AS (
    SELECT COUNT(DISTINCT DATE(recorded_at AT TIME ZONE 'Africa/Lagos')) AS days_analyzed
    FROM clustered
)
SELECT
    t.cluster_lat,
    t.cluster_lng,
    t.night_pings,
    tc.total_night_pings,
    td.days_analyzed,
    ROUND(t.night_pings::numeric / NULLIF(tc.total_night_pings, 0) * 100, 1) AS confidence_pct
FROM totals t, total_count tc, total_days td
ORDER BY t.night_pings DESC
LIMIT 1
"""


@router.route("GET", "/devices/{device_id}/residence")
def get_residence(event, _context):
    auth = require_auth(event)
    device_id = (event.get("pathParameters") or {}).get("device_id", "")

    row = db.query_one(_SQL, {"tenant_id": auth.tenant_id, "device_id": device_id})

    if row is None:
        return json_response(404, {"error": {
            "code": "not_found",
            "message": f"No night-time pings found for device '{device_id}' in the last 14 days.",
        }})

    return json_response(200, {
        "device_id": device_id,
        "cluster_lat": float(row["cluster_lat"]),
        "cluster_lng": float(row["cluster_lng"]),
        "night_pings": int(row["night_pings"]),
        "total_night_pings": int(row["total_night_pings"]),
        "days_analyzed": int(row["days_analyzed"]),
        "confidence_pct": float(row["confidence_pct"]),
    })


@api_handler(logger)
def lambda_handler(event, context):
    return router.dispatch(event, context)
