// Device integrity analysis — pure functions that work on position history.
// Computed checks derive from position data; static checks are fixture-hardcoded
// (in a real backend these would come from a separate /devices/{id}/integrity endpoint).

const UNKNOWN = { status: 'unknown', detail: 'Insufficient data' };

// ── Fixture static checks (per device) ──────────────────────────────────────
// Covers signals that can't be derived from position history alone:
// timezone, IP diversity, GPS spoofing apps, app state, WiFi.
const FIXTURE_STATIC = {
  device_abc123: {
    timezone: { status: 'pass', detail: 'Africa/Lagos matches GPS location' },
    ip_diversity: { status: 'pass', detail: 'Single carrier subnet — no unusual roaming' },
    gps_spoofing_apps: { status: 'pass', detail: 'No mock location apps detected' },
    app_state: { status: 'pass', detail: '94% foreground pings — healthy ratio' },
    wifi: { status: 'pass', detail: '3 known networks, all consistent with location' },
    clock_override: null,
  },
  van_lekki_04: {
    timezone: { status: 'pass', detail: 'Africa/Lagos matches GPS location' },
    ip_diversity: { status: 'warn', detail: '4 different carrier IPs in last session — unusual roaming' },
    gps_spoofing_apps: { status: 'pass', detail: 'No mock location apps detected' },
    app_state: { status: 'pass', detail: '88% foreground pings — healthy ratio' },
    wifi: { status: 'warn', detail: 'No WiFi connections recorded — all pings via mobile data' },
    clock_override: null,
  },
  rider_772: {
    timezone: { status: 'fail', detail: 'Device timezone (Europe/London) does not match GPS location (Lagos)' },
    ip_diversity: { status: 'pass', detail: 'Consistent IP across the session' },
    gps_spoofing_apps: { status: 'fail', detail: 'Mock Location app (com.lexa.fakegps) detected on device' },
    app_state: { status: 'warn', detail: '61% background pings — possible doze mode or low battery' },
    wifi: { status: 'pass', detail: 'WiFi networks consistent with location' },
    // Timestamps for rider_772 are suspiciously regular in fixture mode
    clock_override: { status: 'warn', detail: 'Timestamps too regular — possible replay or scripted client' },
  },
  truck_ikeja_1: {
    timezone: { status: 'pass', detail: 'Africa/Lagos matches GPS location' },
    ip_diversity: { status: 'pass', detail: 'Fixed vehicle IP — consistent with OBD device' },
    gps_spoofing_apps: { status: 'pass', detail: 'No mock location apps detected' },
    app_state: { status: 'warn', detail: '72% background pings — vehicle device likely in doze mode' },
    wifi: { status: 'pass', detail: 'In-vehicle hotspot — stable and consistent' },
    clock_override: null,
  },
};

const DEFAULT_STATIC = {
  timezone: UNKNOWN,
  ip_diversity: UNKNOWN,
  gps_spoofing_apps: UNKNOWN,
  app_state: UNKNOWN,
  wifi: UNKNOWN,
  clock_override: null,
};

// ── Computed checks ──────────────────────────────────────────────────────────

function checkLocationPattern(positions) {
  if (positions.length < 3) return { status: 'unknown', detail: 'Not enough position data' };

  const unique = new Set(positions.map((p) => `${p.lat.toFixed(4)},${p.lng.toFixed(4)}`));
  if (unique.size <= 2 && positions.length >= 6) {
    return { status: 'fail', detail: 'All pings at identical coordinates — GPS spoofing suspected' };
  }

  const lats = positions.map((p) => p.lat);
  const lngs = positions.map((p) => p.lng);
  const spanM = Math.round(
    Math.sqrt(
      ((Math.max(...lats) - Math.min(...lats)) * 111000) ** 2 +
        ((Math.max(...lngs) - Math.min(...lngs)) * 111000) ** 2,
    ),
  );
  return { status: 'pass', detail: `${unique.size} distinct positions across ~${spanM > 0 ? spanM : '<1'}m range` };
}

