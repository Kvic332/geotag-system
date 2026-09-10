package com.geotag.sdk

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL
import kotlin.math.min
import kotlin.random.Random

/**
 * A single device position ping, in exactly the shape `POST /positions` expects
 * (API-CONTRACT.md).
 *
 * [timestamp] is **unix seconds**, per CLAUDE.md and API-CONTRACT.md — not milliseconds.
 * Optional fields are omitted from the JSON entirely when null rather than sent as `null`.
 */
data class LocationPing(
    val deviceId: String,
    val lat: Double,
    val lng: Double,
    val accuracy: Double? = null,
    val speed: Double? = null,
    val bearing: Double? = null,
    val battery: Int? = null,
    val timestamp: Long
) {

    internal fun toJson(): JSONObject = JSONObject().apply {
        put("device_id", deviceId)
        put("lat", lat)
        put("lng", lng)
        if (accuracy != null) put("accuracy", accuracy)
        if (speed != null) put("speed", speed)
        if (bearing != null) put("bearing", bearing)
        if (battery != null) put("battery", battery)
        put("timestamp", timestamp)
    }

    internal companion object {

        /** Parses a ping back off disk. The persisted form is byte-for-byte the wire form. */
        @Throws(JSONException::class)
        fun fromJson(json: JSONObject): LocationPing = LocationPing(
            deviceId = json.getString("device_id"),
            lat = json.getDouble("lat"),
            lng = json.getDouble("lng"),
            accuracy = if (json.isNull("accuracy")) null else json.getDouble("accuracy"),
            speed = if (json.isNull("speed")) null else json.getDouble("speed"),
            bearing = if (json.isNull("bearing")) null else json.getDouble("bearing"),
            battery = if (json.isNull("battery")) null else json.getInt("battery"),
            timestamp = json.getLong("timestamp")
        )
    }
}

/**
 * Buffers position pings and flushes them to `POST /positions` every N seconds in the batch
 * form defined by SPEC-DECISIONS D4.
 *
 * Guarantees:
 * - **Nothing runs on the main thread.** Every disk and network operation is wrapped in
 *   `withContext(Dispatchers.IO)`; the flush loop runs on [Dispatchers.Default].
 * - **Bounded.** At most [maxQueuedPings] pings are retained; on overflow the *oldest* are
 *   dropped (live tracking cares about recent positions) and the drop is logged.
 * - **Durable.** The queue is mirrored to a JSON-lines file under `filesDir/geotag/`, so pings
 *   buffered while offline survive process death.
 * - **No infinite retry of a poison ping.** A ping the server reports in `rejected` is dropped,
 *   never re-sent — it failed validation and always will. A request that fails at the envelope
 *   level (`400`) halves the batch size until the offending ping is isolated and dropped alone.
 * - **No loss of good pings.** Pings are only removed from the queue after the server has
 *   actually answered; transport failures, 401/403, 429 and 5xx all keep the pings and retry
 *   with exponential backoff.
 */
