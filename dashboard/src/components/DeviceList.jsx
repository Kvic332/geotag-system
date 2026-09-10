import {
  batteryClass,
  describeApiError,
  formatCoord,
  fullTimestamp,
  isStale,
  relativeTime,
} from '../utils/format.js';

/**
 * Sidebar of active devices (GET /positions/active — devices seen in the last
 * hour, newest first). Shows battery and last-seen. Selecting a device drives
 * the map focus and triggers the history-trail load in App.
 */
export default function DeviceList({
  devices = [],
  loading = false,
  error = null,
  lastUpdated = null,
  selectedDeviceId = null,
  onSelect,
  history = { loading: false, error: null, count: 0 },
  onRefresh,
}) {
  const hasData = devices.length > 0;

  return (
    <section className="panel panel--devices" aria-label="Active devices">
      <header className="panel__head">
        <h2>
          Devices
          {hasData ? <span className="pill">{devices.length}</span> : null}
        </h2>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRefresh}>
          Refresh
        </button>
      </header>

      {error ? (
        <p className="notice notice--error" role="alert">
          {describeApiError(error)}
          {hasData ? <span className="notice__sub">Showing the last known positions.</span> : null}
        </p>
      ) : null}

      {loading && !hasData && !error ? <p className="notice">Loading devices…</p> : null}

      {!loading && !hasData && !error ? (
        <p className="notice">
          No devices reported in the last hour.
          <span className="notice__sub">
            Send a ping to <code>POST /positions</code> and it will appear here.
          </span>
        </p>
      ) : null}

      <ul className="device-list">
        {devices.map((device) => {
          const selected = device.device_id === selectedDeviceId;
          const stale = isStale(device.recorded_at);
          return (
            <li key={device.device_id}>
              <button
                type="button"
                className={`device-row${selected ? ' device-row--selected' : ''}`}
                onClick={() => onSelect && onSelect(device.device_id)}
                aria-pressed={selected}
              >
                <span className="device-row__top">
                  <span className="device-row__id" title={device.device_id}>
                    {device.device_id}
                  </span>
                  <span className={`battery battery--${batteryClass(device.battery)}`}>
                    <i className="battery__shell">
                      <i
                        className="battery__level"
                        style={{ width: `${clampPercent(device.battery)}%` }}
                      />
                    </i>
                    {device.battery === null || device.battery === undefined
                      ? '—'
                      : `${device.battery}%`}
                  </span>
                </span>
                <span className="device-row__meta">
                  <span className={stale ? 'seen seen--stale' : 'seen'} title={fullTimestamp(device.recorded_at)}>
                    <i className="dot" /> {relativeTime(device.recorded_at)}
                  </span>
                  <span className="coords">
                    {formatCoord(device.lat)}, {formatCoord(device.lng)}
                  </span>
                </span>
                {selected ? (
                  <span className="device-row__history">
                    {history.loading ? 'Loading history trail…' : null}
                    {history.error ? (
                      <span className="text-error">History: {describeApiError(history.error)}</span>
                    ) : null}
                    {!history.loading && !history.error
                      ? `Trail: ${history.count} point${history.count === 1 ? '' : 's'} (24h)`
                      : null}
                  </span>
                ) : null}
              </button>
            </li>
          );
        })}
      </ul>

      {lastUpdated ? (
        <p className="panel__foot">Updated {relativeTime(lastUpdated)}</p>
      ) : null}
    </section>
  );
}

function clampPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.min(100, Math.max(0, number));
}
