package com.geotag.sdk

import android.Manifest
import android.content.Context
import android.location.Location
import android.os.BatteryManager
import android.util.Log
import androidx.annotation.RequiresPermission
import kotlinx.coroutines.CoroutineName
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import java.util.UUID

/**
 * Entry point of the GeoTag Android SDK.
 *
 * ```kotlin
 * val client = GeoTagClient.Builder(context)
 *     .apiUrl("https://your-api-gateway-url.amazonaws.com/prod")
 *     .apiKey("your-cognito-jwt")
 *     .batchIntervalSeconds(10)
 *     .build()
 *
 * client.startTracking()
 *
 * client.setEventCallback(object : GeofenceEventCallback {
 *     override fun onEnter(deviceId: String, geofenceId: String) { }
 *     override fun onExit(deviceId: String, geofenceId: String) { }
 *     override fun onDwell(deviceId: String, geofenceId: String, dwellMs: Long) { }
 * })
 *
 * client.stopTracking()
 * ```
 *
 * One instance per process is the intended usage — hold it in your `Application`. Everything
 * asynchronous is a coroutine and nothing runs on the main thread except the delivery of
 * [GeofenceEventCallback] and [ErrorListener] calls, which is deliberate so they are safe for
 * UI work.
 *
 * Runtime location permissions are **not** requested by the SDK. [startTracking] throws
 * [MissingLocationPermissionException] if they are missing, rather than starting a tracker that
 * would never report anything.
 */
