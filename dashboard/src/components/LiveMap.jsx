import { useEffect, useMemo, useRef } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { formatCoord, formatMeters, formatThreshold, isStale, relativeTime } from '../utils/format.js';
import { circleBounds, toLatLng } from '../utils/geo.js';

// Leaflet, not Mapbox: OpenStreetMap tiles need no token, so this runs for anyone
// who clones the repo. There is deliberately no VITE_MAPBOX_TOKEN anywhere.
const TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
const TILE_ATTRIBUTION = '&copy; OpenStreetMap contributors';

// Lagos — matches the sample ping in CLAUDE.md. Only used until the first fix arrives.
const FALLBACK_CENTER = [6.5244, 3.3792];
const FALLBACK_ZOOM = 12;

// Reverse-geocoding via Nominatim (free, no key). Cache by truncated coord so small
// GPS drift doesn't trigger duplicate requests.
const geocodeCache = new Map();

async function reverseGeocode(lat, lng) {
  const key = `${Number(lat).toFixed(4)},${Number(lng).toFixed(4)}`;
  if (geocodeCache.has(key)) return geocodeCache.get(key);
  try {
    const res = await fetch(
      `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&accept-language=en`,
      { headers: { 'User-Agent': 'GeoTag-Dashboard/1.0' } }
    );
    const data = await res.json();
    const a = data.address ?? {};
    // Build from structured fields first; fall back to the first 4 tokens
    // of display_name so sparse-OSM areas (e.g. Lagos suburbs) still show
    // something useful like "Festac Town, Amuwo Odofin, Lagos".
    const structured = [
      a.road || a.pedestrian || a.footway || a.path,
      a.suburb || a.neighbourhood || a.quarter || a.village,
      a.city_district || a.county,
      a.city || a.town || a.state,
    ].filter(Boolean);
    const address = structured.length >= 2
      ? structured.join(', ')
      : (data.display_name ? data.display_name.split(',').slice(0, 4).join(',').trim() : null);
    geocodeCache.set(key, address);
    return address;
  } catch {
    geocodeCache.set(key, null);
    return null;
  }
}

/**
 * Live map: one marker per active device, geofence zones as overlays
 * (polygons AND circles, per SPEC-DECISIONS D2), plus the history trail of the
 * selected device. Clicking a marker focuses that device.
 */
