import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import {
  buildGeofencePayload,
  createGeofence,
  deleteGeofence,
  updateGeofence,
} from '../api/geofences.js';
import { toApiError } from '../api/client.js';
import { geofenceBounds } from '../utils/geo.js';
import { describeApiError, formatMeters, formatThreshold } from '../utils/format.js';

const IDLE = 'idle';
const POLYGON = 'polygon';
const CIRCLE = 'circle';

const MAX_RADIUS_M = 100000; // API-CONTRACT: radius_m must be > 0 and <= 100000

/**
 * Draw a polygon OR a circle on the live map and save it via POST /geofences,
 * including `dwell_threshold_seconds` (SPEC-DECISIONS D3). Existing zones can be
 * renamed / re-thresholded / re-drawn (PUT) or removed (DELETE).
 *
 * Drawing is done directly against the Leaflet map instance owned by LiveMap,
 * which is handed down from App once the map is ready.
 */
export default function GeofenceEditor({
  map,
  geofences = [],
  loading = false,
  error = null,
  onChanged,
  onDrawingChange,
}) {
  const [mode, setMode] = useState(IDLE);
  const [points, setPoints] = useState([]);
  const [center, setCenter] = useState(null);
  const [radiusM, setRadiusM] = useState(250);
  const [circleFixed, setCircleFixed] = useState(false);
  const [name, setName] = useState('');
  const [dwell, setDwell] = useState('');
  const [editingId, setEditingId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);
  const [notice, setNotice] = useState(null);

  const draftLayerRef = useRef(null);
  // Mirrors of the circle draft state so the Leaflet handlers can read the
  // current values without re-subscribing on every pointer move.
  const centerRef = useRef(null);
  const circleFixedRef = useRef(false);

  const editingZone = useMemo(
    () => geofences.find((zone) => zone.id === editingId) || null,
    [geofences, editingId],
  );

  const hasDraftGeometry =
    (mode === POLYGON && points.length >= 3) || (mode === CIRCLE && center && radiusM > 0);

  // --- draft layer ---------------------------------------------------------
  useEffect(() => {
    if (!map) return undefined;
    const group = L.layerGroup().addTo(map);
    draftLayerRef.current = group;
    return () => {
      draftLayerRef.current = null;
      map.removeLayer(group);
    };
  }, [map]);

  useEffect(() => {
    if (onDrawingChange) onDrawingChange(mode !== IDLE);
  }, [mode, onDrawingChange]);

  const resetDraft = useCallback(() => {
    centerRef.current = null;
    circleFixedRef.current = false;
    setPoints([]);
    setCenter(null);
    setCircleFixed(false);
    setRadiusM(250);
  }, []);

  const cancelDraw = useCallback(() => {
    setMode(IDLE);
    resetDraft();
  }, [resetDraft]);

  // --- map interaction while drawing ---------------------------------------
  useEffect(() => {
    if (!map || mode === IDLE) return undefined;

    const onClick = (event) => {
      const { lat, lng } = event.latlng;
      if (mode === POLYGON) {
        setPoints((current) => [...current, { lat, lng }]);
        return;
      }
      // Circle: first click drops the centre, second click fixes the radius.
      if (!centerRef.current) {
        centerRef.current = { lat, lng };
        circleFixedRef.current = false;
        setCenter({ lat, lng });
        setCircleFixed(false);
      } else {
        circleFixedRef.current = true;
        setCircleFixed(true);
      }
    };

    const onMouseMove = (event) => {
      if (mode !== CIRCLE) return;
      if (!centerRef.current || circleFixedRef.current) return;
      const distance = map.distance(
        [centerRef.current.lat, centerRef.current.lng],
        [event.latlng.lat, event.latlng.lng],
      );
      setRadiusM(Math.max(1, Math.round(distance)));
    };

    const onKeyDown = (event) => {
      if (event.key === 'Escape') cancelDraw();
    };

    map.doubleClickZoom.disable();
    map.on('click', onClick);
    map.on('mousemove', onMouseMove);
    window.addEventListener('keydown', onKeyDown);

    return () => {
      map.doubleClickZoom.enable();
      map.off('click', onClick);
      map.off('mousemove', onMouseMove);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [map, mode, cancelDraw]);

  // --- render the draft ----------------------------------------------------
  useEffect(() => {
    const layer = draftLayerRef.current;
    if (!layer) return;
    layer.clearLayers();

    if (mode === POLYGON && points.length > 0) {
      const latLngs = points.map((p) => [p.lat, p.lng]);
      if (points.length >= 3) {
        L.polygon(latLngs, {
          color: '#059669',
          weight: 2,
          dashArray: '6 4',
          fillColor: '#059669',
          fillOpacity: 0.12,
        }).addTo(layer);
      } else if (points.length === 2) {
        L.polyline(latLngs, { color: '#059669', weight: 2, dashArray: '6 4' }).addTo(layer);
      }
      latLngs.forEach((latLng, index) => {
        L.circleMarker(latLng, {
          radius: 4,
          color: '#059669',
          fillColor: index === 0 ? '#059669' : '#ffffff',
          fillOpacity: 1,
          weight: 2,
        }).addTo(layer);
      });
    }

    if (mode === CIRCLE && center) {
      L.circle([center.lat, center.lng], {
        radius: Math.max(1, Number(radiusM) || 0),
        color: '#059669',
        weight: 2,
        dashArray: '6 4',
        fillColor: '#059669',
        fillOpacity: 0.12,
      }).addTo(layer);
      L.circleMarker([center.lat, center.lng], {
        radius: 4,
        color: '#059669',
        fillColor: '#ffffff',
        fillOpacity: 1,
        weight: 2,
      }).addTo(layer);
    }
  }, [mode, points, center, radiusM]);

  const startDraw = useCallback(
    (nextMode) => {
      setFormError(null);
      setNotice(null);
      resetDraft();
      setMode(nextMode);
      if (!map) {
        setFormError({ code: 'internal_error', message: 'Map is not ready yet.' });
      }
    },
    [map, resetDraft],
  );

  const startEdit = useCallback((zone) => {
    setEditingId(zone.id);
    setName(zone.name || '');
    setDwell(
      zone.dwell_threshold_seconds === null || zone.dwell_threshold_seconds === undefined
        ? ''
        : String(zone.dwell_threshold_seconds),
    );
    setMode(IDLE);
    resetDraft();
    setFormError(null);
    setNotice(null);
  }, [resetDraft]);

  const stopEdit = useCallback(() => {
    setEditingId(null);
    setName('');
    setDwell('');
    cancelDraw();
    setFormError(null);
  }, [cancelDraw]);

  const zoomToZone = useCallback(
    (zone) => {
      if (!map) return;
      const bounds = geofenceBounds(zone);
      if (bounds) map.fitBounds(bounds, { padding: [40, 40] });
    },
    [map],
  );

  const handleSave = async (event) => {
    event.preventDefault();
    setFormError(null);
    setNotice(null);

    const trimmedName = name.trim();
    if (!trimmedName) {
      setFormError({ code: 'validation_error', message: 'Give the zone a name.' });
      return;
    }
    if (dwell !== '' && (!Number.isFinite(Number(dwell)) || Number(dwell) <= 0)) {
      setFormError({
        code: 'validation_error',
        message: 'Dwell threshold must be a positive number of seconds, or blank to disable dwell.',
      });
      return;
    }

    const creating = !editingId;
    if (creating && !hasDraftGeometry) {
      setFormError({
        code: 'validation_error',
        message: 'Draw a polygon (3+ points) or a circle on the map first.',
      });
      return;
    }
    if (mode === CIRCLE && center && !(Number(radiusM) > 0 && Number(radiusM) <= MAX_RADIUS_M)) {
      setFormError({
        code: 'validation_error',
        message: `Radius must be greater than 0 and at most ${MAX_RADIUS_M} m.`,
      });
      return;
    }

    const geometry = hasDraftGeometry
      ? buildGeofencePayload({
          name: trimmedName,
          shapeType: mode,
          points,
          center: center ? [center.lng, center.lat] : null,
          radiusM,
          dwellSeconds: dwell,
        })
      : null;

    // PUT: only supplied fields change. Send geometry only when it was redrawn.
    const payload = geometry ?? {
      name: trimmedName,
      dwell_threshold_seconds: dwell === '' ? null : Number(dwell),
    };
    if (geometry) {
      payload.name = trimmedName;
      payload.dwell_threshold_seconds = dwell === '' ? null : Number(dwell);
    }

    setBusy(true);
    try {
      if (creating) {
        const saved = await createGeofence(payload);
        setNotice(`Created "${saved?.name ?? trimmedName}".`);
        setName('');
        setDwell('');
      } else {
        const saved = await updateGeofence(editingId, payload);
        setNotice(`Updated "${saved?.name ?? trimmedName}".`);
        setEditingId(null);
        setName('');
        setDwell('');
      }
      cancelDraw();
      if (onChanged) await onChanged();
    } catch (err) {
      setFormError(toApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (zone) => {
    // eslint-disable-next-line no-alert
    const confirmed = window.confirm(
      `Delete zone "${zone.name}"?\n\nHistoric events keep their name but lose the zone link.`,
    );
    if (!confirmed) return;
    setBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      await deleteGeofence(zone.id);
      setNotice(`Deleted "${zone.name}".`);
      if (editingId === zone.id) stopEdit();
      if (onChanged) await onChanged();
    } catch (err) {
      setFormError(toApiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel panel--geofences" aria-label="Geofence editor">
      <header className="panel__head">
        <h2>
          Geofences
          {geofences.length ? <span className="pill">{geofences.length}</span> : null}
        </h2>
      </header>

      <div className="draw-controls">
        <button
          type="button"
          className={`btn${mode === POLYGON ? ' btn--active' : ''}`}
          onClick={() => (mode === POLYGON ? cancelDraw() : startDraw(POLYGON))}
          disabled={!map}
        >
          {mode === POLYGON ? 'Cancel polygon' : 'Draw polygon'}
        </button>
        <button
          type="button"
          className={`btn${mode === CIRCLE ? ' btn--active' : ''}`}
          onClick={() => (mode === CIRCLE ? cancelDraw() : startDraw(CIRCLE))}
          disabled={!map}
        >
          {mode === CIRCLE ? 'Cancel circle' : 'Draw circle'}
        </button>
      </div>

      {mode === POLYGON ? (
        <p className="hint">
          Click the map to add vertices ({points.length} placed, 3 minimum). The ring is closed for
          you on save.
          {points.length ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setPoints((current) => current.slice(0, -1))}
            >
              undo point
            </button>
          ) : null}
        </p>
      ) : null}

      {mode === CIRCLE ? (
        <p className="hint">
          {center
            ? circleFixed
              ? `Centre set · radius ${formatMeters(radiusM)}. Adjust below, or click "Draw circle" again to restart.`
              : 'Move the pointer to size the circle, then click to fix the radius.'
            : 'Click the map to drop the circle centre.'}
        </p>
      ) : null}

      <form className="zone-form" onSubmit={handleSave}>
        <label className="field">
          <span>Zone name</span>
          <input
            type="text"
            value={name}
            placeholder="e.g. Apapa Depot"
            onChange={(e) => setName(e.target.value)}
            maxLength={120}
          />
        </label>

        <label className="field">
          <span>
            Dwell threshold (seconds)
            <small> — blank disables dwell events</small>
          </span>
          <input
            type="number"
            min="1"
            step="1"
            value={dwell}
            placeholder="300"
            onChange={(e) => setDwell(e.target.value)}
          />
        </label>

        {mode === CIRCLE && center ? (
          <label className="field">
            <span>Radius (metres)</span>
            <input
              type="number"
              min="1"
              max={MAX_RADIUS_M}
              step="1"
              value={radiusM}
              onChange={(e) => {
                circleFixedRef.current = true;
                setCircleFixed(true);
                setRadiusM(e.target.value === '' ? '' : Number(e.target.value));
              }}
            />
          </label>
        ) : null}

        {editingId ? (
          <p className="hint hint--editing">
            Editing <strong>{editingZone?.name ?? editingId}</strong>.{' '}
            {hasDraftGeometry
              ? 'Geometry will be replaced with the new drawing.'
              : 'Geometry unchanged — draw a new shape to replace it.'}
          </p>
        ) : null}

        {formError ? (
          <p className="notice notice--error" role="alert">
            {describeApiError(formError)}
          </p>
        ) : null}
        {notice ? <p className="notice notice--ok">{notice}</p> : null}

        <div className="zone-form__actions">
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Saving…' : editingId ? 'Save changes' : 'Create zone'}
          </button>
          {editingId ? (
            <button type="button" className="btn btn--ghost" onClick={stopEdit} disabled={busy}>
              Cancel edit
            </button>
          ) : null}
        </div>
      </form>

      {error ? (
        <p className="notice notice--error" role="alert">
          {describeApiError(error)}
        </p>
      ) : null}

      {loading && geofences.length === 0 && !error ? (
        <p className="notice">Loading zones…</p>
      ) : null}

      {!loading && geofences.length === 0 && !error ? (
        <p className="notice">No zones yet — draw one above.</p>
      ) : null}

      <ul className="zone-list">
        {geofences.map((zone) => {
          const threshold = formatThreshold(zone.dwell_threshold_seconds);
          return (
            <li key={zone.id} className={`zone-row${editingId === zone.id ? ' zone-row--editing' : ''}`}>
              <div className="zone-row__main">
                <button type="button" className="linklike zone-row__name" onClick={() => zoomToZone(zone)}>
                  {zone.name}
                </button>
                <span className={`shape-badge shape-badge--${zone.shape_type}`}>
                  {zone.shape_type}
                  {zone.shape_type === 'circle' && zone.radius_m
                    ? ` · ${formatMeters(zone.radius_m)}`
                    : ''}
                </span>
              </div>
              <div className="zone-row__meta">
                <span>{threshold ? `dwell ≥ ${threshold}` : 'dwell off'}</span>
                <span>
                  {zone.device_count ?? 0} inside
                </span>
              </div>
              <div className="zone-row__actions">
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() => startEdit(zone)}
                  disabled={busy}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="btn btn--danger btn--sm"
                  onClick={() => handleDelete(zone)}
                  disabled={busy}
                >
                  Delete
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
