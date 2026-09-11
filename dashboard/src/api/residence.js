import { USE_FIXTURES, request } from './client.js';

const FIXTURE = {
  device_id: 'demo_device',
  cluster_lat: 6.482,
  cluster_lng: 3.284,
  night_pings: 42,
  total_night_pings: 50,
  days_analyzed: 7,
  confidence_pct: 84.0,
};

export async function getResidence(deviceId) {
  if (USE_FIXTURES) return FIXTURE;
  return request({ method: 'GET', url: `/devices/${encodeURIComponent(deviceId)}/residence` });
}