export default function LiveMap({
  devices = [],
  geofences = [],
  historyPositions = [],
  selectedDeviceId = null,
  onSelectDevice,
  onMapReady,
  drawing = false,
  banner = null,
  dataReady = false,
}) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const zoneLayerRef = useRef(null);
  const deviceLayerRef = useRef(null);
  const trailLayerRef = useRef(null);
  const markersRef = useRef(new Map());
  const hasFittedRef = useRef(false);
  const userMovedRef = useRef(false);

  // Keep the click handler current without tearing down markers on every render.
  const selectRef = useRef(onSelectDevice);
  selectRef.current = onSelectDevice;

  // --- map lifecycle -------------------------------------------------------
  useEffect(() => {
    const map = L.map(containerRef.current, {
      center: FALLBACK_CENTER,
      zoom: FALLBACK_ZOOM,
      zoomControl: true,
      preferCanvas: true,
    });

    L.tileLayer(TILE_URL, { maxZoom: 19, attribution: TILE_ATTRIBUTION }).addTo(map);

    zoneLayerRef.current = L.layerGroup().addTo(map);
    trailLayerRef.current = L.layerGroup().addTo(map);
    deviceLayerRef.current = L.layerGroup().addTo(map);

    // Once the operator has panned or zoomed, the view is theirs — never re-fit.
    const markMoved = () => {
      userMovedRef.current = true;
    };
    const container = containerRef.current;
    container.addEventListener('pointerdown', markMoved);
    container.addEventListener('wheel', markMoved, { passive: true });

    mapRef.current = map;
    if (onMapReady) onMapReady(map);

    return () => {
      container.removeEventListener('pointerdown', markMoved);
      container.removeEventListener('wheel', markMoved);
      markersRef.current.clear();
      hasFittedRef.current = false;
      userMovedRef.current = false;
      map.remove();
      mapRef.current = null;
      if (onMapReady) onMapReady(null);
    };
    // onMapReady is stable (useCallback in App); re-running would recreate the map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- geofence overlays ---------------------------------------------------
  const zonesSignature = useMemo(() => JSON.stringify(geofences), [geofences]);

  useEffect(() => {
    const layer = zoneLayerRef.current;
    if (!layer) return;
    layer.clearLayers();

    geofences.forEach((zone) => {
      const isCircle = zone.shape_type === 'circle' && Array.isArray(zone.center) && zone.radius_m;
      const style = {
        color: isCircle ? '#7c3aed' : '#2563eb',
        weight: 2,
        opacity: 0.9,
        fillColor: isCircle ? '#7c3aed' : '#2563eb',
        fillOpacity: 0.08,
      };

      // A circle round-trips as center + radius_m (D2). Drawing it as a true
      // circle is more faithful than the materialised buffer ring, which is only
      // used as the fallback.
      let shape;
      if (isCircle) {
        shape = L.circle(toLatLng(zone.center), { radius: Number(zone.radius_m), ...style });
      } else if (Array.isArray(zone.coordinates) && zone.coordinates.length >= 3) {
        shape = L.polygon(zone.coordinates.map(toLatLng), style);
      } else {
        return;
      }

      shape.bindTooltip(zoneTooltip(zone), { sticky: true });
      shape.addTo(layer);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zonesSignature]);

  // --- device markers ------------------------------------------------------
  useEffect(() => {
    const layer = deviceLayerRef.current;
    const map = mapRef.current;
    if (!layer || !map) return;

    const seen = new Set();

    devices.forEach((device) => {
      if (!Number.isFinite(Number(device.lat)) || !Number.isFinite(Number(device.lng))) return;
      seen.add(device.device_id);
      const latLng = [Number(device.lat), Number(device.lng)];
      const selected = device.device_id === selectedDeviceId;
      const icon = deviceIcon(device, selected);

      let marker = markersRef.current.get(device.device_id);
      if (marker) {
        marker.setLatLng(latLng);
        marker.setIcon(icon);
      } else {
        marker = L.marker(latLng, { icon, title: device.device_id });
        marker.on('click', () => {
          if (selectRef.current) selectRef.current(device.device_id);
        });
        marker.addTo(layer);
        markersRef.current.set(device.device_id, marker);
      }
      const cacheKey = `${Number(device.lat).toFixed(4)},${Number(device.lng).toFixed(4)}`;
      marker.bindTooltip(deviceTooltip(device, geocodeCache.get(cacheKey) ?? null), { direction: 'top', offset: [0, -12] });

      if (!geocodeCache.has(cacheKey)) {
        reverseGeocode(device.lat, device.lng).then((address) => {
          const m = markersRef.current.get(device.device_id);
          if (m) m.setTooltipContent(deviceTooltip(device, address));
        });
      }

      marker.setZIndexOffset(selected ? 1000 : 0);
    });

    markersRef.current.forEach((marker, deviceId) => {
      if (!seen.has(deviceId)) {
        layer.removeLayer(marker);
        markersRef.current.delete(deviceId);
      }
    });

  }, [devices, selectedDeviceId]);

  // Fit once to everything worth seeing — the fleet AND the zones. `dataReady`
  // means both polls have answered, so the fit isn't won by whichever raced home
  // first. After that the view belongs to the user.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || hasFittedRef.current || userMovedRef.current || !dataReady) return;
    if (devices.length === 0 && geofences.length === 0) return;

    const corners = [];
    devices.forEach((device) => {
      if (Number.isFinite(Number(device.lat)) && Number.isFinite(Number(device.lng))) {
        corners.push([Number(device.lat), Number(device.lng)]);
      }
    });
    geofences.forEach((zone) => {
      if (zone.shape_type === 'circle' && Array.isArray(zone.center) && zone.radius_m) {
        const box = circleBounds(zone.center, zone.radius_m);
        if (box) corners.push(box.getSouthWest(), box.getNorthEast());
      } else if (Array.isArray(zone.coordinates)) {
        zone.coordinates.forEach((coordinate) => corners.push(toLatLng(coordinate)));
      }
    });

    const bounds = L.latLngBounds(corners);
    if (bounds.isValid()) {
      // maxZoom only guards the degenerate case (one device, no zones) from
      // slamming to street level; a spread-out fleet fits below it anyway.
      map.fitBounds(bounds, { padding: [48, 48], maxZoom: 16 });
      hasFittedRef.current = true;
    }
  }, [devices, geofences, dataReady]);

  // --- history trail of the selected device --------------------------------
  useEffect(() => {
    const layer = trailLayerRef.current;
    if (!layer) return;
    layer.clearLayers();

    const points = historyPositions
      .filter((p) => Number.isFinite(Number(p.lat)) && Number.isFinite(Number(p.lng)))
      .map((p) => [Number(p.lat), Number(p.lng)]);

    if (points.length < 2) return;

    L.polyline(points, { color: '#f97316', weight: 3, opacity: 0.85 }).addTo(layer);
    L.circleMarker(points[0], {
      radius: 5,
      color: '#f97316',
      fillColor: '#fff',
      fillOpacity: 1,
      weight: 2,
    })
      .bindTooltip('Trail start', { direction: 'top' })
      .addTo(layer);
  }, [historyPositions]);

  // --- focus the selected device -------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selectedDeviceId) return;
    const device = devices.find((d) => d.device_id === selectedDeviceId);
    if (!device) return;
    map.setView([Number(device.lat), Number(device.lng)], Math.max(map.getZoom(), 15), {
      animate: true,
    });
    // Only re-centre when the selection changes, not on every position poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDeviceId]);

  return (
    <div className={`map-shell${drawing ? ' map-shell--drawing' : ''}`}>
      <div ref={containerRef} className="map-canvas" data-testid="live-map" />
      {banner ? <div className="map-banner">{banner}</div> : null}
      <div className="map-legend">
        <span><i className="swatch swatch--device" /> device</span>
        <span><i className="swatch swatch--polygon" /> polygon zone</span>
        <span><i className="swatch swatch--circle" /> circle zone</span>
        <span><i className="swatch swatch--trail" /> history trail</span>
      </div>
    </div>
  );
}

function deviceIcon(device, selected) {
  const stale = isStale(device.recorded_at);
  const classes = ['device-pin'];
  if (selected) classes.push('device-pin--selected');
  if (stale) classes.push('device-pin--stale');
  return L.divIcon({
    className: 'device-pin-wrapper',
    html: `<span class="${classes.join(' ')}"></span>`,
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    return map[char];
  });
}

function deviceTooltip(device, address = null) {
  const battery = device.battery === null || device.battery === undefined ? '—' : `${device.battery}%`;
  const lines = [
    `<strong>${escapeHtml(device.device_id)}</strong>`,
    `battery ${escapeHtml(battery)}`,
    `seen ${escapeHtml(relativeTime(device.recorded_at))}`,
    `${formatCoord(device.lat)}, ${formatCoord(device.lng)}`,
  ];
  if (address) lines.push(`<span style="color:#555;font-style:italic">${escapeHtml(address)}</span>`);
  return lines.join('<br/>');
}

function zoneTooltip(zone) {
  const lines = [`<strong>${escapeHtml(zone.name)}</strong>`, escapeHtml(zone.shape_type)];
  if (zone.shape_type === 'circle' && zone.radius_m) {
    lines.push(`radius ${escapeHtml(formatMeters(zone.radius_m))}`);
  }
  const threshold = formatThreshold(zone.dwell_threshold_seconds);
  lines.push(threshold ? `dwell after ${escapeHtml(threshold)}` : 'dwell disabled');
  if (zone.device_count !== null && zone.device_count !== undefined) {
    lines.push(`${escapeHtml(zone.device_count)} device(s) inside`);
  }
  return lines.join('<br/>');
}
