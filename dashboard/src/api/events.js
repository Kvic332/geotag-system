// Events API — API-CONTRACT.md "GET /events".
// Ordered occurred_at DESCENDING (newest first) — it feeds the live event log.

import { request, USE_FIXTURES } from './client.js';
import { fixtureApi } from './fixtures.js';

/**
 * GET /events
 * Params: device_id, geofence_id, event_type (enter|exit|dwell), from, to,
 *         limit (default 100, max 1000), cursor.
 * -> { count, next_cursor, events: [...] }
 */
export function listEvents({ deviceId, geofenceId, eventType, from, to, limit, cursor } = {}) {
  const params = {};
  if (deviceId) params.device_id = deviceId;
  if (geofenceId) params.geofence_id = geofenceId;
  if (eventType) params.event_type = eventType;
  if (from) params.from = from instanceof Date ? from.toISOString() : from;
  if (to) params.to = to instanceof Date ? to.toISOString() : to;
  if (limit) params.limit = limit;
  if (cursor) params.cursor = cursor;

  if (USE_FIXTURES) return fixtureApi.listEvents(params);
  return request({ method: 'get', url: '/events', params });
}
