package com.geotag.sdk

/**
 * Receives geofence transitions for the device this SDK instance is tracking.
 *
 * The three method signatures are fixed by the "Android SDK usage" snippet in CLAUDE.md and
 * must not change:
 *
 * ```kotlin
 * client.setEventCallback(object : GeofenceEventCallback {
 *     override fun onEnter(deviceId: String, geofenceId: String) { }
 *     override fun onExit(deviceId: String, geofenceId: String) { }
 *     override fun onDwell(deviceId: String, geofenceId: String, dwellMs: Long) { }
 * })
 * ```
 *
 * Events are produced by the **backend**, not by the device: enter/exit/dwell diffing lives in
 * `event_processor` (SPEC-DECISIONS D1/D3). The SDK surfaces them from the `events` array of
 * the `POST /positions` batch response, so a callback fires at most one batch interval after
 * the ping that caused it.
 *
 * All three methods are invoked on the **main thread**, so they are safe for UI updates.
 * Because of that, do not block in them — hand long work to your own coroutine or executor.
 * An exception thrown from a callback is caught and logged; it will not stop tracking.
 */
interface GeofenceEventCallback {

    /** The device crossed into [geofenceId]. */
    fun onEnter(deviceId: String, geofenceId: String)

    /** The device crossed out of [geofenceId]. */
    fun onExit(deviceId: String, geofenceId: String)

    /**
     * The device has been continuously inside [geofenceId] for at least that zone's
     * `dwell_threshold_seconds`. Fires at most once per continuous occupancy (SPEC-DECISIONS D3).
     *
     * [dwellMs] is how long the device had been inside when the event was raised.
     *
     * `dwell_ms` is present on every event object the backend returns, including the
     * `events` array of the `POST /positions` batch response this SDK reads from
     * (API-CONTRACT.md D7) — the parser's `?: 0L` fallback below is defensive only,
     * not a workaround for a live contract gap.
     */
    fun onDwell(deviceId: String, geofenceId: String, dwellMs: Long)
}
