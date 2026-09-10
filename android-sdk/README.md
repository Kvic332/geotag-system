# GeoTag Android SDK (`com.geotag:geotag-sdk`)

> ## ⚠️ THIS MODULE HAS NEVER BEEN COMPILED
>
> **No JDK, no Gradle and no Android SDK were available on the machine this code was written
> on.** Nothing here has been built, run, linted, or tested — not once. It was written to
> CLAUDE.md, SPEC-DECISIONS.md and API-CONTRACT.md by careful reading only.
>
> Treat every file as an unverified first draft. Expect to fix compile errors, and expect the
> pinned dependency versions to need bumping. See **[Building it for the first
> time](#building-it-for-the-first-time)** below, and **[Pinned
> versions](#pinned-versions)** for how much to trust each version number.

A Kotlin library that turns an Android app into a tracked device: it reads fused location
fixes, buffers them, ships them to the GeoTag API in batches, and hands geofence
enter/exit/dwell events back to your app.

---

## Building it for the first time

1. **Install a JDK 17** (AGP 8.5 requires 17; 21 also works). Point `JAVA_HOME` at it.
2. **Install Android Studio** (Koala 2024.1.1 or newer pairs with AGP 8.5) and, through its SDK
   Manager, install:
   - Android SDK Platform **34** (`compileSdk 34`)
   - Android SDK Build-Tools 34.x
   - Google Play services (for `play-services-location`)
3. **Create `android-sdk/local.properties`** containing the SDK path, e.g.
   `sdk.dir=C\:\\Users\\<you>\\AppData\\Local\\Android\\Sdk`.
   It is git-ignored on purpose — never commit it.
4. **Generate the Gradle wrapper.** Only `gradle/wrapper/gradle-wrapper.properties` is checked
   in; the `gradlew` scripts and `gradle-wrapper.jar` are binaries that could not be produced
   here. Either open `android-sdk/` in Android Studio (it offers to create them) or run
   `gradle wrapper --gradle-version 8.9` with a system Gradle.
5. **Open `android-sdk/` as the Gradle project root** (not the repository root — the repo has no
   top-level Gradle build).
6. **Build:** `./gradlew :geotag-sdk:assembleRelease`
7. **Expect failures on this first build** and work through them. The most likely causes, in
   order: a dependency version that no longer resolves, an `org.json`/coroutines API detail, and
   the Kotlin/AGP compatibility pair.
8. There are **no tests in this module**. Instrumented tests are the obvious next task: the
   batch/rejection/persistence logic in `BatchQueue.kt` is the part most worth covering, and it
   is testable against a `MockWebServer` without a real device.

To publish to a local Maven repo for a consuming app: `./gradlew :geotag-sdk:publishToMavenLocal`
(publishes `com.geotag:geotag-sdk:0.1.0`).

---

## Quick start

```kotlin
// Initialise once (e.g. in Application.onCreate)
val client = GeoTagClient.Builder(context)
    .apiUrl("https://your-api-gateway-url.amazonaws.com/prod")
    .apiKey("your-cognito-jwt")
    .batchIntervalSeconds(10)
    .build()

// Start tracking — throws MissingLocationPermissionException if permissions are missing
client.startTracking()

// Listen for geofence events (delivered on the main thread)
client.setEventCallback(object : GeofenceEventCallback {
    override fun onEnter(deviceId: String, geofenceId: String) { }
    override fun onExit(deviceId: String, geofenceId: String) { }
    override fun onDwell(deviceId: String, geofenceId: String, dwellMs: Long) { }
})

// Stop tracking (e.g. in onDestroy)
client.stopTracking()
```

That is the exact snippet from CLAUDE.md's "Android SDK usage" section, and it is the contract
for the public surface. Everything else below is additive.

### Gradle setup in the consuming app

```groovy
dependencies {
    implementation 'com.geotag:geotag-sdk:0.1.0'
}
```

`minSdk` must be **24 or higher**.

---

## Permissions

**Requesting runtime permissions is the app's job — the SDK never asks.** It only checks, and
if nothing has been granted it **fails loudly**: `startTracking()` throws
`MissingLocationPermissionException` and logs at `ERROR` under the tag `GeoTagSDK`. It will
never sit there quietly reporting nothing.

The library manifest merges these into your app:

| Permission | Why |
|---|---|
| `INTERNET` | POST pings to the API |
| `ACCESS_NETWORK_STATE` | connectivity checks |
| `ACCESS_FINE_LOCATION` | GPS-accuracy fixes (what geofencing needs) |
| `ACCESS_COARSE_LOCATION` | fallback; the SDK works but warns, since ~1–3 km accuracy is
usually too coarse for a geofence |

Typical flow:

```kotlin
if (!client.locationManager.hasLocationPermission()) {
    ActivityCompat.requestPermissions(
        activity,
        arrayOf(Manifest.permission.ACCESS_FINE_LOCATION),
        REQUEST_LOCATION
    )
} else {
    client.startTracking()
}
```

### Background tracking is NOT included

`ACCESS_BACKGROUND_LOCATION` is deliberately **not** declared: it is a Play-Store-reviewed
permission, and this SDK does not run a foreground service, so it could not be used correctly
anyway. Tracking stops being reliable as soon as your process is backgrounded. If you need
background tracking, declare the permission in your own app, host `GeoTagClient` inside your own
foreground service with the `location` service type, and keep the process alive yourself.

---

## Public API

### `GeoTagClient`

| Member | Notes |
|---|---|
| `GeoTagClient.Builder(context)` | `apiUrl`, `apiKey`, `batchIntervalSeconds` are the three documented settings |
| `Builder.locationIntervalSeconds(Int)` | how often a fix is requested. Default 10 |
| `Builder.minLocationIntervalSeconds(Int)` | fastest accepted fix. Defaults to half the above |
| `Builder.maxQueuedPings(Int)` | queue bound. Default 2000 |
| `Builder.deviceId(String)` | override the reported `device_id` |
| `startTracking()` | throws if permissions are missing |
| `stopTracking()` | returns immediately; schedules one last flush |
| `setEventCallback(GeofenceEventCallback?)` | main-thread delivery |
| `setErrorListener(ErrorListener?)` | main-thread delivery. **Register this** — it is how you learn about expired tokens |
| `updateApiKey(String)` | swap in a refreshed JWT without losing the queue |
| `suspend flush()` | send now |
| `suspend queuedPingCount()` | how many pings are waiting |
| `suspend deviceId()` | the id being reported |
| `shutdown()` | release everything |
| `locationManager` | permission checks, raw `Flow<Location>` |
| `geofences` | zone registration (see below) |

### `GeofenceEventCallback`

`onEnter(deviceId, geofenceId)`, `onExit(deviceId, geofenceId)`,
`onDwell(deviceId, geofenceId, dwellMs)`. All on the main thread; exceptions thrown from them
are caught and logged, never fatal.

Events are **produced by the backend**, not computed on the device — enter/exit diffing lives in
`event_processor` (SPEC-DECISIONS D1/D3). They arrive on the `POST /positions` response, so a
callback fires up to one batch interval after the ping that caused it.

### `GeofenceClient` — `client.geofences`

Wraps the `/geofences` REST routes; it does **not** use the OS `GeofencingClient`.

```kotlin
val zone = client.geofences.registerCircleZone(
    name = "Depot",
    center = LngLat(lng = 3.3792, lat = 6.5244),
    radiusMeters = 250.0,
    dwellThresholdSeconds = 300
)
client.geofences.listZones()
client.geofences.deregisterZone(zone.id)
```

`LngLat(lng, lat)` exists specifically so the GeoJSON `[lng, lat]` array order from
API-CONTRACT.md cannot be mixed up. `PUT /geofences/{id}` is not wrapped — add it if an app
needs to edit zones.

### Errors

`GeoTagException` is the base type: `MissingLocationPermissionException`,
`GeoTagNetworkException` (transport — pings are kept and retried), `GeoTagApiException`
(carries `httpStatus` and the `errorCode` from the frozen error envelope).

Cognito JWTs expire. When one does you get a `GeoTagApiException` with `httpStatus == 401`;
refresh the token and call `updateApiKey(...)`. Queued pings are retained and go out with the
new token.

---

## How the batch queue works

- Fixes become `LocationPing`s and go into an in-memory deque, mirrored line-by-line to
  `filesDir/geotag/position-queue.jsonl`. **Pings buffered offline survive process death.**
- Every `batchIntervalSeconds`, up to **500** pings (the D4 hard cap) are POSTed to
  `/positions` as `{"pings": [...]}` with `Authorization: Bearer <apiKey>` (D6).
- On a `2xx`, the whole sent batch leaves the queue. Anything the server lists in `rejected` is
  logged with its index and reason and **dropped** — it failed validation and always would, so
  it is never retried. `accepted` is logged.
- On a transport failure, `401`/`403`, `429` or `5xx`, **no ping is lost**: the batch stays
  queued and is retried with exponential backoff (capped at 5 minutes, jittered).
- On a `400`/`413`/`422` — which per D4 means the *request*, not one ping, was refused — the
  batch size is halved on each attempt until a single ping is refused alone, and only that one
  is dropped. Bounded by ~9 attempts, so nothing loops forever.
- The queue is bounded at `maxQueuedPings` (default 2000 ≈ 5.5 hours at one fix per 10s). On
  overflow the **oldest** pings are dropped and a warning is logged.
- Every file and network operation runs on `Dispatchers.IO`; the flush loop runs on
  `Dispatchers.Default`. **Nothing blocks the main thread.**

### Device identity

If you do not call `Builder.deviceId(...)`, the SDK generates `android_<uuid>` on first run and
stores it in its own private `SharedPreferences` (`com.geotag.sdk.prefs`). It is not derived
from any hardware identifier. `client.deviceId()` returns it.

---

## What the SDK sends and reads

Everything below is taken verbatim from API-CONTRACT.md.

**Sends** — `POST /positions`, body `{"pings": [...]}`, each ping:
`device_id`, `lat`, `lng`, `accuracy`, `speed`, `bearing`, `battery`, `timestamp`.
`timestamp` is **unix seconds** (`Location.time / 1000`). Optional fields are omitted rather
than sent as `null`. `lat`/`lng` are validated client-side and non-finite values are discarded
before they can be rejected.

**Reads** — `accepted`, `rejected[].index`, `rejected[].error`, and `events[]` with
`device_id`, `event_type`, `geofence_id`, `geofence_name`, `occurred_at` (and `dwell_ms` when
present).

**Geofences** — sends `name`, `type`, `coordinates` (`[[lng,lat],...]`), `center`
(`[lng,lat]`), `radius_m`, `dwell_threshold_seconds`; reads `id`, `name`, `shape_type`,
`coordinates`, `center`, `radius_m`, `dwell_threshold_seconds`, `created_at`, `device_count`.

**Errors** — `{"error": {"code": ..., "message": ...}}`.

ISO-8601 timestamps in responses (`created_at`, `occurred_at`) are kept as `String`; the SDK
does no date parsing and imposes no date library on you.

---

## Known contract gaps and assumptions

1. ~~`dwell_ms` is missing from the batch response.~~ **Fixed** — this was true when this
   file was first written, but API-CONTRACT.md's D7 addendum and the backend now include
   `dwell_ms` unconditionally on every event object, including the `events[]` array of the
   `POST /positions` batch response this SDK reads from. The parser's `?: 0L` fallback in
   `GeofenceClient.kt` is defensive only, not a workaround for a live gap.
2. **`device_id` on single-ping event objects.** The single-ping `201` response's `events[]`
   entries have no `device_id`. The SDK always uses the batch form, so this does not bite; the
   parser falls back to the batch's own device id anyway.
3. **No push channel.** Events reach the device only as a side effect of a ping being ingested.
   A device that has stopped moving will not learn about a `dwell` event until its next flush.
   That follows from the contract as written; a real product probably wants FCM here.
4. **`GeofenceClient` = REST, not OS geofencing.** CLAUDE.md's file list says "register /
   deregister zones" without saying against what. Since the backend is the geofencing engine
   (D1) and events return on the ingest response, this was read as the `/geofences` routes.
5. **Tenancy is invisible to the SDK.** Per D6 the tenant comes from the JWT's
   `custom:tenant_id` claim, so it is never sent. Getting the token is entirely the app's job.
6. **No `Retry-After` handling** on `429`; the SDK uses its own backoff.
7. **One client per process.** Two `GeoTagClient` instances in the same app would share the same
   queue file and corrupt each other's accounting.

---

## Pinned versions

Chosen to be real, mutually compatible releases. **They were not verified against Maven Central
or Google's Maven repo — this machine had no toolchain and no lookup was performed** — and the
knowledge behind them has a cutoff, so newer releases certainly exist and some of these may be
superseded.

| Dependency | Pinned | Confidence |
|---|---|---|
| Android Gradle Plugin | `8.5.2` | High — a real release; requires JDK 17 and Gradle ≥ 8.7 |
| Gradle | `8.9` | High |
| Kotlin | `2.0.21` | High. If the Kotlin plugin warns that AGP 8.5.2 is newer than it was tested against, that is a warning, not an error |
| `kotlinx-coroutines-android` | `1.8.1` | High |
| `play-services-location` | `21.3.0` | **Medium** — the least certain pin. `21.0.1` is a rock-solid fallback; the `LocationRequest.Builder` API this code uses exists in both |
| `androidx.core:core-ktx` | `1.13.1` | High — needs `compileSdk 34` |
| `androidx.annotation` | `1.8.0` | High |
| `compileSdk` / `minSdk` | `34` / `24` | Deliberately conservative |

Deliberately **not** used: OkHttp, Retrofit, Gson, Moshi, kotlinx-serialization. HTTP is
`java.net.HttpURLConnection` and JSON is the platform's `org.json`, so the SDK adds nothing to
your dependency graph that Android does not already ship. That is also one fewer set of version
numbers to be wrong about.

---

## Logging

Everything logs under the tag **`GeoTagSDK`**. Failures are logged at `ERROR` even when an
`ErrorListener` is registered. The API key is never logged.

```
adb logcat -s GeoTagSDK
```

## Security notes

- No secrets are hardcoded anywhere in this module. The bearer token is supplied by the app at
  runtime, held in memory only, and never written to disk or logged.
- The persisted queue holds position history in the app's private `filesDir`. It is not
  encrypted. If your threat model includes a rooted device, encrypt it or shorten
  `maxQueuedPings`.
- `Builder.build()` warns if `apiUrl` is cleartext `http://`. Use it for local development
  against `http://10.0.2.2:3000` only, and add a `network_security_config` in your app to permit
  cleartext for that host.
