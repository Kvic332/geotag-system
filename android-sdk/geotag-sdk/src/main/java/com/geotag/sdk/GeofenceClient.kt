package com.geotag.sdk

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException

/**
 * A longitude/latitude pair.
 *
 * The field order in the constructor mirrors the wire order: API-CONTRACT.md states that
 * coordinates in **arrays are always `[lng, lat]`** (GeoJSON order), and only object fields are
 * named `lat`/`lng`. Getting this backwards silently puts every device in the wrong hemisphere,
 * so the type exists purely to make the order impossible to mix up.
 */
data class LngLat(val lng: Double, val lat: Double) {

    /** Serialises to `[lng, lat]` — GeoJSON order, as the contract requires. */
    internal fun toJsonArray(): JSONArray {
        val array = JSONArray()
        array.put(lng)
        array.put(lat)
        return array
    }

    internal companion object {
        @Throws(JSONException::class)
        fun fromJsonArray(array: JSONArray): LngLat =
            LngLat(lng = array.getDouble(0), lat = array.getDouble(1))
    }
}

/**
 * A geofence zone exactly as `GET /geofences` returns it (API-CONTRACT.md).
 *
 * [coordinates] always carries the materialised boundary ring, for circles too — the backend
 * stores circles as buffered polygons (SPEC-DECISIONS D2) — so a map can draw any zone from
 * this field alone. [center] and [radiusMeters] are non-null only when [shapeType] is
 * `"circle"`.
 */
data class GeofenceZone(
    val id: String,
    val name: String,
    val shapeType: String,
    val coordinates: List<LngLat>,
    val center: LngLat?,
    val radiusMeters: Double?,
    val dwellThresholdSeconds: Int?,
    /** ISO-8601 UTC, e.g. `2026-09-09T12:00:00Z`. Left as a string; the SDK does no parsing. */
    val createdAt: String?,
    /** Current occupancy reported by the backend. */
    val deviceCount: Int
) {
    internal companion object {
        @Throws(JSONException::class)
        fun fromJson(json: JSONObject): GeofenceZone {
            val ring = ArrayList<LngLat>()
            val coords = json.optJSONArray("coordinates")
            if (coords != null) {
                for (i in 0 until coords.length()) {
                    val pair = coords.optJSONArray(i) ?: continue
                    if (pair.length() >= 2) ring.add(LngLat.fromJsonArray(pair))
                }
            }
            val centerArray = json.optJSONArray("center")
            return GeofenceZone(
                id = json.getString("id"),
                name = json.optString("name"),
                shapeType = json.optString("shape_type", "polygon"),
                coordinates = ring,
                center = if (centerArray != null && centerArray.length() >= 2) {
                    LngLat.fromJsonArray(centerArray)
                } else {
                    null
                },
                radiusMeters = if (json.isNull("radius_m")) null else json.getDouble("radius_m"),
                dwellThresholdSeconds = if (json.isNull("dwell_threshold_seconds")) {
                    null
                } else {
                    json.getInt("dwell_threshold_seconds")
                },
                createdAt = if (json.isNull("created_at")) null else json.optString("created_at"),
                deviceCount = json.optInt("device_count", 0)
            )
        }
    }
}

/**
 * A geofence transition raised by the backend.
 *
 * Produced from the `events` array of the `POST /positions` batch response and, in the same
 * shape, from `GET /events`.
 */
data class GeofenceEvent(
    val deviceId: String,
    val geofenceId: String,
    val geofenceName: String?,
    /** `"enter"`, `"exit"` or `"dwell"`. */
    val eventType: String,
    /** ISO-8601 UTC string as sent by the backend, or null if it was omitted. */
    val occurredAt: String?,
    /** Non-null only on `dwell` events, and only when the backend supplies `dwell_ms`. */
    val dwellMs: Long?
)

/**
 * Parses one entry of an `events` array.
 *
 * The batch response's event objects carry `device_id`; the single-ping response's do not, so
 * [fallbackDeviceId] fills in. Returns null rather than throwing if `geofence_id` or
 * `event_type` is missing, so one malformed event cannot lose the rest of the batch.
 */
internal fun JSONObject.toGeofenceEventOrNull(fallbackDeviceId: String?): GeofenceEvent? {
    val geofenceId = optString("geofence_id").takeIf { it.isNotEmpty() } ?: return null
    val eventType = optString("event_type").takeIf { it.isNotEmpty() } ?: return null
    val deviceId = optString("device_id").takeIf { it.isNotEmpty() } ?: fallbackDeviceId ?: return null
    return GeofenceEvent(
        deviceId = deviceId,
        geofenceId = geofenceId,
        geofenceName = optString("geofence_name").takeIf { it.isNotEmpty() },
        eventType = eventType,
        occurredAt = optString("occurred_at").takeIf { it.isNotEmpty() },
        dwellMs = if (isNull("dwell_ms")) null else optLong("dwell_ms")
    )
}

/**
 * Registers and deregisters geofence zones against the backend.
 *
 * Zones are server-side objects: containment and enter/exit/dwell diffing happen in the backend
 * (SPEC-DECISIONS D1/D3), and the resulting events come back to the device on the
 * `POST /positions` response. This client therefore wraps the `/geofences` REST routes; it does
 * **not** register OS-level geofences with `com.google.android.gms.location.GeofencingClient`.
 *
 * Reach it via [GeoTagClient.geofences]. Every method is a `suspend` function that does its
 * network I/O on [kotlinx.coroutines.Dispatchers.IO]; nothing here touches the main thread.
 *
 * Note that geofence management is normally an operator/dashboard concern — most tracked
 * devices only ever call [GeoTagClient.startTracking]. These methods exist for apps that also
 * act as an admin surface.
 */
