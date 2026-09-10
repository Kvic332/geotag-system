# Consumer ProGuard/R8 rules for the GeoTag SDK.
#
# The SDK uses no reflection and no serialization library, so nothing needs to be kept for
# it to work. These rules only keep the public surface so that consumers who subclass or
# implement the callback from Java keep readable stack traces.

-keep public class com.geotag.sdk.GeoTagClient { public *; }
-keep public class com.geotag.sdk.GeoTagClient$Builder { public *; }
-keep public interface com.geotag.sdk.GeofenceEventCallback { *; }
