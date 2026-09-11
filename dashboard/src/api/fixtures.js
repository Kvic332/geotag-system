// Fixture backend — canned, contract-shaped data for VITE_USE_FIXTURES=true.
//
// Every payload here is copied from the shapes in API-CONTRACT.md so the UI can be
// exercised (and visually verified) with no backend running. Positions drift a
// little on each poll and events accumulate, so "live" behaviour is observable.
//
// This module is dev scaffolding only — it never runs when VITE_USE_FIXTURES is off.

const LAGOS = { lat: 6.5244, lng: 3.3792 };

const iso = (date) => new Date(date).toISOString().replace(/\.\d{3}Z$/, 'Z');

let nextId = 1;
const uuid = () => `fixture-${String(nextId++).padStart(4, '0')}-uuid`;

/** Ring around a centre point, closed (first === last), in [lng, lat] order. */
function ring(center, radiusMeters, segments = 24) {
  const points = [];
  const latRad = (center.lat * Math.PI) / 180;
  for (let i = 0; i < segments; i += 1) {
    const theta = (i / segments) * 2 * Math.PI;
    const dLat = (radiusMeters * Math.cos(theta)) / 111320;
    const dLng = (radiusMeters * Math.sin(theta)) / (111320 * Math.cos(latRad));
    points.push([
      Number((center.lng + dLng).toFixed(6)),
      Number((center.lat + dLat).toFixed(6)),
    ]);
  }
  points.push(points[0]);
  return points;
}

const devices = [
  { device_id: 'device_abc123', lat: LAGOS.lat + 0.0016, lng: LAGOS.lng + 0.0011, battery: 82, ageSeconds: 12 },
  { device_id: 'van_lekki_04', lat: LAGOS.lat - 0.0042, lng: LAGOS.lng + 0.0068, battery: 34, ageSeconds: 95 },
  { device_id: 'rider_772', lat: LAGOS.lat + 0.0071, lng: LAGOS.lng - 0.0035, battery: 9, ageSeconds: 1440 },
  { device_id: 'truck_ikeja_1', lat: LAGOS.lat + 0.0125, lng: LAGOS.lng + 0.0094, battery: 66, ageSeconds: 310 },
];

const geofences = [
  {
    id: 'a1111111-1111-4111-8111-111111111111',
    name: 'Apapa Depot',
    shape_type: 'polygon',
    coordinates: [
      [3.3742, 6.5204],
      [3.3862, 6.5204],
      [3.3862, 6.5294],
      [3.3742, 6.5294],
      [3.3742, 6.5204],
    ],
    center: null,
    radius_m: null,
    dwell_threshold_seconds: 300,
    created_at: iso(Date.now() - 86400000 * 6),
    device_count: 2,
  },
  {
    id: 'b2222222-2222-4222-8222-222222222222',
    name: 'Lekki Client Site',
    shape_type: 'circle',
    coordinates: ring({ lat: LAGOS.lat - 0.0042, lng: LAGOS.lng + 0.0068 }, 420),
    center: [Number((LAGOS.lng + 0.0068).toFixed(6)), Number((LAGOS.lat - 0.0042).toFixed(6))],
    radius_m: 420,
    dwell_threshold_seconds: null,
    created_at: iso(Date.now() - 86400000 * 2),
    device_count: 1,
  },
];

const events = [
  {
    id: uuid(),
    device_id: 'van_lekki_04',
    geofence_id: 'b2222222-2222-4222-8222-222222222222',
    geofence_name: 'Lekki Client Site',
    event_type: 'enter',
    lat: LAGOS.lat - 0.0042,
    lng: LAGOS.lng + 0.0068,
    occurred_at: iso(Date.now() - 90000),
    dwell_ms: null,
  },
  {
    id: uuid(),
    device_id: 'device_abc123',
    geofence_id: 'a1111111-1111-4111-8111-111111111111',
    geofence_name: 'Apapa Depot',
    event_type: 'dwell',
    lat: LAGOS.lat + 0.0016,
    lng: LAGOS.lng + 0.0011,
    occurred_at: iso(Date.now() - 240000),
    dwell_ms: 934000,
  },
  {
    id: uuid(),
    device_id: 'device_abc123',
    geofence_id: 'a1111111-1111-4111-8111-111111111111',
    geofence_name: 'Apapa Depot',
    event_type: 'enter',
    lat: LAGOS.lat + 0.0014,
    lng: LAGOS.lng + 0.0009,
    occurred_at: iso(Date.now() - 1180000),
    dwell_ms: null,
  },
  {
    id: uuid(),
    device_id: 'rider_772',
    geofence_id: 'a1111111-1111-4111-8111-111111111111',
    geofence_name: 'Apapa Depot',
    event_type: 'exit',
    lat: LAGOS.lat + 0.006,
    lng: LAGOS.lng - 0.003,
    occurred_at: iso(Date.now() - 3600000),
    dwell_ms: null,
  },
];

