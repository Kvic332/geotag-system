// Positions API — API-CONTRACT.md "POST /positions" .. "GET /positions/active".

import { request, USE_FIXTURES } from './client.js';
import { fixtureApi } from './fixtures.js';

/**
 * GET /positions/active
 * -> { count, devices: [{ device_id, lat, lng, battery, recorded_at }] }
 * Devices seen in the last hour, newest first.
 */
export function getActivePositions() {
  if (USE_FIXTURES) return fixtureApi.getActivePositions();
  return request({ method: 'get', url: '/positions/active' });
}

/**
 * GET /positions/{device_id}
 * -> { device_id, lat, lng, accuracy, speed, bearing, battery,
 *      recorded_at, source: "redis"|"postgres", zones: [uuid] }
 * Unknown device -> 404 not_found.
 */
export function getLatestPosition(deviceId) {
  if (USE_FIXTURES) return fixtureApi.getLatestPosition(deviceId);
  return request({ method: 'get', url: `/positions/${encodeURIComponent(deviceId)}` });
}

/**
 * GET /positions/{device_id}/history
 * -> { device_id, count, positions: [...] } ordered recorded_at ascending.
 *
 * `from`/`to` accept ISO-8601 or unix seconds; we always send ISO-8601.
 */
export function getPositionHistory(deviceId, { from, to, limit } = {}) {
  if (USE_FIXTURES) return fixtureApi.getHistory(deviceId);
  const params = {};
  if (from) params.from = from instanceof Date ? from.toISOString() : from;
  if (to) params.to = to instanceof Date ? to.toISOString() : to;
  if (limit) params.limit = limit;
  return request({
    method: 'get',
    url: `/positions/${encodeURIComponent(deviceId)}/history`,
    params,
  });
}
