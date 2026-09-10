// Geofences API — API-CONTRACT.md "GET/POST/PUT/DELETE /geofences".
//
// Shapes (SPEC-DECISIONS D2 + D3):
//   polygon -> { name, type: "polygon", coordinates: [[lng,lat], ...], dwell_threshold_seconds }
//   circle  -> { name, type: "circle", center: [lng,lat], radius_m, dwell_threshold_seconds }

import { request, USE_FIXTURES } from './client.js';
import { fixtureApi } from './fixtures.js';

/** GET /geofences -> { count, geofences: [<geofence>] } */
export function listGeofences() {
  if (USE_FIXTURES) return fixtureApi.listGeofences();
  return request({ method: 'get', url: '/geofences' });
}

/** POST /geofences -> 201 <geofence> */
export function createGeofence(payload) {
  if (USE_FIXTURES) return fixtureApi.createGeofence(payload);
  return request({ method: 'post', url: '/geofences', data: payload });
}

/**
 * PUT /geofences/{id} -> 200 <geofence>.
 * Only supplied fields change, so callers pass a partial body. Note that an
 * explicit `dwell_threshold_seconds: null` means "disable dwell for this zone"
 * (D3) — omitting the key means "leave it alone".
 */
export function updateGeofence(id, payload) {
  if (USE_FIXTURES) return fixtureApi.updateGeofence(id, payload);
  return request({ method: 'put', url: `/geofences/${encodeURIComponent(id)}`, data: payload });
}

/** DELETE /geofences/{id} -> 204, no body. */
export function deleteGeofence(id) {
  if (USE_FIXTURES) return fixtureApi.deleteGeofence(id);
  return request({ method: 'delete', url: `/geofences/${encodeURIComponent(id)}` });
}

/**
 * Build a POST/PUT body from editor state.
 * Polygon rings are closed here (first === last) so the ring always has >= 4
 * positions from 3 drawn vertices; the API would close it anyway.
 */
export function buildGeofencePayload({ name, shapeType, points, center, radiusM, dwellSeconds }) {
  const base = {
    name: String(name || '').trim(),
    dwell_threshold_seconds:
      dwellSeconds === '' || dwellSeconds === null || dwellSeconds === undefined
        ? null
        : Number(dwellSeconds),
  };

  if (shapeType === 'circle') {
    return { ...base, type: 'circle', center, radius_m: Number(radiusM) };
  }

  const ring = points.map((point) => [point.lng, point.lat]);
  if (ring.length) {
    const [first] = ring;
    const last = ring[ring.length - 1];
    if (first[0] !== last[0] || first[1] !== last[1]) ring.push([...first]);
  }
  return { ...base, type: 'polygon', coordinates: ring };
}
