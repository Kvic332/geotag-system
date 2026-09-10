// Display helpers. All API timestamps are ISO-8601 UTC strings (API-CONTRACT.md).

export function parseDate(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "12s ago" / "4m ago" / "2h ago" / "3d ago" */
export function relativeTime(value, now = Date.now()) {
  const date = parseDate(value);
  if (!date) return '—';
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 0) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function clockTime(value) {
  const date = parseDate(value);
  if (!date) return '—';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function fullTimestamp(value) {
  const date = parseDate(value);
  return date ? date.toLocaleString() : '—';
}

/** dwell_ms -> "15m 34s" (D3 dwell events carry a duration). */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return null;
  const totalSeconds = Math.max(0, Math.round(Number(ms) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/** dwell_threshold_seconds -> "5m" (null = dwell disabled for the zone). */
export function formatThreshold(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const value = Number(seconds);
  if (!Number.isFinite(value)) return null;
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  return `${(value / 3600).toFixed(1)}h`;
}

export function formatMeters(meters) {
  const value = Number(meters);
  if (!Number.isFinite(value)) return '—';
  return value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${Math.round(value)} m`;
}

export function formatCoord(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(5) : '—';
}

export function batteryClass(battery) {
  if (battery === null || battery === undefined) return 'unknown';
  if (battery <= 15) return 'critical';
  if (battery <= 40) return 'low';
  return 'ok';
}

/** A device is "stale" if its last ping is older than this (Redis TTL is 1h). */
export const STALE_AFTER_MS = 5 * 60 * 1000;

export function isStale(recordedAt, now = Date.now()) {
  const date = parseDate(recordedAt);
  if (!date) return true;
  return now - date.getTime() > STALE_AFTER_MS;
}

/** Human copy for a normalised API error, tuned per contract error code. */
export function describeApiError(error) {
  if (!error) return null;
  switch (error.code) {
    case 'unauthorized':
      return 'Unauthorized (401) — paste a valid dev token in the header. Mint one with backend/scripts/mint_dev_token.py.';
    case 'forbidden':
      return `Forbidden (403) — ${error.message}`;
    case 'not_found':
      return error.message || 'Not found (404).';
    case 'validation_error':
      return error.message || 'The API rejected the request (400).';
    case 'network_error':
      return `${error.message} (Also check CORS: the API must allow http://localhost:5173.)`;
    case 'timeout':
      return error.message;
    default:
      return error.message || 'Unexpected error.';
  }
}
