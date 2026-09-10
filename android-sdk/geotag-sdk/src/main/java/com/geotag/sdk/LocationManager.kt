package com.geotag.sdk

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.os.Looper
import android.util.Log
import androidx.annotation.RequiresPermission
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationAvailability
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.buffer
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Thin coroutine wrapper around [FusedLocationProviderClient].
 *
 * Location updates are exposed as a cold [Flow]: `requestLocationUpdates` is issued when the
 * flow is collected and `removeLocationUpdates` when collection stops, so there is no way to
 * leak a registration.
 *
 * **Permissions are the app's job.** This class never asks for anything — it only checks, and
 * it fails loudly (an exception plus an `ERROR` log) when the app has not been granted a
 * location permission. It will never silently sit there reporting nothing.
 *
 * The class is named `LocationManager` because CLAUDE.md's project structure names the file
 * `LocationManager.kt`. It is unrelated to `android.location.LocationManager`.
 */
class LocationManager internal constructor(context: Context) {

    private val appContext: Context = context.applicationContext

    private val fusedClient: FusedLocationProviderClient by lazy {
        LocationServices.getFusedLocationProviderClient(appContext)
    }

    /** True when either fine or coarse location has been granted. */
    fun hasLocationPermission(): Boolean =
        isGranted(Manifest.permission.ACCESS_FINE_LOCATION) ||
            isGranted(Manifest.permission.ACCESS_COARSE_LOCATION)

    /** True when `ACCESS_FINE_LOCATION` specifically has been granted. */
    fun hasFineLocationPermission(): Boolean = isGranted(Manifest.permission.ACCESS_FINE_LOCATION)

    /**
     * Throws [MissingLocationPermissionException] if no location permission has been granted.
     *
     * This is the "fail loudly" path required by the coding standards: an app that forgets to
     * request permissions gets an exception and an `ERROR` log line, not silence.
     */
    fun requireLocationPermission() {
        if (!hasLocationPermission()) {
            val message = "GeoTag SDK cannot report positions: neither " +
                "ACCESS_FINE_LOCATION nor ACCESS_COARSE_LOCATION has been granted to " +
                "${appContext.packageName}. Requesting runtime permissions is the app's " +
                "responsibility — call ActivityCompat.requestPermissions(...) and only then " +
                "GeoTagClient.startTracking()."
            Log.e(TAG, message)
            throw MissingLocationPermissionException(message)
        }
        if (!hasFineLocationPermission()) {
            Log.w(
                TAG,
                "Only ACCESS_COARSE_LOCATION is granted. Positions will be accurate to roughly " +
                    "1-3 km, which is usually too coarse for geofencing."
            )
        }
    }

    /**
     * A cold flow of device positions.
     *
     * @param intervalSeconds desired interval between updates.
     * @param minIntervalSeconds fastest interval the SDK will accept an update at; defaults to
     *   half of [intervalSeconds].
     * @param priority one of the `com.google.android.gms.location.Priority` constants.
     *
     * The flow fails with [MissingLocationPermissionException] when collected without
     * permission, and with [GeoTagException] if Play services refuses the request.
     */
    @RequiresPermission(
        anyOf = [Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION]
    )
    fun locationUpdates(
        intervalSeconds: Int,
        minIntervalSeconds: Int = (intervalSeconds / 2).coerceAtLeast(1),
        priority: Int = Priority.PRIORITY_HIGH_ACCURACY
    ): Flow<Location> = callbackFlow {
        requireLocationPermission()

        val request = LocationRequest.Builder(priority, intervalSeconds * 1000L)
            .setMinUpdateIntervalMillis(minIntervalSeconds * 1000L)
            .setWaitForAccurateLocation(false)
            .build()

        val callback = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                for (location in result.locations) {
                    trySend(location)
                }
            }

            override fun onLocationAvailability(availability: LocationAvailability) {
                if (!availability.isLocationAvailable) {
                    Log.w(TAG, "Location is temporarily unavailable (no usable provider/fix)")
                }
            }
        }

        try {
            fusedClient.requestLocationUpdates(request, callback, Looper.getMainLooper())
                .addOnFailureListener { error ->
                    close(GeoTagException("Play services refused requestLocationUpdates", error))
                }
        } catch (e: SecurityException) {
            close(
                MissingLocationPermissionException(
                    "Play services rejected requestLocationUpdates for lack of a location " +
                        "permission: ${e.message}"
                )
            )
        }

        awaitClose { fusedClient.removeLocationUpdates(callback) }
    }.buffer(capacity = LOCATION_BUFFER_CAPACITY, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    /**
     * The last position Play services already had, if any — used to send one ping immediately
     * instead of waiting a whole interval for the first fix. Returns null when there is no
     * cached fix or the lookup fails.
     */
    @RequiresPermission(
        anyOf = [Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION]
    )
    suspend fun lastKnownLocation(): Location? {
        requireLocationPermission()
        return suspendCancellableCoroutine { continuation ->
            try {
                fusedClient.lastLocation
                    .addOnSuccessListener { location ->
                        if (continuation.isActive) continuation.resume(location)
                    }
                    .addOnFailureListener { error ->
                        Log.w(TAG, "lastLocation lookup failed", error)
                        if (continuation.isActive) continuation.resume(null)
                    }
            } catch (e: SecurityException) {
                Log.w(TAG, "lastLocation rejected for lack of permission", e)
                if (continuation.isActive) continuation.resume(null)
            }
        }
    }

    private fun isGranted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(appContext, permission) == PackageManager.PERMISSION_GRANTED

    private companion object {
        /**
         * Fixes are produced faster than a slow network flush can drain them only in pathological
         * cases; if that happens, keep the newest.
         */
        const val LOCATION_BUFFER_CAPACITY = 64
    }
}
