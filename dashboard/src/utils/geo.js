import L from 'leaflet';

/**
 * API coordinates are ALWAYS [lng, lat] (GeoJSON order, per API-CONTRACT.md);
 * Leaflet wants [lat, lng]. Every conversion goes through here.
 */
export function toLatLng(coordinate) {
  return [Number(coordinate[1]), Number(coordinate[0])];
}

/**
 * Bounding box for a circle zone, computed WITHOUT a map.
 * Leaflet's Circle.getBounds() needs the layer attached to a map; LatLng.toBounds
 * does not, so this also works before the first render.
 *
 * @param {[number, number]} center [lng, lat]
 * @param {number} radiusMeters
 * @returns {L.LatLngBounds|null}
 */
export function circleBounds(center, radiusMeters) {
  const radius = Number(radiusMeters);
  if (!Array.isArray(center) || !Number.isFinite(radius) || radius <= 0) return null;
  const [lat, lng] = toLatLng(center);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return L.latLng(lat, lng).toBounds(radius * 2);
}

/** Bounds covering a whole geofence, whichever shape it is. Null if undrawable. */
export function geofenceBounds(zone) {
  if (!zone) return null;
  if (zone.shape_type === 'circle' && Array.isArray(zone.center) && zone.radius_m) {
    return circleBounds(zone.center, zone.radius_m);
  }
  if (Array.isArray(zone.coordinates) && zone.coordinates.length >= 3) {
    const bounds = L.latLngBounds(zone.coordinates.map(toLatLng));
    return bounds.isValid() ? bounds : null;
  }
  return null;
}