class BatchQueue internal constructor(
    context: Context,
    private val apiUrl: String,
    private val apiKeyProvider: () -> String,
    private val batchIntervalSeconds: Int,
    private val maxQueuedPings: Int,
    private val onEvents: (List<GeofenceEvent>) -> Unit,
    private val onError: (GeoTagException) -> Unit
) {

    private val appContext: Context = context.applicationContext
    private val queueDir: File = File(appContext.filesDir, QUEUE_DIR_NAME)
    private val queueFile: File = File(queueDir, QUEUE_FILE_NAME)
    private val tempFile: File = File(queueDir, "$QUEUE_FILE_NAME.tmp")

    /** Guards [pending] and the on-disk mirror. */
    private val stateMutex = Mutex()

    /** Serialises flushes so "drop the first N" after a send is always the batch that was sent. */
    private val flushMutex = Mutex()

    private val pending = ArrayDeque<LocationPing>()
    private var loadedFromDisk = false

    /** Shrinks on a 400 to bisect a poison ping; resets to [MAX_PINGS_PER_REQUEST] on success. */
    private var batchLimit = MAX_PINGS_PER_REQUEST

    private var loopJob: Job? = null

    // ---------------------------------------------------------------- public-ish surface

    /** Adds a ping to the queue and mirrors it to disk. Suspends; never blocks the caller. */
    suspend fun enqueue(ping: LocationPing) {
        stateMutex.withLock {
            ensureLoadedLocked()
            pending.addLast(ping)
            if (pending.size > maxQueuedPings) {
                val overflow = pending.size - maxQueuedPings
                repeat(overflow) { pending.removeFirst() }
                Log.w(
                    TAG,
                    "Queue full ($maxQueuedPings); dropped $overflow oldest ping(s). " +
                        "Increase GeoTagClient.Builder.maxQueuedPings or shorten the batch interval."
                )
                rewriteMirrorLocked()
            } else {
                appendToMirrorLocked(ping)
            }
        }
    }

    /** Number of pings currently buffered (persisted + in memory). */
    suspend fun size(): Int = stateMutex.withLock {
        ensureLoadedLocked()
        pending.size
    }

    /**
     * Sends at most [batchLimit] (<= 500) buffered pings.
     *
     * @return true when the queue made progress (or was empty) — i.e. the backoff may reset.
     */
    suspend fun flush(): Boolean = flushMutex.withLock {
        val batch = stateMutex.withLock {
            ensureLoadedLocked()
            pending.take(min(batchLimit, MAX_PINGS_PER_REQUEST))
        }
        if (batch.isEmpty()) return@withLock true

        // Batch form from SPEC-DECISIONS D4: {"pings": [ <ping>, ... ]}, at most 500.
        val pings = JSONArray()
        for (ping in batch) pings.put(ping.toJson())
        val payload = JSONObject().put("pings", pings).toString()

        val response = try {
            GeoTagHttp.request(
                url = "$apiUrl/positions",
                method = "POST",
                bearerToken = apiKeyProvider(),
                body = payload
            )
        } catch (e: IOException) {
            // Offline, DNS failure, timeout: keep every ping, retry with backoff.
            report(GeoTagNetworkException("Failed to POST ${batch.size} ping(s) to $apiUrl/positions", e))
            return@withLock false
        }

        when {
            response.code in 200..299 -> {
                handleSuccess(batch, response.body)
                true
            }

            response.code == 401 || response.code == 403 -> {
                // The Cognito JWT is missing, invalid or expired. Keep the pings — they are
                // still good — but say so loudly every time so the app can refresh the token
                // via GeoTagClient.updateApiKey().
                report(
                    GeoTagApiException(
                        response.code,
                        errorCodeOf(response.body),
                        "Rejected by $apiUrl/positions (HTTP ${response.code}). The API key / " +
                            "Cognito JWT is missing, invalid or expired — call " +
                            "GeoTagClient.updateApiKey() with a fresh token. " +
                            "${batch.size} ping(s) kept for retry."
                    )
                )
                false
            }

            response.code == 400 || response.code == 413 || response.code == 422 -> {
                handlePoisonBatch(batch, response)
                false
            }

            else -> {
                // 408, 429, 5xx, anything else: transient, keep the pings.
                report(
                    GeoTagApiException(
                        response.code,
                        errorCodeOf(response.body),
                        "Transient failure from $apiUrl/positions (HTTP ${response.code}); " +
                            "${batch.size} ping(s) kept for retry."
                    )
                )
                false
            }
        }
    }

    /** Starts the "flush every N seconds" loop inside [scope]. Idempotent. */
    fun start(scope: CoroutineScope) {
        if (loopJob?.isActive == true) return
        loopJob = scope.launch(Dispatchers.Default) {
            var consecutiveFailures = 0
            while (isActive) {
                delay(nextDelayMillis(consecutiveFailures))
                val progressed = try {
                    flush()
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    // The loop must outlive any single bad flush, so this catch is broad on
                    // purpose. Cancellation is rethrown above so structured concurrency still
                    // works.
                    report(GeoTagException("Unexpected error while flushing the position queue", e))
                    false
                }
                consecutiveFailures = if (progressed) 0 else min(consecutiveFailures + 1, MAX_BACKOFF_STEPS)
            }
        }
    }

    /** Stops the flush loop. Buffered pings stay on disk and are picked up on the next start. */
    fun stop() {
        loopJob?.cancel()
        loopJob = null
    }

    // ---------------------------------------------------------------- response handling

    private suspend fun handleSuccess(batch: List<LocationPing>, body: String) {
        batchLimit = MAX_PINGS_PER_REQUEST

        // The server has taken responsibility for every ping in this batch — accepted ones are
        // stored, rejected ones are permanently invalid. Either way they leave the queue, which
        // is what stops a bad ping being retried forever.
        dropSent(batch.size)

        val parsed = try {
            JSONObject(body)
        } catch (e: JSONException) {
            report(GeoTagException("POST /positions returned a non-JSON body", e))
            return
        }

        val accepted = parsed.optInt("accepted", batch.size)
        val rejected = parsed.optJSONArray("rejected")
        if (rejected != null && rejected.length() > 0) {
            for (i in 0 until rejected.length()) {
                val entry = rejected.optJSONObject(i) ?: continue
                val index = entry.optInt("index", -1)
                val reason = entry.optString("error")
                val ping = batch.getOrNull(index)
                Log.w(
                    TAG,
                    "Server rejected ping index=$index (device=${ping?.deviceId}, " +
                        "ts=${ping?.timestamp}): $reason — dropped, will not be retried."
                )
            }
            report(
                GeoTagException(
                    "POST /positions accepted $accepted of ${batch.size} ping(s); " +
                        "${rejected.length()} permanently rejected and dropped."
                )
            )
        }

        val events = parsed.optJSONArray("events")
        if (events != null && events.length() > 0) {
            val parsedEvents = ArrayList<GeofenceEvent>(events.length())
            for (i in 0 until events.length()) {
                val obj = events.optJSONObject(i) ?: continue
                val event = obj.toGeofenceEventOrNull(fallbackDeviceId = batch.firstOrNull()?.deviceId)
                if (event != null) parsedEvents.add(event)
            }
            if (parsedEvents.isNotEmpty()) onEvents(parsedEvents)
        }
    }

    /**
     * A `400`/`413`/`422` means the *request* was refused, not individual pings (D4 says a bad
     * ping inside a batch must come back in `rejected`, not fail the batch). Bisect: halve the
     * batch until a single ping is refused on its own, then drop just that one. Bounded by
     * log2(500) ≈ 9 attempts, so nothing retries forever.
     */
    private suspend fun handlePoisonBatch(batch: List<LocationPing>, response: HttpResponse) {
        if (batch.size > 1) {
            batchLimit = batch.size / 2
            report(
                GeoTagApiException(
                    response.code,
                    errorCodeOf(response.body),
                    "POST /positions refused a batch of ${batch.size} (HTTP ${response.code}); " +
                        "halving to $batchLimit to isolate the offending ping."
                )
            )
        } else {
            val bad = batch.first()
            dropSent(1)
            batchLimit = MAX_PINGS_PER_REQUEST
            report(
                GeoTagApiException(
                    response.code,
                    errorCodeOf(response.body),
                    "POST /positions permanently refused a single ping " +
                        "(device=${bad.deviceId}, ts=${bad.timestamp}, HTTP ${response.code}); " +
                        "dropping it."
                )
            )
        }
    }

    private suspend fun dropSent(count: Int) {
        stateMutex.withLock {
            repeat(min(count, pending.size)) { pending.removeFirst() }
            rewriteMirrorLocked()
        }
    }

    private fun errorCodeOf(body: String): String? = try {
        JSONObject(body).optJSONObject("error")?.optString("code")?.takeIf { it.isNotEmpty() }
    } catch (e: JSONException) {
        null
    }

    private fun report(error: GeoTagException) {
        Log.e(TAG, error.message ?: error.toString(), error.cause)
        onError(error)
    }

    private fun nextDelayMillis(consecutiveFailures: Int): Long {
        val base = batchIntervalSeconds * 1000L
        if (consecutiveFailures == 0) return base
        val backoff = min(base shl consecutiveFailures, MAX_BACKOFF_MILLIS)
        // Jitter so a fleet of devices does not stampede a recovering backend.
        return backoff + Random.nextLong(0L, min(backoff / 4 + 1, 5_000L))
    }

    // ---------------------------------------------------------------- disk mirror
    // All of these must be called while holding [stateMutex].

    private suspend fun ensureLoadedLocked() {
        if (loadedFromDisk) return
        loadedFromDisk = true
        val restored = withContext(Dispatchers.IO) { readMirror() }
        if (restored.isEmpty()) return
        // Persisted pings are older than anything enqueued this process, so they go in front.
        for (ping in restored.asReversed()) pending.addFirst(ping)
        while (pending.size > maxQueuedPings) pending.removeFirst()
        Log.i(TAG, "Restored ${pending.size} buffered ping(s) from disk")
        rewriteMirrorLocked()
    }

    private suspend fun appendToMirrorLocked(ping: LocationPing) {
        val line = ping.toJson().toString() + "\n"
        withContext<Unit>(Dispatchers.IO) {
            try {
                if (!queueDir.exists()) queueDir.mkdirs()
                FileOutputStream(queueFile, true).use { out ->
                    out.write(line.toByteArray(Charsets.UTF_8))
                }
            } catch (e: IOException) {
                Log.w(TAG, "Could not persist ping; it is still buffered in memory", e)
            }
        }
    }

    private suspend fun rewriteMirrorLocked() {
        val snapshot = pending.toList()
        withContext<Unit>(Dispatchers.IO) {
            try {
                if (!queueDir.exists()) queueDir.mkdirs()
                if (snapshot.isEmpty()) {
                    queueFile.delete()
                } else {
                    FileOutputStream(tempFile).use { out ->
                        for (ping in snapshot) {
                            out.write((ping.toJson().toString() + "\n").toByteArray(Charsets.UTF_8))
                        }
                        out.fd.sync()
                    }
                    if (!tempFile.renameTo(queueFile)) {
                        queueFile.delete()
                        if (!tempFile.renameTo(queueFile)) {
                            Log.w(TAG, "Could not replace the persisted queue file")
                        }
                    }
                }
            } catch (e: IOException) {
                Log.w(TAG, "Could not rewrite the persisted queue file", e)
            }
        }
    }

    private fun readMirror(): List<LocationPing> {
        if (!queueFile.exists()) return emptyList()
        return try {
            queueFile.readLines().mapNotNull { line ->
                if (line.isBlank()) {
                    null
                } else {
                    try {
                        LocationPing.fromJson(JSONObject(line))
                    } catch (e: JSONException) {
                        Log.w(TAG, "Skipping corrupt persisted ping", e)
                        null
                    }
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "Could not read the persisted queue file", e)
            emptyList()
        }
    }

    internal companion object {
        /** Hard cap from SPEC-DECISIONS D4. */
        const val MAX_PINGS_PER_REQUEST = 500

        private const val QUEUE_DIR_NAME = "geotag"
        private const val QUEUE_FILE_NAME = "position-queue.jsonl"
        private const val MAX_BACKOFF_MILLIS = 5L * 60L * 1000L
        private const val MAX_BACKOFF_STEPS = 6
    }
}

// -------------------------------------------------------------------------- HTTP transport

/** Raw HTTP result. `body` is the response body, or the error body on a non-2xx. */
internal data class HttpResponse(val code: Int, val body: String)

/**
 * Minimal JSON-over-HTTPS transport built on [HttpURLConnection].
 *
 * Deliberately dependency-free: an SDK that drags OkHttp/Retrofit/Gson into every consumer app
 * is a version-conflict generator. Every call is confined to [Dispatchers.IO].
 *
 * Every request carries `Authorization: Bearer <jwt>` per SPEC-DECISIONS D6 / API-CONTRACT.md.
 * The token is supplied by the app; nothing is hardcoded here.
 */
internal object GeoTagHttp {

    private const val CONNECT_TIMEOUT_MILLIS = 15_000
    private const val READ_TIMEOUT_MILLIS = 30_000

    @Throws(IOException::class)
    suspend fun request(
        url: String,
        method: String,
        bearerToken: String,
        body: String? = null
    ): HttpResponse = withContext(Dispatchers.IO) {
        val parsed = try {
            URL(url)
        } catch (e: MalformedURLException) {
            throw IOException("Malformed GeoTag API URL: $url", e)
        }
        val connection = parsed.openConnection() as HttpURLConnection
        try {
            connection.requestMethod = method
            connection.connectTimeout = CONNECT_TIMEOUT_MILLIS
            connection.readTimeout = READ_TIMEOUT_MILLIS
            connection.useCaches = false
            connection.setRequestProperty("Authorization", "Bearer $bearerToken")
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("User-Agent", "geotag-android-sdk/${GeoTagClient.VERSION}")

            if (body != null) {
                val bytes = body.toByteArray(Charsets.UTF_8)
                connection.doOutput = true
                connection.setFixedLengthStreamingMode(bytes.size)
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(bytes) }
            }

            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            HttpResponse(code, text)
        } finally {
            connection.disconnect()
        }
    }
}

internal const val TAG = "GeoTagSDK"
