import { useCallback, useEffect, useMemo, useState } from 'react';
import LiveMap from './components/LiveMap.jsx';
import DeviceList from './components/DeviceList.jsx';
import GeofenceEditor from './components/GeofenceEditor.jsx';
import EventLog from './components/EventLog.jsx';
import TokenBar from './components/TokenBar.jsx';
import IntegrityPanel from './components/IntegrityPanel.jsx';
import ResidenceAnalysis from './components/ResidenceAnalysis.jsx';
import { API_URL, USE_FIXTURES, makeError, toApiError } from './api/client.js';
import { hasToken } from './api/token.js';
import { getActivePositions, getPositionHistory } from './api/positions.js';
import { listGeofences } from './api/geofences.js';
import { listEvents } from './api/events.js';
import { usePolling } from './hooks/usePolling.js';
import { describeApiError, relativeTime } from './utils/format.js';

// No WebSocket/SSE exists in the contract, so "real-time" is polling.
const DEVICE_POLL_MS = 5000;
const EVENT_POLL_MS = 5000;
const GEOFENCE_POLL_MS = 20000;

const HISTORY_WINDOW_MS = 24 * 60 * 60 * 1000;
const HISTORY_LIMIT = 1000;
const EVENT_LIMIT = 100;

export default function App() {
  const [map, setMap] = useState(null);
  const [selectedDeviceId, setSelectedDeviceId] = useState(null);
  const [eventType, setEventType] = useState('');
  const [filterEventsByDevice, setFilterEventsByDevice] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [tokenVersion, setTokenVersion] = useState(0);
  const [history, setHistory] = useState({ loading: false, error: null, positions: [] });

  const tokenPresent = hasToken();
  // Fixture mode needs no auth; otherwise nothing is fetched until a token exists,
  // so we don't spray 401s at the backend every 5 seconds.
  const enabled = USE_FIXTURES || tokenPresent;

  const missingTokenError = useMemo(
    () =>
      enabled
        ? null
        : makeError(
            'no_token',
            'No dev token set. Paste a JWT from backend/scripts/mint_dev_token.py in the header — every route requires Authorization: Bearer.',
          ),
    [enabled],
  );

  const eventDeviceFilter = filterEventsByDevice ? selectedDeviceId : null;

  // tokenVersion is in the dep lists on purpose: changing the token must refetch.
  /* eslint-disable react-hooks/exhaustive-deps */
  const fetchDevices = useCallback(() => getActivePositions(), [tokenVersion]);
  const fetchGeofences = useCallback(() => listGeofences(), [tokenVersion]);
  const fetchEvents = useCallback(
    () =>
      listEvents({
        eventType: eventType || undefined,
        deviceId: eventDeviceFilter || undefined,
        limit: EVENT_LIMIT,
      }),
    [eventType, eventDeviceFilter, tokenVersion],
  );
  /* eslint-enable react-hooks/exhaustive-deps */

  const devicesPoll = usePolling(fetchDevices, DEVICE_POLL_MS, { enabled });
  const geofencesPoll = usePolling(fetchGeofences, GEOFENCE_POLL_MS, { enabled });
  const eventsPoll = usePolling(fetchEvents, EVENT_POLL_MS, { enabled });

  const devices = devicesPoll.data?.devices ?? [];
  const geofences = geofencesPoll.data?.geofences ?? [];
  const events = eventsPoll.data?.events ?? [];

  // History trail for the selected device (GET /positions/{id}/history).
  useEffect(() => {
    if (!selectedDeviceId || !enabled) {
      setHistory({ loading: false, error: null, positions: [] });
      return undefined;
    }

    let cancelled = false;
    setHistory({ loading: true, error: null, positions: [] });

    getPositionHistory(selectedDeviceId, {
      from: new Date(Date.now() - HISTORY_WINDOW_MS),
      to: new Date(),
      limit: HISTORY_LIMIT,
    })
      .then((result) => {
        if (cancelled) return;
        setHistory({ loading: false, error: null, positions: result?.positions ?? [] });
      })
      .catch((err) => {
        if (cancelled) return;
        setHistory({ loading: false, error: toApiError(err), positions: [] });
      });

    return () => {
      cancelled = true;
    };
  }, [selectedDeviceId, enabled, tokenVersion]);

  const handleMapReady = useCallback((instance) => setMap(instance), []);
  const handleDrawingChange = useCallback((value) => setDrawing(value), []);
  const handleTokenChange = useCallback(() => setTokenVersion((version) => version + 1), []);

  const handleSelectDevice = useCallback((deviceId) => {
    setSelectedDeviceId((current) => (current === deviceId ? null : deviceId));
  }, []);

  const handleGeofencesChanged = useCallback(async () => {
    await geofencesPoll.refresh();
  }, [geofencesPoll]);

  const connection = describeConnection({
    enabled,
    error: devicesPoll.error,
    lastUpdated: devicesPoll.lastUpdated,
    loading: devicesPoll.loading,
  });

  const mapBanner = missingTokenError
    ? 'No dev token — the map has no data to show. Paste a token in the header.'
    : devicesPoll.error
      ? describeApiError(devicesPoll.error)
      : null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="logo" aria-hidden="true" />
          <div>
            <h1>GeoTag</h1>
            <p>Live device map &amp; geofencing</p>
          </div>
        </div>

        <div className="topbar__status">
          <span className={`conn conn--${connection.state}`} title={connection.detail}>
            <i className="dot" />
            {connection.label}
          </span>
          <code className="api-url" title="VITE_API_URL">
            {USE_FIXTURES ? 'fixtures (no backend)' : API_URL}
          </code>
          {USE_FIXTURES ? <span className="badge badge--fixtures">FIXTURE MODE</span> : null}
        </div>

        {USE_FIXTURES ? null : <TokenBar onTokenChange={handleTokenChange} />}
      </header>

      {missingTokenError ? (
        <p className="topbanner topbanner--warn" role="alert">
          {describeApiError(missingTokenError)}
        </p>
      ) : null}

      <main className="layout">
        <div className="col col--left">
          <DeviceList
            devices={devices}
            loading={devicesPoll.loading}
            error={devicesPoll.error || missingTokenError}
            lastUpdated={devicesPoll.lastUpdated}
            selectedDeviceId={selectedDeviceId}
            onSelect={handleSelectDevice}
            onRefresh={devicesPoll.refresh}
            history={{
              loading: history.loading,
              error: history.error,
              count: history.positions.length,
            }}
          />
          <IntegrityPanel
            deviceId={selectedDeviceId}
            positions={history.positions}
            loading={history.loading}
          />
          <ResidenceAnalysis deviceId={selectedDeviceId} />
        </div>

        <div className="col col--map">
          <LiveMap
            devices={devices}
            geofences={geofences}
            historyPositions={history.positions}
            selectedDeviceId={selectedDeviceId}
            onSelectDevice={handleSelectDevice}
            onMapReady={handleMapReady}
            drawing={drawing}
            banner={mapBanner}
            dataReady={Boolean(devicesPoll.lastUpdated) && Boolean(geofencesPoll.lastUpdated)}
          />
        </div>

        <div className="col col--right">
          <GeofenceEditor
            map={map}
            geofences={geofences}
            loading={geofencesPoll.loading}
            error={geofencesPoll.error || missingTokenError}
            onChanged={handleGeofencesChanged}
            onDrawingChange={handleDrawingChange}
          />

          <div className="event-filters">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={filterEventsByDevice}
                disabled={!selectedDeviceId}
                onChange={(event) => setFilterEventsByDevice(event.target.checked)}
              />
              Only the selected device
            </label>
          </div>

          <EventLog
            events={events}
            loading={eventsPoll.loading}
            error={eventsPoll.error || missingTokenError}
            lastUpdated={eventsPoll.lastUpdated}
            nextCursor={eventsPoll.data?.next_cursor ?? null}
            eventType={eventType}
            onEventTypeChange={setEventType}
            deviceFilter={eventDeviceFilter}
            onClearDeviceFilter={() => setFilterEventsByDevice(false)}
            onSelectDevice={handleSelectDevice}
          />
        </div>
      </main>
    </div>
  );
}

function describeConnection({ enabled, error, lastUpdated, loading }) {
  if (!enabled) {
    return { state: 'off', label: 'not authenticated', detail: 'No bearer token set.' };
  }
  if (loading && !lastUpdated && !error) {
    return { state: 'pending', label: 'connecting…', detail: 'First poll in flight.' };
  }
  if (error) {
    return {
      state: 'down',
      label: error.code === 'unauthorized' ? 'unauthorized' : 'backend unreachable',
      detail: describeApiError(error),
    };
  }
  return {
    state: 'up',
    label: `live · ${relativeTime(lastUpdated)}`,
    detail: lastUpdated ? lastUpdated.toLocaleString() : '',
  };
}