class GeoTagClient private constructor(
    context: Context,
    private val config: Config
) {

    private val appContext: Context = context.applicationContext

    /**
     * The bearer token sent on every request. Held in memory only — the SDK never writes it to
     * disk, logs it, or hardcodes a default.
     */
    @Volatile
    private var apiKey: String = config.apiKey

    @Volatile
    private var eventCallback: GeofenceEventCallback? = null

    @Volatile
    private var errorListener: ErrorListener? = null

    private val lifecycleLock = Any()

    /** Outlives [stopTracking] so a final flush can still complete. */
    private val ioScope = CoroutineScope(
        SupervisorJob() + Dispatchers.IO + CoroutineName("geotag-io")
    )

    private val mainScope = CoroutineScope(
        SupervisorJob() + Dispatchers.Main.immediate + CoroutineName("geotag-callbacks")
    )

    /** Non-null exactly while tracking is active. */
    private var trackingScope: CoroutineScope? = null

    /** Location plumbing, exposed so apps can pre-flight permissions before calling start. */
    val locationManager: LocationManager = LocationManager(appContext)

    /** Geofence zone registration/deregistration against `/geofences`. */
    val geofences: GeofenceClient = GeofenceClient(config.apiUrl) { apiKey }

    private val batchQueue: BatchQueue = BatchQueue(
        context = appContext,
        apiUrl = config.apiUrl,
        apiKeyProvider = { apiKey },
        batchIntervalSeconds = config.batchIntervalSeconds,
        maxQueuedPings = config.maxQueuedPings,
        onEvents = { events -> dispatchEvents(events) },
        onError = { error -> dispatchError(error) }
    )

    /** Resolved off the main thread; SharedPreferences access is disk I/O. */
    private val deviceIdDeferred: Deferred<String> = ioScope.async { resolveDeviceId() }

    // ------------------------------------------------------------------ public API

    /**
     * Starts location tracking and the batch flush loop.
     *
     * @throws MissingLocationPermissionException if neither `ACCESS_FINE_LOCATION` nor
     *   `ACCESS_COARSE_LOCATION` has been granted. Requesting them is the app's job.
     */
    @RequiresPermission(
        anyOf = [Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION]
    )
    fun startTracking() {
        locationManager.requireLocationPermission()

        val scope = synchronized(lifecycleLock) {
            if (trackingScope != null) {
                Log.w(TAG, "startTracking() ignored: already tracking")
                return
            }
            CoroutineScope(
                SupervisorJob() + Dispatchers.Default + CoroutineName("geotag-tracking")
            ).also { trackingScope = it }
        }

        batchQueue.start(scope)

        scope.launch {
            val id = deviceId()

            // Ping once immediately from the cached fix so the dashboard sees the device now
            // rather than one interval from now.
            locationManager.lastKnownLocation()?.let { cached ->
                buildPing(cached, id)?.let { batchQueue.enqueue(it) }
            }

            locationManager
                .locationUpdates(
                    intervalSeconds = config.locationIntervalSeconds,
                    minIntervalSeconds = config.minLocationIntervalSeconds
                )
                .catch { throwable ->
                    dispatchError(
                        throwable as? GeoTagException
                            ?: GeoTagException("Location updates failed", throwable)
                    )
                }
                .collect { location ->
                    val ping = buildPing(location, id) ?: return@collect
                    batchQueue.enqueue(ping)
                }
        }

        Log.i(
            TAG,
            "Tracking started (location every ${config.locationIntervalSeconds}s, " +
                "flush every ${config.batchIntervalSeconds}s, queue cap ${config.maxQueuedPings})"
        )
    }

    /**
     * Stops location tracking and the flush loop, then attempts one final flush in the
     * background. Returns immediately; it never blocks the caller.
     *
     * Anything still buffered stays on disk and is sent after the next [startTracking].
     */
    fun stopTracking() {
        val scope = synchronized(lifecycleLock) {
            trackingScope.also { trackingScope = null }
        }
        if (scope == null) {
            Log.w(TAG, "stopTracking() ignored: not tracking")
            return
        }

        batchQueue.stop()
        scope.cancel()

        ioScope.launch {
            try {
                batchQueue.flush()
            } catch (e: Exception) {
                Log.w(TAG, "Final flush after stopTracking() failed; pings remain queued", e)
            }
        }

        Log.i(TAG, "Tracking stopped; a final flush was scheduled")
    }

    /**
     * Registers the geofence event listener. Pass null to clear it.
     *
     * Callbacks are delivered on the main thread. Safe to call before or after
     * [startTracking].
     */
    fun setEventCallback(callback: GeofenceEventCallback?) {
        eventCallback = callback
    }

    /**
     * Registers a listener for transport, auth and validation problems. Optional but strongly
     * recommended: without it, failures are only visible in logcat under the tag `GeoTagSDK`.
     *
     * Delivered on the main thread.
     */
    fun setErrorListener(listener: ErrorListener?) {
        errorListener = listener
    }

    /**
     * Replaces the bearer token. Cognito JWTs expire; when the SDK reports a 401/403 through
     * [setErrorListener], refresh the token and pass it here. Queued pings are retained and
     * retried with the new token.
     */
    fun updateApiKey(apiKey: String) {
        require(apiKey.isNotBlank()) { "apiKey must not be blank" }
        this.apiKey = apiKey
        Log.i(TAG, "API key updated")
    }

    /** Flushes the queue now instead of waiting for the next interval. */
    suspend fun flush() {
        batchQueue.flush()
    }

    /** How many pings are waiting to be sent (in memory plus restored from disk). */
    suspend fun queuedPingCount(): Int = batchQueue.size()

    /**
     * The device identifier this client reports under — the value sent as `device_id`.
     *
     * Either the id supplied to [Builder.deviceId] or a random one generated on first run and
     * persisted in the SDK's own private `SharedPreferences`. Suspends because resolving it
     * touches disk.
     */
    suspend fun deviceId(): String = deviceIdDeferred.await()

    /**
     * Releases everything. Call from `Application`/process teardown if you ever need to drop
     * the client; the instance is unusable afterwards.
     *
     * This cancels the final flush that [stopTracking] schedules, so anything still buffered is
     * left on disk for the next process. To send it first, `flush()` and then `shutdown()`.
     */
    fun shutdown() {
        stopTracking()
        mainScope.cancel()
        ioScope.cancel()
    }

    // ------------------------------------------------------------------ internals

    private fun buildPing(location: Location, deviceId: String): LocationPing? {
        val lat = location.latitude
        val lng = location.longitude
        if (!lat.isFinite() || !lng.isFinite() || lat < -90.0 || lat > 90.0 ||
            lng < -180.0 || lng > 180.0
        ) {
            Log.w(TAG, "Discarding a fix with an out-of-range coordinate: lat=$lat lng=$lng")
            return null
        }

        // location.time is UTC milliseconds since the epoch; the API wants unix SECONDS.
        val timestampSeconds =
            if (location.time > 0L) location.time / 1000L else System.currentTimeMillis() / 1000L

        return LocationPing(
            deviceId = deviceId,
            lat = lat,
            lng = lng,
            accuracy = if (location.hasAccuracy()) location.accuracy.toDouble().finiteOrNull() else null,
            speed = if (location.hasSpeed()) location.speed.toDouble().finiteOrNull() else null,
            bearing = if (location.hasBearing()) location.bearing.toDouble().finiteOrNull() else null,
            battery = batteryPercent(),
            timestamp = timestampSeconds
        )
    }

    private fun Double.finiteOrNull(): Double? = if (isFinite()) this else null

    /** Battery level 0-100, or null when the platform will not say. */
    private fun batteryPercent(): Int? {
        val manager = appContext.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
            ?: return null
        val level = manager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        return if (level in 0..100) level else null
    }

    private fun resolveDeviceId(): String {
        config.deviceId?.let { return it }
        return try {
            val prefs = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            prefs.getString(PREF_KEY_DEVICE_ID, null) ?: run {
                val generated = "android_" + UUID.randomUUID().toString().replace("-", "")
                prefs.edit().putString(PREF_KEY_DEVICE_ID, generated).apply()
                Log.i(TAG, "Generated a new device id: $generated")
                generated
            }
        } catch (e: Exception) {
            val fallback = "android_" + UUID.randomUUID().toString().replace("-", "")
            Log.e(
                TAG,
                "Could not persist a device id; using a process-scoped one ($fallback). " +
                    "Positions from this install will not be attributable across restarts.",
                e
            )
            fallback
        }
    }

    private fun dispatchEvents(events: List<GeofenceEvent>) {
        if (events.isEmpty()) return
        val callback = eventCallback
        if (callback == null) {
            Log.d(TAG, "Discarding ${events.size} geofence event(s): no callback registered")
            return
        }
        mainScope.launch {
            for (event in events) {
                try {
                    when (event.eventType) {
                        EVENT_ENTER -> callback.onEnter(event.deviceId, event.geofenceId)
                        EVENT_EXIT -> callback.onExit(event.deviceId, event.geofenceId)
                        EVENT_DWELL -> {
                            if (event.dwellMs == null) {
                                Log.w(
                                    TAG,
                                    "Dwell event for geofence ${event.geofenceId} carried no " +
                                        "dwell_ms; reporting 0"
                                )
                            }
                            callback.onDwell(event.deviceId, event.geofenceId, event.dwellMs ?: 0L)
                        }

                        else -> Log.w(TAG, "Ignoring unknown event_type '${event.eventType}'")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "GeofenceEventCallback threw for ${event.eventType}", e)
                }
            }
        }
    }

    private fun dispatchError(error: GeoTagException) {
        val listener = errorListener ?: return
        mainScope.launch {
            try {
                listener.onError(error)
            } catch (e: Exception) {
                Log.e(TAG, "ErrorListener threw", e)
            }
        }
    }

    // ------------------------------------------------------------------ configuration

    internal data class Config(
        val apiUrl: String,
        val apiKey: String,
        val batchIntervalSeconds: Int,
        val locationIntervalSeconds: Int,
        val minLocationIntervalSeconds: Int,
        val maxQueuedPings: Int,
        val deviceId: String?
    )

    /**
     * Builds a [GeoTagClient].
     *
     * [apiUrl], [apiKey] and [batchIntervalSeconds] are the three settings named in CLAUDE.md;
     * the rest have defaults.
     */
    class Builder(context: Context) {

        private val appContext: Context = context.applicationContext

        private var apiUrl: String? = null
        private var apiKey: String? = null
        private var batchIntervalSeconds: Int = GeoTagClient.DEFAULT_BATCH_INTERVAL_SECONDS
        private var locationIntervalSeconds: Int = GeoTagClient.DEFAULT_LOCATION_INTERVAL_SECONDS
        private var minLocationIntervalSeconds: Int? = null
        private var maxQueuedPings: Int = GeoTagClient.DEFAULT_MAX_QUEUED_PINGS
        private var deviceId: String? = null

        /** Base URL of the API, e.g. `https://xxxx.execute-api.eu-west-1.amazonaws.com/prod`. */
        fun apiUrl(apiUrl: String): Builder = apply { this.apiUrl = apiUrl }

        /**
         * The Cognito JWT sent as `Authorization: Bearer <apiKey>` (SPEC-DECISIONS D6). The
         * tenant is read by the backend from the token's `custom:tenant_id` claim, so it is
         * never sent as a parameter.
         *
         * Obtain this from your own sign-in flow at runtime. Do not compile a token into the
         * app.
         */
        fun apiKey(apiKey: String): Builder = apply { this.apiKey = apiKey }

        /** How often the buffered pings are POSTed to `/positions`. Must be 1..3600. */
        fun batchIntervalSeconds(seconds: Int): Builder = apply { this.batchIntervalSeconds = seconds }

        /** How often a position fix is requested from Play services. Defaults to 10s. */
        fun locationIntervalSeconds(seconds: Int): Builder =
            apply { this.locationIntervalSeconds = seconds }

        /** Fastest fix the SDK will accept. Defaults to half of [locationIntervalSeconds]. */
        fun minLocationIntervalSeconds(seconds: Int): Builder =
            apply { this.minLocationIntervalSeconds = seconds }

        /**
         * Upper bound on buffered pings. When exceeded the oldest are dropped, so this caps how
         * much offline history is kept: at one fix per 10s, the default 2000 covers about 5.5
         * hours offline.
         */
        fun maxQueuedPings(max: Int): Builder = apply { this.maxQueuedPings = max }

        /**
         * Overrides the reported `device_id`. Omit it and the SDK generates a random id on
         * first run and persists it in its own private `SharedPreferences`.
         */
        fun deviceId(deviceId: String): Builder = apply { this.deviceId = deviceId }

        fun build(): GeoTagClient {
            val url = requireNotNull(apiUrl) { "apiUrl(...) is required" }.trim().trimEnd('/')
            require(url.startsWith("https://") || url.startsWith("http://")) {
                "apiUrl must start with http:// or https:// (got '$url')"
            }
            if (url.startsWith("http://")) {
                Log.w(
                    TAG,
                    "apiUrl uses cleartext http://. Position data is personal data — use https " +
                        "outside local development."
                )
            }

            val key = requireNotNull(apiKey) { "apiKey(...) is required" }
            require(key.isNotBlank()) { "apiKey must not be blank" }

            require(batchIntervalSeconds in 1..3600) {
                "batchIntervalSeconds must be 1..3600 (got $batchIntervalSeconds)"
            }
            require(locationIntervalSeconds in 1..3600) {
                "locationIntervalSeconds must be 1..3600 (got $locationIntervalSeconds)"
            }
            require(maxQueuedPings in 1..1_000_000) {
                "maxQueuedPings must be 1..1000000 (got $maxQueuedPings)"
            }

            val minInterval = minLocationIntervalSeconds
                ?: (locationIntervalSeconds / 2).coerceAtLeast(1)
            require(minInterval in 1..locationIntervalSeconds) {
                "minLocationIntervalSeconds must be 1..$locationIntervalSeconds (got $minInterval)"
            }

            deviceId?.let { require(it.isNotBlank()) { "deviceId must not be blank" } }

            return GeoTagClient(
                context = appContext,
                config = Config(
                    apiUrl = url,
                    apiKey = key,
                    batchIntervalSeconds = batchIntervalSeconds,
                    locationIntervalSeconds = locationIntervalSeconds,
                    minLocationIntervalSeconds = minInterval,
                    maxQueuedPings = maxQueuedPings,
                    deviceId = deviceId
                )
            )
        }
    }

    companion object {
        /** SDK version, sent as part of the `User-Agent`. */
        const val VERSION: String = "0.1.0"

        const val DEFAULT_BATCH_INTERVAL_SECONDS: Int = 10
        const val DEFAULT_LOCATION_INTERVAL_SECONDS: Int = 10
        const val DEFAULT_MAX_QUEUED_PINGS: Int = 2000

        private const val PREFS_NAME = "com.geotag.sdk.prefs"
        private const val PREF_KEY_DEVICE_ID = "device_id"

        private const val EVENT_ENTER = "enter"
        private const val EVENT_EXIT = "exit"
        private const val EVENT_DWELL = "dwell"
    }
}

/** Notified of SDK-level failures. Invoked on the main thread. */
fun interface ErrorListener {
    fun onError(error: GeoTagException)
}

/** Base type for everything the SDK throws or reports. */
open class GeoTagException(
    message: String,
    cause: Throwable? = null
) : Exception(message, cause)

/**
 * No location permission has been granted. The SDK deliberately does not degrade quietly:
 * without this exception an app would appear to be tracking while reporting nothing.
 */
class MissingLocationPermissionException(
    message: String
) : GeoTagException(message)

/** Transport failure — offline, DNS, TLS, timeout. Pings are retained and retried. */
class GeoTagNetworkException(
    message: String,
    cause: Throwable? = null
) : GeoTagException(message, cause)

/**
 * The API answered with a non-2xx status. [errorCode] is the machine code from the frozen
 * error envelope (`unauthorized`, `forbidden`, `not_found`, `validation_error`,
 * `internal_error`) when the body carried one.
 */
class GeoTagApiException(
    val httpStatus: Int,
    val errorCode: String?,
    message: String
) : GeoTagException(message)