let tick = 0;

function drift() {
  tick += 1;
  devices.forEach((device, index) => {
    const phase = tick / 9 + index;
    device.lat = Number((device.lat + Math.sin(phase) * 0.00035).toFixed(6));
    device.lng = Number((device.lng + Math.cos(phase) * 0.00035).toFixed(6));
    if (device.ageSeconds < 3600) device.ageSeconds = Math.max(3, device.ageSeconds - 3);
  });

  // Emit a synthetic event every so often so the log visibly ticks over.
  if (tick % 6 === 0) {
    const device = devices[tick % devices.length];
    const zone = geofences[tick % geofences.length];
    const kinds = ['enter', 'exit', 'dwell'];
    const eventType = kinds[Math.floor(tick / 6) % kinds.length];
    events.unshift({
      id: uuid(),
      device_id: device.device_id,
      geofence_id: zone.id,
      geofence_name: zone.name,
      event_type: eventType,
      lat: device.lat,
      lng: device.lng,
      occurred_at: iso(Date.now()),
      dwell_ms: eventType === 'dwell' ? 300000 + tick * 1000 : null,
    });
    events.length = Math.min(events.length, 200);
  }
}

const delay = (ms = 140) => new Promise((resolve) => setTimeout(resolve, ms));

function notFound(message) {
  const error = new Error(message);
  error.__apiError = true;
  error.code = 'not_found';
  error.status = 404;
  return error;
}

function validation(message) {
  const error = new Error(message);
  error.__apiError = true;
  error.code = 'validation_error';
  error.status = 400;
  return error;
}