function checkClockIntegrity(positions, override) {
  if (override) return override;
  if (positions.length < 5) return { status: 'unknown', detail: 'Not enough timestamps' };

  const times = positions
    .map((p) => new Date(p.recorded_at).getTime())
    .filter((t) => !Number.isNaN(t))
    .sort((a, b) => a - b);

  const intervals = times.slice(1).map((t, i) => t - times[i]);
  const mean = intervals.reduce((a, b) => a + b, 0) / intervals.length;
  if (mean <= 0) return { status: 'unknown', detail: 'Invalid timestamps' };

  const variance = intervals.reduce((s, t) => s + (t - mean) ** 2, 0) / intervals.length;
  const cv = Math.sqrt(variance) / mean;

  if (cv < 0.04) {
    return { status: 'warn', detail: 'Timestamps perfectly regular — possible replay or simulation' };
  }
  return { status: 'pass', detail: `Natural timing variance (CV ${(cv * 100).toFixed(1)}%)` };
}

function checkChargingPattern(positions) {
  const rows = positions
    .filter((p) => p.battery != null)
    .sort((a, b) => new Date(a.recorded_at) - new Date(b.recorded_at));

  if (rows.length < 4) return { status: 'unknown', detail: 'No battery data available' };

  let spikes = 0;
  for (let i = 1; i < rows.length; i++) {
    const delta = rows[i].battery - rows[i - 1].battery;
    if (delta > 25 || delta < -35) spikes++;
  }

  const latest = rows[rows.length - 1].battery;
  if (spikes > 1) {
    return { status: 'warn', detail: `${spikes} abnormal battery level changes detected` };
  }
  if (latest <= 10) {
    return { status: 'warn', detail: `Battery critically low (${latest}%) — signal reliability may be affected` };
  }
  return { status: 'pass', detail: `Normal charge/drain pattern, last reading ${latest}%` };
}

function checkSensorIntegrity(positions) {
  if (positions.length < 5) return { status: 'unknown', detail: 'Not enough position samples' };

  const sorted = [...positions].sort((a, b) => new Date(a.recorded_at) - new Date(b.recorded_at));
  let bad = 0;
  let checked = 0;

  for (let i = 1; i < sorted.length; i++) {
    const prev = sorted[i - 1];
    const curr = sorted[i];
    if (curr.speed == null) continue;

    const dt = (new Date(curr.recorded_at) - new Date(prev.recorded_at)) / 1000;
    if (dt <= 0) continue;

    const dlat = curr.lat - prev.lat;
    const dlng = curr.lng - prev.lng;
    const gpsSpeed = (Math.sqrt(dlat * dlat + dlng * dlng) * 111000) / dt;

    checked++;
    if (Math.abs(gpsSpeed - curr.speed) > 25 && gpsSpeed > 0.5) bad++;
  }

  if (checked < 3) return { status: 'unknown', detail: 'Insufficient speed data' };

  if (bad / checked > 0.35) {
    return { status: 'warn', detail: `Speed sensor disagrees with GPS on ${bad}/${checked} samples` };
  }
  return { status: 'pass', detail: `Speed sensor consistent with GPS displacement (${checked} samples)` };
}

// ── Public API ───────────────────────────────────────────────────────────────

export function analyzeIntegrity(deviceId, positions) {
  const s = FIXTURE_STATIC[deviceId] || DEFAULT_STATIC;

  return [
    { id: 'location_pattern', name: 'Location Pattern', ...checkLocationPattern(positions) },
    { id: 'timezone', name: 'Timezone Consistency', ...(s.timezone ?? UNKNOWN) },
    { id: 'clock', name: 'Clock Integrity', ...checkClockIntegrity(positions, s.clock_override) },
    { id: 'gps_apps', name: 'GPS Spoofing Apps', ...(s.gps_spoofing_apps ?? UNKNOWN) },
    { id: 'sensor', name: 'Sensor Integrity', ...checkSensorIntegrity(positions) },
    { id: 'charging', name: 'Charging Pattern', ...checkChargingPattern(positions) },
    { id: 'app_state', name: 'App State Pattern', ...(s.app_state ?? UNKNOWN) },
    { id: 'wifi', name: 'WiFi Consistency', ...(s.wifi ?? UNKNOWN) },
    { id: 'ip_diversity', name: 'IP Diversity', ...(s.ip_diversity ?? UNKNOWN) },
  ];
}

export function integrityScore(checks) {
  const known = checks.filter((c) => c.status !== 'unknown');
  return {
    passed: known.filter((c) => c.status === 'pass').length,
    warned: known.filter((c) => c.status === 'warn').length,
    failed: known.filter((c) => c.status === 'fail').length,
    total: known.length,
  };
}
