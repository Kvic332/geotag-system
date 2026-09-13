import { request } from './client.js';

export function getIntelligence(deviceId) {
  return request({ method: 'GET', url: `/devices/${encodeURIComponent(deviceId)}/intelligence` });
}

export function generateIntelligence(deviceId) {
  return request({ method: 'POST', url: `/devices/${encodeURIComponent(deviceId)}/intelligence/generate` });
}