export const fixtureApi = {
  async getActivePositions() {
    await delay();
    drift();
    const list = [...devices].sort((a, b) => a.ageSeconds - b.ageSeconds);
    return {
      count: list.length,
      devices: list.map((device) => ({
        device_id: device.device_id,
        lat: device.lat,
        lng: device.lng,
        battery: device.battery,
        recorded_at: iso(Date.now() - device.ageSeconds * 1000),
      })),
    };
  },

  async getLatestPosition(deviceId) {
    await delay();
    const device = devices.find((d) => d.device_id === deviceId);
    if (!device) throw notFound(`No position for device ${deviceId}.`);
    return {
      device_id: device.device_id,
      lat: device.lat,
      lng: device.lng,
      accuracy: 12.5,
      speed: 4.2,
      bearing: 180,
      battery: device.battery,
      recorded_at: iso(Date.now() - device.ageSeconds * 1000),
      source: 'redis',
      zones: [geofences[0].id],
    };
  },

  async getHistory(deviceId) {
    await delay(220);
    const device = devices.find((d) => d.device_id === deviceId);
    if (!device) throw notFound(`No position history for device ${deviceId}.`);
    const positions = [];
    const steps = 40;
    for (let i = steps; i >= 0; i -= 1) {
      const t = (steps - i) / steps;
      positions.push({
        lat: Number((device.lat - 0.009 * (1 - t) + Math.sin(t * 7) * 0.0011).toFixed(6)),
        lng: Number((device.lng - 0.011 * (1 - t) + Math.cos(t * 5) * 0.0013).toFixed(6)),
        accuracy: 8 + (i % 5),
        speed: Number((3 + Math.sin(t * 9) * 2).toFixed(2)),
        bearing: Number(((t * 360) % 360).toFixed(1)),
        battery: Math.min(100, device.battery + i),
        // ±12 s jitter so the clock-integrity check sees natural variance
        recorded_at: iso(Date.now() - i * 90000 + Math.round((Math.random() - 0.5) * 24000)),
      });
    }
    return { device_id: deviceId, count: positions.length, positions };
  },

  async listGeofences() {
    await delay();
    return { count: geofences.length, geofences: geofences.map((zone) => ({ ...zone })) };
  },

  async createGeofence(payload) {
    await delay(260);
    if (!payload.name) throw validation('name is required.');
    const zone = buildZone(payload, {
      id: uuid(),
      created_at: iso(Date.now()),
      device_count: 0,
    });
    geofences.push(zone);
    return { ...zone };
  },

  async updateGeofence(id, payload) {
    await delay(260);
    const index = geofences.findIndex((zone) => zone.id === id);
    if (index === -1) throw notFound(`Geofence ${id} not found.`);
    const existing = geofences[index];
    const merged = buildZone(
      {
        name: payload.name ?? existing.name,
        type: payload.type ?? existing.shape_type,
        coordinates: payload.coordinates ?? existing.coordinates,
        center: payload.center ?? existing.center,
        radius_m: payload.radius_m ?? existing.radius_m,
        dwell_threshold_seconds:
          'dwell_threshold_seconds' in payload
            ? payload.dwell_threshold_seconds
            : existing.dwell_threshold_seconds,
      },
      { id: existing.id, created_at: existing.created_at, device_count: existing.device_count },
    );
    geofences[index] = merged;
    return { ...merged };
  },

  async deleteGeofence(id) {
    await delay(200);
    const index = geofences.findIndex((zone) => zone.id === id);
    if (index === -1) throw notFound(`Geofence ${id} not found.`);
    geofences.splice(index, 1);
    // Contract: deleting a zone must not orphan events — the FK is ON DELETE SET NULL,
    // so historic rows keep their name but lose the id.
    events.forEach((event) => {
      if (event.geofence_id === id) event.geofence_id = null;
    });
    return null;
  },

  async listEvents(params = {}) {
    await delay();
    let list = [...events];
    if (params.device_id) list = list.filter((e) => e.device_id === params.device_id);
    if (params.geofence_id) list = list.filter((e) => e.geofence_id === params.geofence_id);
    if (params.event_type) list = list.filter((e) => e.event_type === params.event_type);
    const limit = params.limit ?? 100;
    return {
      count: Math.min(list.length, limit),
      next_cursor: list.length > limit ? 'fixture-cursor' : null,
      events: list.slice(0, limit).map((event) => ({ ...event })),
    };
  },
};

function buildZone(payload, meta) {
  const shapeType = payload.type === 'circle' ? 'circle' : 'polygon';
  if (shapeType === 'circle') {
    const center = payload.center;
    const radius = Number(payload.radius_m);
    if (!Array.isArray(center) || center.length !== 2) {
      throw validation('center must be [lng, lat].');
    }
    if (!(radius > 0) || radius > 100000) {
      throw validation('radius_m must be > 0 and <= 100000.');
    }
    return {
      id: meta.id,
      name: payload.name,
      shape_type: 'circle',
      coordinates: ring({ lat: center[1], lng: center[0] }, radius, 64),
      center,
      radius_m: radius,
      dwell_threshold_seconds: payload.dwell_threshold_seconds ?? null,
      created_at: meta.created_at,
      device_count: meta.device_count,
    };
  }

  const coordinates = Array.isArray(payload.coordinates) ? [...payload.coordinates] : [];
  if (coordinates.length < 4) {
    throw validation('Polygon rings must have at least 4 positions.');
  }
  const [first] = coordinates;
  const last = coordinates[coordinates.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) coordinates.push([...first]);
  return {
    id: meta.id,
    name: payload.name,
    shape_type: 'polygon',
    coordinates,
    center: null,
    radius_m: null,
    dwell_threshold_seconds: payload.dwell_threshold_seconds ?? null,
    created_at: meta.created_at,
    device_count: meta.device_count,
  };
}