class GeofenceClient internal constructor(
    private val apiUrl: String,
    private val apiKeyProvider: () -> String
) {

    /** Lists every zone visible to the caller's tenant. */
    @Throws(GeoTagException::class)
    suspend fun listZones(): List<GeofenceZone> {
        val response = send("GET", "$apiUrl/geofences", body = null)
        val root = parseJson(response.body, "GET /geofences")
        val array = root.optJSONArray("geofences") ?: return emptyList()
        val zones = ArrayList<GeofenceZone>(array.length())
        for (i in 0 until array.length()) {
            val obj = array.optJSONObject(i) ?: continue
            try {
                zones.add(GeofenceZone.fromJson(obj))
            } catch (e: JSONException) {
                throw GeoTagException("Malformed geofence in GET /geofences response", e)
            }
        }
        return zones
    }

    /**
     * Registers a polygon zone.
     *
     * [ring] is in `[lng, lat]` order. A ring that is not closed is closed automatically; the
     * backend does the same, but doing it here means the request that goes out is already valid.
     * At least four positions are required once closed.
     */
    @Throws(GeoTagException::class)
    suspend fun registerPolygonZone(
        name: String,
        ring: List<LngLat>,
        dwellThresholdSeconds: Int? = null
    ): GeofenceZone {
        require(name.isNotBlank()) { "Geofence name must not be blank" }
        val closed = closeRing(ring)
        require(closed.size >= 4) {
            "A polygon ring needs at least 4 positions once closed (got ${closed.size})"
        }
        val ringJson = JSONArray()
        for (point in closed) ringJson.put(point.toJsonArray())

        val body = JSONObject()
        body.put("name", name)
        body.put("type", "polygon")
        body.put("coordinates", ringJson)
        body.put("dwell_threshold_seconds", dwellThresholdSeconds ?: JSONObject.NULL)
        return createZone(body)
    }

    /**
     * Registers a circular zone. The backend materialises it into a polygon boundary
     * (SPEC-DECISIONS D2) but returns it as a circle.
     *
     * [radiusMeters] must be greater than 0 and at most 100000.
     */
    @Throws(GeoTagException::class)
    suspend fun registerCircleZone(
        name: String,
        center: LngLat,
        radiusMeters: Double,
        dwellThresholdSeconds: Int? = null
    ): GeofenceZone {
        require(name.isNotBlank()) { "Geofence name must not be blank" }
        require(radiusMeters > 0.0 && radiusMeters <= MAX_RADIUS_METERS) {
            "radiusMeters must be in (0, $MAX_RADIUS_METERS] (got $radiusMeters)"
        }
        val body = JSONObject()
        body.put("name", name)
        body.put("type", "circle")
        body.put("center", center.toJsonArray())
        body.put("radius_m", radiusMeters)
        body.put("dwell_threshold_seconds", dwellThresholdSeconds ?: JSONObject.NULL)
        return createZone(body)
    }

    /** Deletes a zone. `DELETE /geofences/{id}` answers `204` with no body. */
    @Throws(GeoTagException::class)
    suspend fun deregisterZone(zoneId: String) {
        require(zoneId.isNotBlank()) { "zoneId must not be blank" }
        send("DELETE", "$apiUrl/geofences/$zoneId", body = null)
    }

    // ---------------------------------------------------------------- internals

    private suspend fun createZone(body: JSONObject): GeofenceZone {
        val response = send("POST", "$apiUrl/geofences", body = body.toString())
        val root = parseJson(response.body, "POST /geofences")
        return try {
            GeofenceZone.fromJson(root)
        } catch (e: JSONException) {
            throw GeoTagException("Malformed geofence in POST /geofences response", e)
        }
    }

    private suspend fun send(method: String, url: String, body: String?): HttpResponse {
        val response = try {
            GeoTagHttp.request(url, method, apiKeyProvider(), body)
        } catch (e: IOException) {
            throw GeoTagNetworkException("$method $url failed", e)
        }
        if (response.code !in 200..299) throw response.toApiException(method, url)
        return response
    }

    private fun parseJson(body: String, what: String): JSONObject = try {
        JSONObject(body)
    } catch (e: JSONException) {
        throw GeoTagException("$what returned a non-JSON body", e)
    }

    private fun closeRing(ring: List<LngLat>): List<LngLat> {
        if (ring.isEmpty()) return ring
        return if (ring.first() == ring.last()) ring else ring + ring.first()
    }

    internal companion object {
        const val MAX_RADIUS_METERS = 100_000.0
    }
}

/**
 * Maps a non-2xx response onto the frozen error envelope
 * `{"error": {"code": "...", "message": "..."}}` from API-CONTRACT.md.
 */
internal fun HttpResponse.toApiException(method: String, url: String): GeoTagApiException {
    var code: String? = null
    var message: String? = null
    try {
        val error = JSONObject(body).optJSONObject("error")
        if (error != null) {
            code = error.optString("code").takeIf { it.isNotEmpty() }
            message = error.optString("message").takeIf { it.isNotEmpty() }
        }
    } catch (e: JSONException) {
        // Non-JSON error body (e.g. an API Gateway HTML page). Fall back to the status line.
    }
    return GeoTagApiException(
        httpStatus = this.code,
        errorCode = code,
        message = "$method $url failed with HTTP ${this.code}" +
            (code?.let { " ($it)" } ?: "") +
            (message?.let { ": $it" } ?: "")
    )
}
