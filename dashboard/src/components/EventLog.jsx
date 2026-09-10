import { useEffect, useMemo, useRef, useState } from 'react';
import {
  clockTime,
  describeApiError,
  formatDuration,
  fullTimestamp,
  relativeTime,
} from '../utils/format.js';

const EVENT_TYPES = ['enter', 'exit', 'dwell'];

/**
 * Live event feed from GET /events, newest first.
 *
 * The contract already orders occurred_at descending; the sort here is defensive
 * so a backend ordering regression shows up as "still newest first" rather than
 * a scrambled feed. Dwell rows carry `dwell_ms` and render the duration.
 */
export default function EventLog({
  events = [],
  loading = false,
  error = null,
  lastUpdated = null,
  nextCursor = null,
  eventType = '',
  onEventTypeChange,
  deviceFilter = null,
  onClearDeviceFilter,
  onSelectDevice,
}) {
  const sorted = useMemo(
    () =>
      [...events].sort(
        (a, b) => new Date(b.occurred_at).getTime() - new Date(a.occurred_at).getTime(),
      ),
    [events],
  );

  const flashed = useFlashOnNew(sorted);
  const hasData = sorted.length > 0;

  return (
    <section className="panel panel--events" aria-label="Geofence event log">
      <header className="panel__head">
        <h2>
          Events
          {hasData ? <span className="pill">{sorted.length}</span> : null}
        </h2>
        <label className="field field--inline">
          <span className="sr-only">Filter by event type</span>
          <select
            value={eventType}
            onChange={(e) => onEventTypeChange && onEventTypeChange(e.target.value)}
          >
            <option value="">all types</option>
            {EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
      </header>

      {deviceFilter ? (
        <p className="filter-chip">
          Filtered to <strong>{deviceFilter}</strong>
          <button type="button" className="btn btn--ghost btn--sm" onClick={onClearDeviceFilter}>
            clear
          </button>
        </p>
      ) : null}

      {error ? (
        <p className="notice notice--error" role="alert">
          {describeApiError(error)}
          {hasData ? <span className="notice__sub">Showing the last events received.</span> : null}
        </p>
      ) : null}

      {loading && !hasData && !error ? <p className="notice">Loading events…</p> : null}

      {!loading && !hasData && !error ? (
        <p className="notice">
          No events yet.
          <span className="notice__sub">
            Enter, exit and dwell events appear here as devices cross zone boundaries.
          </span>
        </p>
      ) : null}

      <ol className="event-list">
        {sorted.map((event) => {
          const duration = formatDuration(event.dwell_ms);
          return (
            <li
              key={event.id}
              className={`event-row event-row--${event.event_type}${
                flashed.has(event.id) ? ' event-row--new' : ''
              }`}
            >
              <span className={`event-badge event-badge--${event.event_type}`}>
                {event.event_type}
              </span>
              <span className="event-body">
                <span className="event-line">
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => onSelectDevice && onSelectDevice(event.device_id)}
                    title={`Focus ${event.device_id} on the map`}
                  >
                    {event.device_id}
                  </button>
                  <span className="event-verb">{verbFor(event.event_type)}</span>
                  <strong>{event.geofence_name || '(deleted zone)'}</strong>
                </span>
                <span className="event-sub">
                  <span title={fullTimestamp(event.occurred_at)}>
                    {clockTime(event.occurred_at)} · {relativeTime(event.occurred_at)}
                  </span>
                  {duration ? <span className="event-dwell">dwelled {duration}</span> : null}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      <p className="panel__foot">
        {lastUpdated ? `Updated ${relativeTime(lastUpdated)}` : null}
        {nextCursor ? ' · older events available (paging not wired up)' : null}
      </p>
    </section>
  );
}

function verbFor(type) {
  if (type === 'enter') return 'entered';
  if (type === 'exit') return 'exited';
  if (type === 'dwell') return 'is dwelling in';
  return type;
}

/** Briefly highlight rows that were not in the previous poll. */
function useFlashOnNew(events) {
  const seenRef = useRef(null);
  const [flashed, setFlashed] = useState(() => new Set());

  useEffect(() => {
    const ids = new Set(events.map((event) => event.id));
    if (seenRef.current === null) {
      // First payload: seed without flashing everything.
      seenRef.current = ids;
      return undefined;
    }
    const fresh = new Set();
    ids.forEach((id) => {
      if (!seenRef.current.has(id)) fresh.add(id);
    });
    seenRef.current = ids;
    if (fresh.size === 0) return undefined;

    setFlashed(fresh);
    const timer = window.setTimeout(() => setFlashed(new Set()), 2200);
    return () => window.clearTimeout(timer);
  }, [events]);

  return flashed;
}
